"""Local course jobs run in isolated processes, with per-note live imports."""
from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import uuid

from src.runtime.progress import PREFIX

ROOT = Path(__file__).resolve().parents[1]


class LocalRunBusy(ValueError):
    pass


class LocalRuns:
    def __init__(self, database):
        self.database = database
        self.lock = threading.RLock()
        self.jobs = {}
        self.processes = {}
        self.threads = {}

    @staticmethod
    def python() -> str:
        value = ROOT / (".venv-course/Scripts/python.exe" if os.name == "nt" else ".venv-course/bin/python")
        return str(value) if value.is_file() else sys.executable

    def capabilities(self):
        result = subprocess.run([self.python(), "-c",
            "import importlib.util,json; print(json.dumps([n for n in ['openai','sherpa_onnx','rapidocr_onnxruntime','psutil','imagehash','numpy','PIL','requests','curl_cffi','imageio_ffmpeg'] if importlib.util.find_spec(n) is None]))"],
            capture_output=True, text=True, timeout=10)
        missing = json.loads(result.stdout) if result.returncode == 0 else ["Python 运行环境"]
        models = (ROOT / "silero_vad.onnx").is_file() and (ROOT / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17").is_dir()
        return {"local": True, "process_ready": not missing and models,
                "summary_ready": result.returncode == 0 and not set(missing).intersection({"openai", "imagehash", "PIL", "numpy"}), "missing": missing,
                "models_ready": models}

    def list(self):
        with self.lock:
            return deepcopy(list(self.jobs.values())[::-1][:20])

    def start(self, request, credentials, document, api_keys):
        with self.lock:
            if any(row["status"] in {"queued", "in_progress"} for row in self.jobs.values()):
                raise LocalRunBusy("已有本地任务运行，请等待结束或先停止该任务")
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"id": job_id, "target": "local", "kind": request["kind"],
                "status": "queued", "course_ids": request["course_ids"], "total": 0,
                "completed": 0, "failed": 0, "current": "准备运行", "logs": [],
                "created_at": datetime.now(timezone.utc).isoformat()}
            payload = {**request, "credentials": {"stuid": credentials.stuid, "uispsw": credentials.uispsw},
                       "providers": document, "api_keys": api_keys}
            thread = threading.Thread(target=self._run, args=(job_id, payload, credentials),
                                      name=f"icourse-run-{job_id[:8]}", daemon=True)
            self.threads[job_id] = thread
            thread.start()
            return deepcopy(self.jobs[job_id])

    def _run(self, job_id, payload, credentials):
        job = self.jobs[job_id]
        secrets = [credentials.stuid, credentials.uispsw, *payload["api_keys"].values()]
        try:
            with tempfile.TemporaryDirectory(prefix="run-", dir=self.database._temp_dir) as directory, ExitStack() as cleanup:
                os.chmod(directory, 0o700)
                source = Path(directory) / "icourse.db"
                with self.database._lock:
                    if self.database.db_path.is_file():
                        from scripts.publish_progress import snapshot_database
                        snapshot_database(self.database.db_path, source)
                payload["db_path"] = str(source)
                with self.lock:
                    if job["status"] == "cancelled":
                        return
                    process = subprocess.Popen([self.python(), "-u", str(ROOT / "scripts/run_console_job.py")],
                        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, start_new_session=True)
                    self.processes[job_id] = process
                    cleanup.callback(self._terminate, process)
                    job["status"] = "in_progress"
                process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.close()
                for line in process.stdout:
                    if line.startswith(PREFIX):
                        event = json.loads(line[len(PREFIX):])
                        kind = event["kind"]
                        if kind in {"note", "failed", "checkpoint"} and source.is_file():
                            self.database.accept_run_note(source, str(event["sub_id"]), credentials)
                        with self.lock:
                            if kind == "queue":
                                job["total"] = event["total"]
                            elif kind == "lecture":
                                job["current"] = event.get("title") or event["sub_id"]
                            elif kind == "note":
                                job["completed"] += 1
                            elif kind in {"failed", "course_failed"}:
                                job["failed"] += 1
                        continue
                    clean = line.strip()
                    for secret in secrets:
                        if secret:
                            clean = clean.replace(secret, "[已隐藏]")
                    clean = re.sub(r"https?://\S+", "[URL]", clean)
                    clean = re.sub(r"(?i)(authorization|cookie):.*", r"\1: [已隐藏]", clean)
                    if clean:
                        with self.lock:
                            job["logs"] = (job["logs"] + [clean[:500]])[-40:]
                code = process.wait()
                with self.lock:
                    if job["status"] != "cancelled":
                        job["status"] = "completed" if code == 0 and not job["failed"] else "failed"
                        job["current"] = "运行结束"
        except Exception as exc:
            with self.lock:
                if job["status"] != "cancelled":
                    job["status"] = "failed"
                    job["current"] = f"本地运行失败（{type(exc).__name__}）"
        finally:
            with self.lock:
                process = self.processes.pop(job_id, None)
                if process and process.poll() is None:
                    self._terminate(process)
                job["finished_at"] = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _terminate(process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)

    def cancel(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError("本地任务不存在")
            job = self.jobs[job_id]
            if job["status"] not in {"queued", "in_progress"}:
                return deepcopy(job)
            job["status"] = "cancelled"
            job["current"] = "已停止，已生成的笔记保留"
            process = self.processes.get(job_id)
        if process:
            self._terminate(process)
        return deepcopy(job)

    def close(self):
        for job in self.list():
            if job["status"] in {"queued", "in_progress"}:
                self.cancel(job["id"])
        for thread in list(self.threads.values()):
            thread.join(timeout=5)
