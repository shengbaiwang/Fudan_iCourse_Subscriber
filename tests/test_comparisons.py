from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from local_web.database import DatabaseManager
from local_web.server import create_app
from local_web.state import RuntimeCredentials, RuntimeState, SettingsStore
from local_web.talks import TalkStore, TalkUploadQueue, TalkLLMError
from src.data.database import Database


class NoKeychain:
    available = False
    def load(self, _settings): return None


PROVIDER = {"name": "test", "base_url": "https://example.test/v1", "api_key_env": "LLM_TEST_API_KEY",
            "enabled": True, "models": ["first", "new", "failing"]}


def wait_for(check):
    end = time.monotonic() + 5
    while not check():
        assert time.monotonic() < end, "background job did not finish"
        time.sleep(.01)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SEA_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("ICOURSE_WEB_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_TEST_API_KEY", "synthetic-key")
    state = RuntimeState(store=SettingsStore(tmp_path), credential_store=NoKeychain())
    manager = DatabaseManager(tmp_path / "cache")
    course = Database(str(manager.db_path))
    course.upsert_course("c", "课程", "教师")
    course.insert_lecture("l", "c", "课次", "2026-10-27")
    course.update_transcript("l", "同一份转写材料")
    course.update_summary("l", "旧笔记", "test/old")
    course.conn.close()
    store = TalkStore(tmp_path / "talks")
    class GitHub:
        def repository_variable(self, _name):
            return json.dumps({"version": 1, "providers": [PROVIDER]})
    with patch("local_web.server.GitHubClient", return_value=GitHub()):
        app = create_app(state=state, database=manager, talk_store=store)
        state.credentials = RuntimeCredentials("token", "student", "password")
        client = TestClient(app)
        yield client, app, manager, store, state
        app.state.comparisons.close()
        app.state.talk_uploads.close()
        manager.close()


def test_selected_models_share_material_keep_history_and_do_not_fall_back(env):
    client, app, manager, store, state = env
    item = store.create_talk("讲座", "同一份转写材料")
    store.save_summary(item["id"], "已有版本", "旧标题", "test/old")
    url = f"/api/local/comparisons/talk/{item['id']}"
    calls = []
    def chat(base, model, key, system, user, **kwargs):
        calls.append((model, system, user))
        if model == "failing":
            raise TalkLLMError("synthetic-key quota")
        return "# 标题\n\n新模型笔记"
    def once(provider, model, call, **kwargs): return call({}), {}
    with patch("local_web.talks.chat_complete", side_effect=chat), patch("local_web.talks.complete_with_recovery", side_effect=once):
        response = client.post(url, json={"models":[{"provider":"test", "model":"failing"}, {"provider":"test", "model":"new"}]})
        assert response.status_code == 200
        wait_for(lambda: not app.state.comparisons.is_running("talk", item["id"]))
        jobs = client.get(url).json()["jobs"]
        assert [row["status"] for row in jobs] == ["failed", "completed"]
        assert "synthetic-key" not in json.dumps(jobs)
        assert [call[0] for call in calls] == ["failing", "new"]
        assert calls[0][1:] == calls[1][1:]
        assert {v["model"] for v in store.get_talk(item["id"])["versions"]} == {"test/old", "test/new"}
        assert client.post(url, json={"models":[{"provider":"test", "model":"failing"}]}).status_code == 200
        wait_for(lambda: not app.state.comparisons.is_running("talk", item["id"]))
        assert len(client.get(url).json()["jobs"]) == 2
        assert len(store.get_talk(item["id"])["versions"]) == 2


def test_course_comparison_survives_encrypted_library_restart(env):
    client, app, manager, store, state = env
    with patch("local_web.talks.chat_complete", return_value="# 新标题\n\n本轮笔记"):
        assert client.post("/api/local/comparisons/lecture/l", json={"models":[{"provider":"test", "model":"new"}]}).status_code == 200
        wait_for(lambda: not app.state.comparisons.is_running("lecture", "l"))
    jobs = client.get("/api/local/comparisons/lecture/l").json()["jobs"]
    assert jobs[0]["status"] == "completed", jobs
    assert manager.lecture("l")["summary"] == "本轮笔记"
    restored = DatabaseManager(manager.cache_dir)
    try:
        assert restored.unlock_persistent(state.credentials)
        assert {v["model"] for v in restored.lecture("l")["summary_versions"]} == {"test/old", "test/new"}
        assert "本轮笔记".encode() not in manager.overlay_path.read_bytes()
    finally:
        restored.close()


def test_reject_unknown_or_keyless_selection_before_any_job_starts(env, monkeypatch):
    client, app, manager, store, state = env
    url = "/api/local/comparisons/lecture/l"
    assert client.post(url, json={"models":[{"provider":"test", "model":"unadded"}]}).status_code == 400
    monkeypatch.delenv("LLM_TEST_API_KEY")
    with patch("local_web.server.LocalAPIKeychain.load", return_value=None):
        assert client.post(url, json={"models":[{"provider":"test", "model":"new"}]}).status_code == 400
        assert client.get(url).json()["jobs"] == []


def test_recording_intake_receipt_is_idempotent_and_visible_without_upload_dialog(env):
    client, app, manager, store, state = env
    with patch.object(app.state.talk_uploads, "_pump_locked"):
        data = {"title":"", "upload_token":"same-selection-retry"}
        files = {"file":("录音文件.mp3", b"ID3synthetic audio", "audio/mpeg")}
        first = client.post("/api/local/talks/upload", data=data, files=files)
        second = client.post("/api/local/talks/upload", data=data, files=files)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    rows = client.get("/api/local/talks").json()
    assert len(rows) == 1
    assert rows[0]["display_title"] == "录音文件"
    assert rows[0]["queue_state"] == "queued"
    assert rows[0]["audio_bytes"] > 0
    assert client.post(f"/api/local/talks/{rows[0]['id']}/pause-upload").status_code == 200
    assert client.get("/api/local/talks").json()[0]["status"] == "paused"


class MemoryCloud:
    def __init__(self): self.files = {}; self.dispatches = []
    def default_branch(self): return "main"
    def workflow_file_on_ref(self, *args): return True
    def ensure_branch(self, *args, **kwargs): return "sha"
    def read_branch_file(self, branch, path): return self.files.get(path)
    def write_branch_file(self, branch, path, data, message): self.files[path] = data
    def set_timeout(self, value): pass
    def reset_timeout(self): pass
    def dispatch_workflow(self, name, **kwargs): self.dispatches.append(kwargs)


def test_overlapping_intake_retries_do_not_replace_staged_ciphertext(tmp_path):
    store = TalkStore(tmp_path / "library")
    queue = TalkUploadQueue(store, MemoryCloud)
    barrier = threading.Barrier(2)
    def intake(blob):
        barrier.wait(timeout=3)
        return queue.enqueue_blob(blob, "mp3", "录音", 10, "same-receipt")
    with patch.object(queue, "_pump_locked"), patch.object(queue, "stage_blob", wraps=queue.stage_blob) as stage:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(intake, [b"ciphertext-one", b"ciphertext-two"]))
    assert results[0]["id"] == results[1]["id"]
    assert stage.call_count == 1
    assert len(store.list_talks()) == 1
    assert store.get_talk(results[0]["id"])["upload_attempts"] == 1
    queue.close()


def test_pause_during_chunk_upload_preserves_receipt_and_resume_does_not_repeat_it(tmp_path):
    store = TalkStore(tmp_path / "library")
    cloud = MemoryCloud()
    queue = TalkUploadQueue(store, lambda: cloud)
    talk_id = store.create_audio_talk("录音", "mp3", 0, "")["id"]
    queue.stage_blob(talk_id, b"encrypted recording", "mp3", "录音")
    original = cloud.write_branch_file
    def write(*args, **kwargs):
        original(*args, **kwargs)
        if args[1].endswith("part.enc"):
            assert queue.pause(talk_id)
    cloud.write_branch_file = write
    queue._run(talk_id)
    assert store.get_talk(talk_id)["status"] == "paused"
    assert store.upload_progress(talk_id)["done"] == 1
    assert not cloud.dispatches
    assert queue.resume_pending() == 0
    cloud.write_branch_file = original
    assert queue.submit(talk_id)
    wait_for(lambda: not queue.is_active(talk_id))
    assert store.get_talk(talk_id)["status"] == "transcribing"
    assert len(cloud.dispatches) == 1
    assert queue.staged_blob(talk_id)
    queue.close()


def test_batch_queue_limits_cloud_work_and_restores_pending_order(tmp_path):
    store = TalkStore(tmp_path / "library")
    cloud = MemoryCloud()
    ids = [store.create_audio_talk(str(i), "mp3", 0, "")["id"] for i in range(4)]
    store.set_status(ids[0], "transcribing")
    store.set_status(ids[1], "summarizing")
    queue = TalkUploadQueue(store, lambda: cloud)
    for talk_id in ids[2:]: queue.stage_blob(talk_id, b"audio", "mp3", "录音")
    assert queue.resume_pending() == 2
    assert queue.describe(ids[2])["queue_position"] == 1
    assert queue.describe(ids[3])["queue_position"] == 2
    queue.close()
    restarted = TalkUploadQueue(store, lambda: cloud)
    assert restarted.resume_pending() == 2
    assert restarted.describe(ids[2])["queue_position"] == 1
    assert restarted.pause(ids[2])
    store.set_status(ids[0], "ready")
    restarted.resume_pending()
    wait_for(lambda: not restarted.is_active(ids[3]))
    assert len(cloud.dispatches) == 1
    assert store.get_talk(ids[2])["status"] == "paused"
    restarted.close()


def test_restart_resumes_reserved_dispatches_before_waiting_uploads(tmp_path):
    store = TalkStore(tmp_path / "library")
    cloud = MemoryCloud()
    queue = TalkUploadQueue(store, lambda: cloud)
    ids = [store.create_audio_talk(str(i), "mp3", 0, "")["id"] for i in range(3)]
    for talk_id in ids:
        queue.stage_blob(talk_id, b"audio", "mp3", "录音")
    requests = {talk_id: store.begin_cloud_request(talk_id) for talk_id in ids[1:]}
    assert queue.resume_pending() == 3
    wait_for(lambda: len(cloud.dispatches) == 2 and not any(queue.is_active(i) for i in ids[1:]))
    assert {dispatch["inputs"]["request_id"] for dispatch in cloud.dispatches} == set(requests.values())
    assert queue.describe(ids[0])["queue_state"] == "queued"
    store.set_status(ids[1], "ready")
    queue.resume_pending()
    wait_for(lambda: not queue.is_active(ids[0]))
    assert len(cloud.dispatches) == 3
    queue.close()
