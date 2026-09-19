"""Reconcile encrypted cloud checkpoints even when no browser tab is open."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone

from .talks import TALK_AUDIO_BRANCH, decrypt_talk_blob, talk_transcript_remote_path


class TalkCloudSync:
    def __init__(self, store, uploads, make_client, credentials):
        self.store, self.uploads = store, uploads
        self.make_client, self.credentials = make_client, credentials
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def sync(self, talk_id):
        with self._lock:
            item = self.store.get_talk(talk_id)
            if not item:
                raise ValueError("讲座不存在")
            if item["source"] != "upload" or item["status"] == "ready":
                return item
            credentials = self.credentials()
            if not credentials:
                return item
            gh = self.make_client()
            blob = gh.read_branch_file(TALK_AUDIO_BRANCH, talk_transcript_remote_path(talk_id))
            if blob:
                payload = json.loads(decrypt_talk_blob(blob, credentials.stuid, credentials.uispsw))
                if not isinstance(payload, dict) or payload.get("talk_id") != talk_id:
                    raise ValueError("云端结果与讲座 ID 不匹配")
                # An old failed attempt must not cancel a new retry.
                if payload.get("request_id", "") == item.get("cloud_request_id", ""):
                    status = payload.get("status", "")
                    result_hash = hashlib.sha256(blob).hexdigest()
                    if item.get("cloud_result_hash") != result_hash:
                        self.store.save_cloud_result(
                            talk_id, payload.get("transcript", ""), payload.get("summary", ""),
                            payload.get("ai_title", ""), payload.get("summary_model", ""),
                            result_hash=result_hash, status=status,
                            error=str(payload.get("error") or ""),
                            error_stage=str(payload.get("error_stage") or ""))
                    item = self.store.get_talk(talk_id)
                    if item["status"] == "ready":
                        self.uploads.drop_staging(talk_id)
                        return item
                    if status == "failed":
                        return item
            request_id = item.get("cloud_request_id")
            if request_id:
                run = gh.talk_workflow_run(talk_id, request_id)
                if run and run.get("status") == "completed":
                    updated = run.get("updated_at")
                    if updated and (datetime.now(timezone.utc) - datetime.fromisoformat(updated.replace("Z", "+00:00"))).total_seconds() < 60:
                        return item
                    # Includes model download, missing secrets, cancellation and
                    # timeout failures before the script can write a checkpoint.
                    self.store.set_status(
                        talk_id, "failed",
                        f"云端任务已结束（{run.get('conclusion') or 'unknown'}），尚无完整笔记。"
                        "已完成转写会保留，点击继续处理可重试。",
                        "summarize" if item.get("transcript") else "transcribe")
                else:
                    stamp = item.get("cloud_requested_at")
                    if stamp and (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() > 6 * 3600:
                        self.store.set_status(talk_id, "failed", "云端处理超时，请检查任务后继续处理", "transcribe")
            elif item["status"] == "transcribing":
                self.store.set_status(talk_id, "failed", "旧任务无法确认处理进度，点击继续处理可恢复", "transcribe")
            return self.store.get_talk(talk_id)

    def start(self):
        self.store.recover_interrupted()
        def worker():
            while not self._stop.is_set():
                if self.credentials():
                    self.uploads.resume_pending()
                    for item in self.store.list_talks():
                        if self._stop.is_set():
                            return
                        if item["source"] != "upload" or item["status"] not in {"transcribing", "summarizing", "failed"}:
                            continue
                        if item["status"] == "failed" and item.get("error_stage") != "dispatch":
                            continue
                        try:
                            self.sync(item["id"])
                        except Exception:
                            # Transient network/auth failures do not destroy a
                            # running job; explicit refresh exposes the error.
                            continue
                self._stop.wait(15)
        self._thread = threading.Thread(target=worker, name="talk-cloud-sync", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self.uploads.close()
