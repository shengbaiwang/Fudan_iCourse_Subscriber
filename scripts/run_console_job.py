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
        "PUBLISH_NOTE_PROGRESS": "0",
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
    from src.runtime.lecture_process import drive_lectures
    # The console already has credentials; local login uses the native HTTPS
    # adapter required by the macOS WebVPN endpoint.
    creds = RuntimeCredentials("", credentials["stuid"], credentials["uispsw"])
    client = local_course.login(creds)
    from src.data.database import Database
    from src.runtime.reporter import Reporter
    db = Database()
    reporter = Reporter()
    def check_session(value):
        if not value.check_alive():
            refreshed = local_course.login(creds)
            value.vpn.session.close()
            value.vpn = refreshed.vpn
            value._userinfo = None
    try:
        lectures = main._enumerate_lectures(client, db, reporter, force_video_recheck=True,
                                           session_check=check_session)
        drive_lectures(client, db, reporter, lectures, check_session, local=True)
    finally:
        db.conn.close()
        client.vpn.session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run(json.loads(sys.stdin.readline())))
