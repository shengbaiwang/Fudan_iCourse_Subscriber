"""One lecture in a disposable process; parent supervises its full lifetime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def restore_client(payload):
    import requests
    from src.api.icourse import ICourseClient
    from src.api.webvpn import WebVPNSession

    vpn = WebVPNSession()
    if payload.get("local"):
        from scripts.local_http import BrowserHTTPSAdapter
        vpn.session.mount("https://webvpn.fudan.edu.cn/", BrowserHTTPSAdapter())
    vpn.session.headers.update(payload["session"]["headers"])
    for cookie in payload["session"]["cookies"]:
        vpn.session.cookies.set_cookie(requests.cookies.create_cookie(**cookie))
    vpn.logged_in = True
    return ICourseClient(vpn)


def run(payload, *, wait=True):
    os.umask(0o077)
    from src.data.database import Database
    from src.runtime.reporter import Reporter
    from src.runtime.scheduler import Scheduler
    from src.ai.summarizer import Summarizer
    from src.ai.transcriber import Transcriber
    from src.pipeline.lecture_runner import LectureRunner

    db = Database(payload["db_path"])
    reporter = Reporter()
    client = restore_client(payload)
    scheduler = Scheduler(reporter=reporter)
    try:
        summarizer = Summarizer()
        if payload.get("local"):
            from scripts import local_course
            from scripts.local_audio import LocalAudioDownloader
            from scripts.local_pipeline import LocalLectureRunner, StrictPPTClient
            from src.runtime.progress import emit

            scheduler.audio_downloader.shutdown()
            scheduler.audio_downloader = LocalAudioDownloader(
                Path(payload["db_path"]).parent / "media", local_course.write_private,
                reporter=reporter)
            client = StrictPPTClient(client, db, local_course)

            class ConsoleLectureRunner(LocalLectureRunner):
                def _summarize(self, sub_id, course_title, transcript, transcript_segments):
                    self._record_ppt_status(sub_id)
                    return LectureRunner._summarize(self, sub_id, course_title, transcript, transcript_segments)

            runner = ConsoleLectureRunner(client, db, scheduler, Transcriber(), reporter,
                checkpoint=lambda: emit("checkpoint", sub_id=str(payload["lecture"]["sub_id"])),
                runner=local_course, summary_worker=None)
            runner._summarizer = summarizer
        else:
            runner = LectureRunner(client, db, scheduler, Transcriber(), summarizer, reporter)
        # No cross-lecture workers: everything in this process belongs to
        # this lecture and can be safely stopped by its parent on timeout.
        runner.run(payload["course_id"], payload["course_title"], payload["lecture"])
        return 0
    except Exception as exc:
        print(f"[Lecture worker] {type(exc).__name__}", flush=True)
        sub_id = str(payload["lecture"]["sub_id"])
        if not (db.get_lecture(sub_id) or {}).get("error_stage"):
            db.update_error(sub_id, "process_timeout", f"课次处理失败（{type(exc).__name__}），稍后自动重试")
        return 1
    finally:
        scheduler.shutdown(wait=wait)
        if wait:
            db.conn.close()
            client.vpn.session.close()


def worker_main():
    # ThreadPoolExecutor's interpreter-exit hook waits for running native
    # OCR even after shutdown(wait=False). This process is disposable;
    # committed SQLite checkpoints are retained and the OS closes resources.
    code = 1
    try:
        code = run(json.loads(sys.stdin.readline()), wait=False)
    except Exception as exc:
        print(f"[Lecture worker] {type(exc).__name__}", flush=True)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)


if __name__ == "__main__":
    worker_main()
