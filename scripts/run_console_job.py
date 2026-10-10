"""Private stdin protocol for console jobs; credentials never enter argv."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(payload):
    os.umask(0o077)
    credentials = payload["credentials"]
    os.environ.update({"StuId": credentials["stuid"], "UISPsw": credentials["uispsw"],
        "DB_PATH": payload["db_path"], "DATA_DIR": str(Path(payload["db_path"]).parent),
        "COURSE_IDS": ",".join(payload["course_ids"]),
        "MODEL_PROVIDERS_JSON": json.dumps(payload["providers"]),
        "LECTURE_ORDER": payload["lecture_order"], "LOCAL_RUN_EVENTS": "1",
        "PUBLISH_NOTE_PROGRESS": "0", "SUMMARY_PROVIDER": payload.get("provider", ""),
        "SUMMARY_MODEL": payload.get("model", ""),
        "USE_OFFICIAL_TRANSCRIPT": "1" if payload.get("use_official_transcript") else "0"})
    os.environ.update(payload["api_keys"])
    if payload["kind"] == "process" and not shutil.which("ffmpeg"):
        import imageio_ffmpeg
        binary = Path(payload["db_path"]).parent / "ffmpeg"
        binary.symlink_to(imageio_ffmpeg.get_ffmpeg_exe())
        os.environ["PATH"] = str(binary.parent) + os.pathsep + os.environ.get("PATH", "")
    if payload["kind"] == "rerun":
        os.environ["RESUMMARIZE_SUB_IDS"] = ",".join(payload["sub_ids"])
        from scripts.resummarize import run as resummarize
        return resummarize()
    if payload["kind"] == "titles":
        os.environ["TITLE_COURSE_IDS"] = ",".join(payload["course_ids"])
        from scripts.generate_titles import run as titles
        return titles()
    import main
    from local_web.state import RuntimeCredentials
    from scripts import local_course
    from scripts.local_pipeline import LocalLectureRunner, StrictPPTClient
    from scripts.local_audio import LocalAudioDownloader
    from src.pipeline.lecture_runner import LectureRunner
    from src.runtime.progress import emit
    # The console already has credentials; local login uses the native HTTPS
    # adapter required by the macOS WebVPN endpoint.
    creds = RuntimeCredentials("", credentials["stuid"], credentials["uispsw"])
    client = local_course.login(creds)
    from src.data.database import Database
    from src.runtime.reporter import Reporter
    from src.runtime.scheduler import Scheduler
    from src.ai.summarizer import Summarizer
    from src.ai.transcriber import Transcriber
    db = Database()
    reporter = Reporter()
    scheduler = Scheduler(reporter=reporter)
    scheduler.audio_downloader.shutdown()
    scheduler.audio_downloader = LocalAudioDownloader(
        Path(payload["db_path"]).parent / "media", local_course.write_private,
        reporter=reporter)
    client = StrictPPTClient(client, db, local_course)
    def check_session(value):
        if not value.check_alive():
            refreshed = local_course.login(creds)
            value.client.vpn.session.close()
            value.client.vpn = refreshed.vpn
            value.client._userinfo = None
    main._check_session = check_session
    summarizer = Summarizer()

    class ConsoleLectureRunner(LocalLectureRunner):
        def _needs_audio(self, sub_id):
            existing = self._db.get_lecture(sub_id) or {}
            if main.config.USE_OFFICIAL_TRANSCRIPT and not existing.get("transcript"):
                return LectureRunner._needs_audio(self, sub_id)
            return super()._needs_audio(sub_id)

        def _get_transcript(self, existing, course_id, sub_id):
            if main.config.USE_OFFICIAL_TRANSCRIPT and not (existing or {}).get("transcript"):
                try:
                    segments = self._official_cache.pop(sub_id, None)
                    if segments is None:
                        segments = self._client.get_transcript_segments(sub_id)
                    if self._official_transcript_usable(segments, duration_hint_s=self._db.get_max_ppt_created_sec(sub_id)):
                        text = " ".join(row["text"] for row in segments)
                        if text.strip():
                            self._db.update_transcript(sub_id, text)
                            self.local.save_meta(self._db, sub_id, "segments", segments)
                            self.local.save_meta(self._db, sub_id, "audio", {"asr_backend": "official"})
                            self.checkpoint()
                            self._release_audio(sub_id)
                            return text, segments
                except Exception as exc:
                    self._reporter.info(f"官方转录不可用，继续本机识别（{type(exc).__name__}）")
            return super()._get_transcript(existing, course_id, sub_id)

        def _summarize(self, sub_id, course_title, transcript, transcript_segments):
            if not self.local.meta(self._db, sub_id, "ocr"):
                count = self._client.source_count(sub_id)
                with self._db._lock:
                    failed = self._db.conn.execute(
                        "SELECT count(*) FROM ppt_pages WHERE sub_id=? AND ocr_status IN ('pending','failed')", (sub_id,)).fetchone()[0]
                if failed:
                    raise ValueError(f"仍有 {failed} 页课件未完成 OCR")
                pages = self._db.get_done_ppt_pages(sub_id)
                if count and not any(page.get("text", "").strip() for page in pages):
                    raise ValueError("有课件截图但未得到有效 OCR 文字")
                self.local.save_meta(self._db, sub_id, "ocr", {"source_pages": count, "kept_pages": len(pages)})
                self.checkpoint()
            return LectureRunner._summarize(self, sub_id, course_title, transcript, transcript_segments)

    runner = ConsoleLectureRunner(client, db, scheduler, Transcriber(), reporter,
        checkpoint=lambda: emit("checkpoint", sub_id=str(runner.lecture["sub_id"])),
        runner=local_course, summary_worker=None)
    runner._summarizer = summarizer
    try:
        lectures = main._enumerate_lectures(client, db, reporter, force_video_recheck=True)
        main._drive_lectures(client, db, scheduler, runner._transcriber, summarizer, reporter, lectures, runner=runner)
    finally:
        scheduler.shutdown()
        db.conn.close()
        client.vpn.session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run(json.loads(sys.stdin.readline())))
