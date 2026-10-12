"""Read ASR/official text, add optional slides, summarize, and checkpoint.

Primary material is read before bounded slide preparation/OCR. Official
text is independently usable when video is absent or denied. The caller
checks the session and supervises the lecture's entire process lifetime.
Legacy local preparation can still overlap the next lecture via next_info.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from src.ai import bucketer
from src.ai.title import build_title_material, split_generated_title
from src.pipeline.ppt_pipeline import PPTPipeline
from src.ai.transcriber import IncompleteAudioError, NoAudioStreamError
from src.api.icourse import VideoAccessError, VideoLookupError, VideoNotReadyError
from src.api.materials import transcript_problem
from src.runtime import config
from src.runtime.material_deadline import MaterialTimeout, material_deadline

if TYPE_CHECKING:
    from src.data.database import Database
    from src.api.icourse import ICourseClient
    from src.runtime.reporter import Reporter
    from src.runtime.scheduler import Scheduler
    from src.ai.summarizer import Summarizer
    from src.ai.transcriber import Transcriber


class LectureRunner:
    """Run one lecture end-to-end.  Construct once per orchestration session,
    call ``run`` for each lecture in order."""

    def __init__(self, client: "ICourseClient", db: "Database",
                 scheduler: "Scheduler", transcriber: "Transcriber",
                 summarizer: "Summarizer", reporter: "Reporter"):
        self._client = client
        self._db = db
        self._scheduler = scheduler
        self._transcriber = transcriber
        self._summarizer = summarizer
        self._reporter = reporter
        self._ppt = PPTPipeline(db, scheduler, reporter)
        # Official-transcript segments fetched during the prefetch decision
        # (``_needs_audio``), keyed by sub_id, so ``_get_transcript`` doesn't
        # re-fetch them one lecture later.
        self._official_cache: dict[str, list[dict]] = {}
        self._official_failures: dict[str, str] = {}

    # ── Public entry point ──────────────────────────────────────────────

    def run(self, course_id: str, course_title: str, lecture: dict,
            next_info: Optional[tuple[str, str]] = None) -> Optional[str]:
        """Process one lecture.  Returns the summary text or None.

        ``next_info``: ``(course_id, sub_id)`` of the next lecture, used to
        kick off its prefetch concurrently.  Pass ``None`` for the last
        lecture in the batch.
        """
        sub_id = str(lecture["sub_id"])
        sub_title = lecture.get("sub_title", sub_id)
        date = lecture.get("date", "")
        t_start = time.time()
        self._reporter.lecture_start(course_title, sub_title, date)

        existing = self._db.get_lecture(sub_id)
        # ── Phase A — short-circuit if a summary already exists ─────────
        if self._has_summary(existing):
            self._reporter.lecture_skip_v2_done(
                sub_title, len(existing["summary"])
            )
            self._schedule_next(next_info)
            self._db.mark_processed(sub_id)
            self._db.clear_error(sub_id)
            return existing["summary"]

        # ── Phase C — schedule next lecture's prefetch ─────────────────
        # Done BEFORE ASR so the next audio download can start filling its
        # AudioDownloader slot while we transcribe.  Both audio + image
        # prefetches are idempotent so this is safe to call any time.
        self._schedule_next(next_info)

        # ── Phase D — ASR transcription ────────────────────────────────
        transcript, transcript_segments = self._get_transcript(
            existing, course_id, sub_id,
        )
        if transcript is None:
            # _get_transcript already logged + persisted the skip reason.
            return None

        # Read the primary material first. Optional slides must not delay
        # the official-transcript fallback or waste OCR on empty lectures.
        try:
            with material_deadline(180):
                ppt_handle = self._ppt.submit(
                    self._client, course_id, sub_id, defer_ocr=True,
                )
        except MaterialTimeout:
            ppt_handle = None
            self._warn_ppt(sub_id, "课件准备超时")
        except Exception as exc:
            ppt_handle = None
            self._warn_ppt(sub_id, f"课件准备失败（{type(exc).__name__}）")

        # ── Phase E — drain remaining OCR work ─────────────────────────
        self._finish_ppt(sub_id, ppt_handle)

        # ── Phase E2 — kick off next lecture's OCR (runs during LLM) ───
        # Images were prefetched in Phase C; prefetch_and_ocr spawns a
        # background thread that collects them, dedups and submits OCR,
        # so the whole thing genuinely overlaps with this lecture's LLM
        # wait.  Whatever isn't finished when the LLM returns is absorbed
        # by the next lecture's own PPTPipeline.submit().
        if next_info:
            next_course, next_sub = next_info
            self._ppt.prefetch_and_ocr(self._client, next_course, next_sub)

        # ── Phase F — bucketed-prompt LLM summary ──────────────────────
        if not transcript.strip():
            self._reporter.info("    Empty transcript, skipping summary.")
            self._release_audio(sub_id)
            self._db.mark_processed(sub_id)
            self._db.clear_error(sub_id)
            return None

        summary = self._summarize(
            sub_id, course_title, transcript, transcript_segments,
        )
        if summary is None:
            self._release_audio(sub_id)
            return None

        # ── Phase G — persist + clear errors ───────────────────────────
        self._db.mark_processed(sub_id)
        self._db.clear_error(sub_id)

        # ── Phase H — release audio resources (ffmpeg + file) ──────────
        self._release_audio(sub_id)

        elapsed = time.time() - t_start
        self._reporter.lecture_done(course_title, sub_title, elapsed)
        return summary

    def _warn_ppt(self, sub_id, message):
        self._db.update_ppt_warning(sub_id, message)
        self._reporter.info(f"    [PPT] {message}；课件作为补充材料")

    def _finish_ppt(self, sub_id, handle):
        if handle is None:
            return
        try:
            with material_deadline(300):
                stats = handle.drain()
            failure_count = self._db.get_ppt_failure_count(sub_id)
            warning = getattr(stats, "list_error", None)
            if not isinstance(warning, str):
                warning = None
            if failure_count:
                warning = "；".join(filter(None, (warning, f"{failure_count} 页课件未完成 OCR")))
            elif getattr(stats, "failed", 0) and isinstance(stats.failed, int):
                warning = warning or f"{stats.failed} 页课件处理失败"
            if warning:
                self._warn_ppt(sub_id, warning)
            else:
                self._db.update_ppt_warning(sub_id, None)
        except MaterialTimeout:
            self._warn_ppt(sub_id, "课件 OCR 超时")
        except Exception as exc:
            self._warn_ppt(sub_id, f"课件处理失败（{type(exc).__name__}）")

    # ── Internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _has_summary(existing: dict | None) -> bool:
        return bool(
            existing
            and existing.get("summary")
        )

    def prefetch_first(self, course_id: str, sub_id: str) -> None:
        """Prefetch for the first lecture in the batch — same decision
        logic (skip the audio download when transcription won't need it)
        as the in-loop Phase C prefetch."""
        self._schedule_next((course_id, sub_id))

    def _schedule_next(self, next_info: Optional[tuple[str, str]]):
        if next_info is None:
            return
        next_course, next_sub = next_info
        # Image prefetch is always useful; the audio download — a full
        # ffmpeg pull of the lecture — only when the next lecture will
        # actually be ASR-transcribed.  Both are idempotent so repeated
        # invocations are no-ops; the audio side blocks on its semaphore
        # until a download slot frees.
        try:
            self._scheduler.prefetch_lecture(
                self._client, next_course, next_sub,
                audio=self._needs_audio(next_sub),
            )
        except InterruptedError:
            raise
        except Exception as exc:
            self._reporter.info(f"    课次 {next_sub} 预取失败（{type(exc).__name__}），处理时重新检查")

    def _needs_audio(self, sub_id: str) -> bool:
        """False when transcription won't need the audio stream: a cached
        transcript exists, or the official transcript looks usable.  Keeps
        prefetching from spending a download slot (and a full lecture of
        bandwidth) on audio that would just be killed in Phase H."""
        existing = self._db.get_lecture(sub_id)
        if existing and existing.get("transcript") and existing.get("transcript_source") == "asr":
            return False
        if config.USE_OFFICIAL_TRANSCRIPT or (existing or {}).get("transcript_source") == "official":
            try:
                segments = self._checked_official_transcript(sub_id)
                if segments:
                    self._official_cache[sub_id] = segments
                    return False
            except Exception as e:
                self._reporter.info(
                    f"    [Official transcript] probe for {sub_id} failed: "
                    f"{type(e).__name__}: {e}"
                )
        return True

    def _checked_official_transcript(self, sub_id):
        row = self._db.get_lecture(sub_id) or {}
        segments = self._official_cache.pop(sub_id, None)
        if segments is None:
            segments = self._client.get_transcript_segments(sub_id)
        problem = transcript_problem(segments)
        if problem:
            self._official_failures[sub_id] = problem
            self._reporter.info(f"    [Official transcript] {problem}")
            return None
        try:
            evidence = self._client.get_recording_duration(row.get("course_id", ""), sub_id)
        except InterruptedError:
            raise
        except Exception as exc:
            evidence = None
            self._reporter.info(f"    录播时长读取失败（{type(exc).__name__}），继续使用官方转录")
        if isinstance(evidence, dict):
            self._db.update_recording_duration(sub_id, evidence.get("seconds"), evidence.get("source"))
        row = self._db.get_lecture(sub_id) or {}
        duration = row.get("recording_duration") if row.get("recording_duration_source") else None
        problem = transcript_problem(segments, duration)
        if problem:
            self._official_failures[sub_id] = problem
            self._reporter.info(f"    [Official transcript] {problem}")
            return None
        self._official_failures.pop(sub_id, None)
        return segments

    def _availability_message(self, sub_id, message):
        problem = self._official_failures.get(sub_id)
        return f"{message}；{problem}" if problem else message

    def _get_transcript(self, existing: dict | None, course_id: str,
                        sub_id: str) -> tuple[Optional[str], Optional[list]]:
        """Return (transcript, segments) or (None, None) on skip.

        Tries the official iCourse transcript first — when available and
        complete-enough (no >20 min silence gaps) it replaces the ASR
        step entirely, saving ~5 min of CPU time per lecture.
        """
        if existing and existing.get("transcript") and existing.get("transcript_source") == "asr":
            self._reporter.info(
                f"    Transcript exists "
                f"({len(existing['transcript'])} chars), "
                f"skipping transcription."
            )
            return existing["transcript"], None

        # Explicit official-first preference uses the same integrity gate.
        if config.USE_OFFICIAL_TRANSCRIPT or (existing or {}).get("transcript_source") == "official":
            fallback = self._official_video_fallback(sub_id)
            if fallback[0] is not None:
                return fallback

        # Pull the audio handle.  ``schedule`` is idempotent — usually the
        # previous lecture already kicked it off (Phase C), but for the
        # first lecture in the batch we still need to fire it ourselves.
        downloader = self._scheduler.audio_downloader
        downloader.schedule(self._client, course_id, sub_id)
        try:
            handle = downloader.get(sub_id, timeout=120)
        except VideoAccessError as e:
            fallback = self._official_video_fallback(sub_id)
            if fallback[0] is not None:
                return fallback
            self._reporter.info(f"    [ACCESS] {e}")
            self._db.update_error(sub_id, "video_access", self._availability_message(sub_id, str(e)))
            return None, None
        except VideoNotReadyError as e:
            fallback = self._official_video_fallback(sub_id)
            if fallback[0] is not None:
                return fallback
            self._reporter.info(f"    [WAIT] {e}")
            self._db.update_error(sub_id, "no_video", self._availability_message(sub_id, str(e)))
            return None, None
        except VideoLookupError as e:
            fallback = self._official_video_fallback(sub_id)
            if fallback[0] is not None:
                return fallback
            self._reporter.info(f"    [RETRY] {e}")
            self._db.update_error(sub_id, "video", self._availability_message(sub_id, str(e)))
            return None, None
        except TimeoutError as e:
            fallback = self._official_video_fallback(sub_id)
            if fallback[0] is not None:
                return fallback
            self._reporter.info(f"    [SKIP] {e}")
            self._db.update_error(sub_id, "video", self._availability_message(sub_id, "录播准备超时，稍后自动重试"))
            return None, None
        except InterruptedError:
            raise
        except Exception as e:
            fallback = self._official_video_fallback(sub_id)
            if fallback[0] is not None:
                return fallback
            # Spawning FFmpeg/reading metadata can fail before an audio handle
            # exists. Do not turn that failure into a false no_video result.
            message = f"录播准备失败，稍后自动重试（{type(e).__name__}）"
            self._reporter.info(f"    [RETRY] {message}")
            self._db.update_error(sub_id, "video", self._availability_message(sub_id, message))
            return None, None
        if handle is None:
            fallback = self._official_video_fallback(sub_id)
            if fallback[0] is not None:
                return fallback
            # Compatibility with downloaders that return None for a genuinely
            # missing recording. Availability checks use a bounded cooldown.
            self._reporter.lecture_skip_no_video(
                existing.get("sub_title", sub_id) if existing else sub_id
            )
            self._db.update_error(sub_id, "no_video", self._availability_message(sub_id, "平台尚未提供可播放的录播地址；将定期自动复查"))
            return None, None

        try:
            transcript, segments = self._transcriber.transcribe_tail(
                handle.path, handle.process, handle.stderr_chunks,
            )
        except NoAudioStreamError as e:
            self._reporter.info(f"    [SKIP] Video-only (no audio stream): {e}")
            self._db.update_error(sub_id, "transcribe", str(e))
            self._db.mark_processed(sub_id)
            self._release_audio(sub_id)
            return None, None
        except IncompleteAudioError as e:
            # Truncated download (ffmpeg may even exit 0 on a server-side
            # cut).  Don't persist the partial transcript — it would
            # short-circuit the retry — just record the error so the
            # lecture is retried up to max_errors times.
            self._reporter.info(
                f"    [SKIP] Incomplete audio, will retry next run: {e}"
            )
            self._db.update_error(sub_id, "transcribe", str(e))
            self._release_audio(sub_id)
            return None, None
        except Exception as e:
            self._reporter.info(
                f"    [FAIL] Transcription error: {type(e).__name__}: {e}"
            )
            self._db.update_error(sub_id, "transcribe", str(e))
            self._release_audio(sub_id)
            raise

        self._db.update_transcript(sub_id, transcript)
        return transcript, segments

    def _official_video_fallback(self, sub_id: str) -> tuple[Optional[str], Optional[list]]:
        """Use readable official text independently of video availability."""
        try:
            segments = self._checked_official_transcript(sub_id)
            if not segments:
                return None, None
            text = " ".join(s["text"] for s in segments)
            self._db.update_transcript(sub_id, text, source="official")
            row = self._db.get_lecture(sub_id) or {}
            check = "已核对录播时长" if row.get("recording_duration") else "平台未提供录播时长"
            self._reporter.info(f"    采用官方转录（{len(text)} 字，{check}）")
            self._release_audio(sub_id)
            return text, segments
        except InterruptedError:
            raise
        except Exception as exc:
            self._official_failures[sub_id] = f"官方转录核验失败（{type(exc).__name__}）"
            self._reporter.info(f"    {self._official_failures[sub_id]}，保留待复查状态")
            return None, None

    def _summarize(self, sub_id: str, course_title: str, transcript: str,
                   transcript_segments: list[dict] | None) -> Optional[str]:
        try:
            kept_pages = self._db.get_done_ppt_pages(sub_id)
            prompt_text, mode = bucketer.assemble(
                transcript, transcript_segments, kept_pages,
            )
            self._reporter.info(
                f"    [Time] Generating summary at "
                f"{time.strftime('%H:%M:%S')}"
                f" — mode={mode}, prompt={len(prompt_text)} chars"
            )
            summary, model_used = self._summarizer.summarize(
                course_title, prompt_text,
            )
            self._reporter.info(
                f"    [OK] Summary by {model_used}: {len(summary)} chars"
            )
            # The prompt asks the model to lead with `# <title>`, so one call
            # yields both summary and title (saves a second API request).
            ai_title, body = split_generated_title(summary)
            self._db.update_summary(sub_id, body, model_used)
            self._save_ai_title(sub_id, ai_title, course_title, transcript, kept_pages)
            return body
        except Exception as e:
            self._reporter.info(
                f"    [FAIL] Summarization error: {type(e).__name__}: {e}"
            )
            self._db.update_error(sub_id, "summarize", str(e))
            raise

    def _save_ai_title(self, sub_id: str, ai_title: str, course_title: str,
                       transcript: str, kept_pages: list[dict]):
        """Persist the note title.  It normally arrives embedded in the
        summary output; only when missing do we spend a second, tiny call.
        Any failure leaves the previous title untouched."""
        try:
            title = (ai_title or "").strip()
            if not title:
                material = build_title_material(course_title, transcript, kept_pages)
                title, _title_model = self._summarizer.generate_title(material)
            self._db.update_ai_title(sub_id, title)
        except Exception as e:
            self._reporter.info(
                f"    [WARN] title generation failed: {type(e).__name__}: {e}"
            )

    def _release_audio(self, sub_id: str):
        try:
            self._scheduler.audio_downloader.release(sub_id)
        except Exception as e:
            self._reporter.info(
                f"    [WARN] audio release failed: {type(e).__name__}: {e}"
            )


