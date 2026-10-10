"""Start a reusable local preparation job detached from this terminal."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import local_course as runner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course-config", type=Path)
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--provider", help="可选：在后台任务中直接调用本机已配置的摘要 API")
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit 必须至少为 1")
    runner.configure_course(args.course_config)
    os.umask(0o077)
    runner.RUN_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(runner.RUN_DIR, 0o700)
    with open(runner.RUN_DIR / "launch.lock", "a") as launch_lock:
        fcntl.flock(launch_lock, fcntl.LOCK_EX)
        with open(runner.RUN_DIR / "job.lock", "a") as job_lock:
            try:
                fcntl.flock(job_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("这门课已有本机任务运行") from None
        marker = runner.RUN_DIR / "background-job.json"
        if marker.exists():
            previous = json.loads(marker.read_text())
            started = datetime.fromisoformat(previous["started_at"])
            # job.lock is authoritative once preparation starts. Check a
            # recent launcher PID only during the brief pre-start window.
            if time.time() - started.timestamp() < 120:
                try:
                    os.kill(int(previous["pid"]), 0)
                except ProcessLookupError:
                    pass
                else:
                    raise RuntimeError("已有后台任务，使用 status 查看进度")
        command = ["/usr/bin/nohup", str(ROOT / "scripts/run_local_course.command"),
                   "prepare", "--limit", str(args.limit)]
        if args.course_config:
            command += ["--course-config", str(args.course_config.resolve())]
        if args.provider:
            command += ["--provider", args.provider]
        if args.model:
            command += ["--model", args.model]
        log = runner.RUN_DIR / "run.log"
        with open(log, "ab", buffering=0) as output:
            os.chmod(log, 0o600)
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        runner.write_private(marker, json.dumps({"pid": process.pid, "course_id": runner.COURSE_ID,
                             "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "log": str(log)}).encode())
        print(f"本机后台任务已启动：PID {process.pid}\n日志：{log}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(runner.safe_error(exc), file=sys.stderr)
        raise SystemExit(1)
