"""Independent, exact-model note comparisons using one immutable prompt."""
from __future__ import annotations

from copy import deepcopy
import threading
import uuid

from .talks import complete_talk, split_summary_output


class ComparisonJobs:
    def __init__(self):
        self._lock = threading.RLock()
        self._jobs = {}
        self._slots = threading.Semaphore(2)
        self._stopping = False

    def list(self, source, source_id):
        with self._lock:
            return deepcopy(self._jobs.get((source, source_id), []))

    def is_running(self, source, source_id):
        return any(row["status"] in {"queued", "running"} for row in self.list(source, source_id))

    def submit(self, source, source_id, providers, system, user, save):
        with self._lock:
            if self._stopping or self.is_running(source, source_id):
                raise ValueError("这篇笔记已有对比任务，请等待完成")
            rows = [{"id": uuid.uuid4().hex, "provider": p["name"], "model": p["models"][0],
                     "status": "queued", "error": ""} for p in providers]
            chosen = {(row["provider"], row["model"]) for row in rows}
            previous = self._jobs.get((source, source_id), [])
            self._jobs[(source, source_id)] = [row for row in previous
                                             if (row["provider"], row["model"]) not in chosen] + rows
            threading.Thread(target=self._run, args=(rows, providers, system, user, save),
                             name="note-compare", daemon=True).start()
            return deepcopy(rows)

    def _run(self, rows, providers, system, user, save):
        while not self._slots.acquire(timeout=0.2):
            if self._stopping:
                return
        try:
            self._run_models(rows, providers, system, user, save)
        finally:
            self._slots.release()

    def _run_models(self, rows, providers, system, user, save):
        # Each provider has exactly one selected model. Recovery may retry that
        # model, but can never silently substitute a different comparison entry.
        for row, provider in zip(rows, providers):
            with self._lock:
                if self._stopping:
                    row["status"] = "cancelled"
                    continue
                row["status"] = "running"
            try:
                raw, model = complete_talk([provider], system, user)
                title, body = split_summary_output(raw)
                with self._lock:
                    if self._stopping:
                        row["status"] = "cancelled"
                        continue
                    save(body, title, model)
                    row["status"] = "completed"
            except Exception as exc:
                with self._lock:
                    row["status"] = "failed"
                    row["error"] = str(exc).replace(provider["api_key"], "[已隐藏]")[:1500]

    def close(self):
        with self._lock:
            self._stopping = True
            for rows in self._jobs.values():
                for row in rows:
                    if row["status"] == "queued":
                        row["status"] = "cancelled"
