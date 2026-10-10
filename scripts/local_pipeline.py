"""Reuse the Actions lecture state machine with local integrity gates."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import time

from src.pipeline.lecture_runner import LectureRunner
from src.api.icourse import VideoAccessError, VideoLookupError, VideoNotReadyError


class StrictPPTClient:
    """Keep the shared prefetcher from treating a failed list as zero pages."""
    def __init__(self, client, db, runner):
        self.client, self.db, self.runner = client, db, runner
        self.items, self.errors = {}, {}
        self.lock = threading.RLock()

    def __getattr__(self, name):
        return getattr(self.client, name)

    def get_ppt_list(self, course_id, sub_id):
        sub_id = str(sub_id)
        with self.lock:
            if self.runner.meta(self.db, sub_id, "ocr"):
                return []  # verified OCR rows already live in the database
            if sub_id in self.errors:
                raise self.errors[sub_id]
            if sub_id not in self.items:
                try:
                    self.items[sub_id] = self.client.get_ppt_list(course_id, sub_id)
                except Exception as exc:
                    error = RuntimeError(self.runner.safe_error(exc))
                    self.errors[sub_id] = error
                    raise error from None
            return [dict(item) for item in self.items[sub_id]]

    def source_count(self, sub_id):
        with self.lock:
            if sub_id in self.errors:
                raise self.errors[sub_id]
            if sub_id not in self.items:
                raise ValueError("课件清单尚未成功读取")
            return len(self.items[sub_id])

    def retry(self, sub_id):
        with self.lock:
            self.errors.pop(sub_id, None)


class SummaryWorker:
    """API calls or source-bound connector results overlap later lectures.

    This is part of the running local job, not a Codex scheduled task.
    No connector credentials are copied into the Python process.
    """
    def __init__(self, db, checkpoint, manifest, runner, summarizer=None):
        self.db, self.checkpoint, self.manifest, self.runner = db, checkpoint, manifest, runner
        self.summarizer = summarizer
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-summary") if summarizer else None
        self.scheduled, self.rejected = set(), {}
        self.export_lock = threading.RLock()
        self.stop = threading.Event()
        self.inbox = runner.RUN_DIR / "summary-results"
        self.inbox.mkdir(exist_ok=True)
        self.thread = threading.Thread(target=self._watch, name="local-summary-inbox", daemon=True)
        self.thread.start()

    def schedule(self, lecture):
        sub_id = str(lecture["sub_id"])
        if self.pool and sub_id not in self.scheduled:
            self.scheduled.add(sub_id)
            self.pool.submit(self._summarize, lecture)

    def _export(self, lecture):
        with self.export_lock:
            self.runner.export_note(self.db, lecture)
            self.runner.export_index(self.db, self.manifest)

    def _summarize(self, lecture):
        sub_id = str(lecture["sub_id"])
        for attempt in range(3):
            try:
                # A matching complete connector result takes precedence
                # over a new paid API call after a restart.
                result = self.inbox / f"{sub_id}.json"
                if result.exists():
                    try:
                        self.runner.import_summary(self.db, self.checkpoint, lecture, result)
                    except Exception as exc:
                        print(f"[{sub_id}] 已有摘要结果不可导入：{self.runner.safe_error(exc)}", flush=True)
                self.runner.summarize(self.db, self.checkpoint, lecture, self.summarizer)
                self._export(lecture)
                return
            except Exception as exc:
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                error = self.runner.safe_error(exc)
                self.db.update_error(sub_id, "summarize", error)
                self.checkpoint()
                print(f"[{sub_id}] 摘要失败：{error}", flush=True)

    def consume(self):
        for lecture in self.runner.lecture_list(self.manifest):
            sub_id = str(lecture["sub_id"])
            if not (self.runner.meta(self.db, sub_id, "audio") and self.runner.meta(self.db, sub_id, "ocr")):
                continue
            if (self.db.get_lecture(sub_id) or {}).get("summary"):
                continue
            if self.pool and sub_id in self.scheduled:
                continue  # the API worker also checks existing result files
            path = self.inbox / f"{sub_id}.json"
            if not path.exists():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if self.rejected.get(sub_id) == digest:
                continue
            try:
                self.runner.import_summary(self.db, self.checkpoint, lecture, path)
                self._export(lecture)
            except Exception as exc:
                self.rejected[sub_id] = digest
                print(f"[{sub_id}] 拒绝摘要结果：{self.runner.safe_error(exc)}", flush=True)

    def _watch(self):
        while not self.stop.wait(2):
            try:
                self.consume()
            except Exception as exc:
                print(f"摘要收件检查失败：{self.runner.safe_error(exc)}", flush=True)

    def shutdown(self):
        if self.pool:
            self.pool.shutdown(wait=True)
        self.stop.set()
        self.thread.join()
        self.consume()


class LocalLectureRunner(LectureRunner):
    """Actions prefetch/ASR/OCR phases, with stricter commits and async notes."""
    def __init__(self, client, db, scheduler, transcriber, reporter, *, checkpoint, runner, summary_worker):
        super().__init__(client, db, scheduler, transcriber, None, reporter)
        self.local, self.checkpoint, self.summary_worker = runner, checkpoint, summary_worker

    def run(self, *args, **kwargs):
        lecture = args[2] if len(args) > 2 else kwargs["lecture"]
        sub_id = str(lecture["sub_id"])
        self.lecture = lecture
        # Failed OCR rows need retry; done/invalid/dedup rows stay untouched.
        with self._db._lock, self._db.conn:
            self._db.conn.execute("UPDATE ppt_pages SET ocr_status='pending' WHERE sub_id=? AND ocr_status='failed'", (sub_id,))
        try:
            return super().run(*args, **kwargs)
        finally:
            self._release_audio(sub_id)

    def _release_audio(self, sub_id):
        # The shared state machine also releases when its summary hook
        # returns None. Route every release through the same commit gate.
        self._scheduler.audio_downloader.release(sub_id, verified=bool(self.local.meta(self._db, sub_id, "audio")))

    def _needs_audio(self, sub_id):
        row = self._db.get_lecture(sub_id) or {}
        return not (row.get("transcript") and self.local.meta(self._db, sub_id, "audio"))

    def _get_transcript(self, existing, course_id, sub_id):
        if not self._needs_audio(sub_id):
            return existing["transcript"], self.local.meta(self._db, sub_id, "segments")
        downloader = self._scheduler.audio_downloader
        downloader.schedule(self._client, course_id, sub_id)
        try:
            handle = downloader.get(sub_id, timeout=7200)
        except (VideoAccessError, VideoNotReadyError) as exc:
            fallback = self._official_video_fallback(sub_id)
            if fallback[0] is not None:
                return fallback
            stage = "video_access" if isinstance(exc, VideoAccessError) else "no_video"
            self._db.update_error(sub_id, stage, str(exc))
            self.checkpoint()
            return None, None
        except (VideoLookupError, TimeoutError) as exc:
            message = str(exc) if isinstance(exc, VideoLookupError) else "录播准备超时，稍后自动重试"
            self._db.update_error(sub_id, "video", message)
            self.checkpoint()
            return None, None
        except InterruptedError:
            raise
        except Exception as exc:
            self._db.update_error(sub_id, "video", f"录播准备失败，稍后自动重试（{type(exc).__name__}）")
            self.checkpoint()
            return None, None
        if handle is None:
            fallback = self._official_video_fallback(sub_id)
            if fallback[0] is not None:
                return fallback
            self._db.update_error(sub_id, "no_video", "平台尚未提供可播放的录播地址；将定期自动复查")
            self.checkpoint()
            return None, None
        text, segments = self._transcriber.transcribe_tail(handle.path, handle.process, handle.stderr_chunks)
        media = downloader.verify(sub_id)
        expected, actual = self._transcriber._media_duration or 0, self._transcriber._last_duration
        self.local.verify_duration(expected, actual)
        if not text.strip():
            raise ValueError("完整音频未识别出文字，需检查录音")
        self._db.update_transcript(sub_id, text)
        self.local.save_meta(self._db, sub_id, "segments", segments)
        self.local.save_meta(self._db, sub_id, "audio", {
            "expected_seconds": expected, "decoded_seconds": actual,
            "transcript_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "asr_backend": "sensevoice", "asr_threads": self._transcriber._num_threads,
            "media_bytes": media["total_bytes"], "media_source_sha256": media["source_sha256"],
            "decode_mode": media["decode_mode"], "request_count": media.get("request_count"),
            "signature_count": media.get("signature_count"),
        })
        self.checkpoint()
        print(f"[{sub_id}] 字节数与音频时长均通过，转录已保存加密检查点", flush=True)
        return text, segments

    def _official_video_fallback(self, sub_id):
        text, segments = super()._official_video_fallback(sub_id)
        if text is not None:
            self.local.save_meta(self._db, sub_id, "segments", segments)
            self.local.save_meta(self._db, sub_id, "audio", {
                "asr_backend": "official",
                "transcript_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "recovery": "video_unavailable",
            })
            self.checkpoint()
        return text, segments

    def _summarize(self, sub_id, course_title, transcript, transcript_segments):
        if not self.local.meta(self._db, sub_id, "ocr"):
            count = self._client.source_count(sub_id)
            with self._db._lock:
                failed = self._db.conn.execute("SELECT count(*) FROM ppt_pages WHERE sub_id=? AND ocr_status IN ('pending','failed')", (sub_id,)).fetchone()[0]
            if failed:
                self.checkpoint()
                raise ValueError(f"仍有 {failed} 页课件未完成 OCR")
            pages = self._db.get_done_ppt_pages(sub_id)
            if count and not any(page.get("text", "").strip() for page in pages):
                self.checkpoint()
                raise ValueError("有课件截图但未得到有效 OCR 文字，需检查截图内容")
            self.local.save_meta(self._db, sub_id, "ocr", {"source_pages": count, "kept_pages": len(pages)})
            self._db.clear_error(sub_id)
            self.checkpoint()
        self.local.write_summary_request(self._db, self.lecture)
        self.local.export_materials(self._db, self.lecture)
        self.summary_worker.schedule(self.lecture)
        print(f"[{sub_id}] 材料验收完成，摘要可与后续课次处理重叠", flush=True)
        # Shared runner must not mark a prepared lecture as a finished note.
        return None
