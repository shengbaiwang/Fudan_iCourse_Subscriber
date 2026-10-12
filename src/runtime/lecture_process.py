"""Bound each lecture, including native workers and executor shutdown.

Only the child owns ASR/OCR pools. A stuck thread cannot prevent the parent
from saving a retry checkpoint and proceeding to the next lecture. Session
cookies travel through private stdin, never argv or a temporary file.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parents[2]


def lecture_timeout():
    value = float(os.environ.get("LECTURE_TIMEOUT_SECONDS", "2700"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("LECTURE_TIMEOUT_SECONDS 必须为正数")
    return value


def session_payload(client):
    client = getattr(client, "client", client)
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain,
                "path": c.path, "secure": c.secure, "expires": c.expires,
                "rest": c._rest} for c in client.vpn.session.cookies]
    return {"cookies": cookies, "headers": dict(client.vpn.session.headers)}


def _stop(process):
    # Always kill the group, even if its leader has already exited: ffmpeg
    # or a native helper may still be alive and holding media/SQLite files.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


def run_lecture_process(payload, timeout, *, command=None):
    if command is None:
        command = ([sys.executable, "--lecture-worker"] if getattr(sys, "frozen", False)
                   else [sys.executable, "-u", str(ROOT / "scripts/run_lecture_job.py")])
    process = subprocess.Popen(command, stdin=subprocess.PIPE, text=True,
                               start_new_session=True)
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.getsignal(signal.SIGTERM)

        def cancelled(signum, _frame):
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, cancelled)
    try:
        process.communicate(json.dumps(payload, ensure_ascii=False) + "\n", timeout=timeout)
        return process.returncode
    except subprocess.TimeoutExpired:
        raise TimeoutError("课次处理超过等待上限") from None
    finally:
        try:
            _stop(process)
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)


def drive_lectures(client, db, reporter, lectures, check_session, *, local=False):
    from src.runtime.progress import emit, note_saved

    timeout = lecture_timeout()
    emit("queue", total=len(lectures))
    completed = failed = 0
    for index, (course_id, course_title, lecture) in enumerate(lectures, 1):
        sub_id = str(lecture["sub_id"])
        emit("lecture", sub_id=sub_id, course_id=course_id, index=index,
             total=len(lectures), title=lecture.get("sub_title", ""))
        reporter.info(f"[Lecture {index}/{len(lectures)}] {course_title} / "
                      f"{lecture.get('sub_title', sub_id)}（上限 {timeout / 60:g} 分钟）")
        try:
            check_session(client)
            code = run_lecture_process({
                "session": session_payload(client), "db_path": str(Path(db.db_path).resolve()),
                "course_id": course_id, "course_title": course_title,
                "lecture": lecture, "local": local,
            }, timeout)
            if code and not (db.get_lecture(sub_id) or {}).get("summary"):
                row = db.get_lecture(sub_id) or {}
                if not row.get("error_stage"):
                    db.update_error(sub_id, "process_timeout", f"课次工作进程退出（{code}），稍后自动重试")
        except TimeoutError:
            if not (db.get_lecture(sub_id) or {}).get("summary"):
                db.update_error(sub_id, "process_timeout",
                                f"课次处理超过 {timeout / 60:g} 分钟，已终止本节并继续后续课次；稍后自动重试")
            reporter.info(f"    [TIMEOUT] {sub_id}：已停止本节工作进程，继续下一节")
        except Exception as exc:
            db.update_error(sub_id, "process_timeout", f"课次工作进程启动失败（{type(exc).__name__}），稍后自动重试")
            reporter.info(f"    [RETRY] {sub_id}：{type(exc).__name__}")
        row = db.get_lecture(sub_id) or {}
        if row.get("summary"):
            # Notes commit before optional title/cleanup work. A timeout in
            # those steps must not lose or hide an already generated note.
            db.mark_processed(sub_id)
            db.clear_error(sub_id)
            note_saved(db, sub_id)
            completed += 1
        else:
            emit("failed", sub_id=sub_id)
            failed += 1
    reporter.info(f"本次课次处理：生成 {completed} 节笔记，{failed} 节待重试或等待材料。")
    return completed, failed
