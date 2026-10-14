"""An unsubscribed course can be deleted without local notes resurrecting it."""
import sqlite3
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from local_web.database import DatabaseManager
from local_web.github_client import BlobEntry, DataManifest, GitHubAPIError
from local_web.notebook import Notebook
from local_web.server import create_app
from local_web.state import RuntimeCredentials, RuntimeState, SettingsStore
from local_web.talks import TalkStore
from src.data.crypto_box import decrypt, derive_new_password
from src.data.database import Database
from src.data.sharder import shard_database


CREDS = RuntimeCredentials("fixture-token", "student", "password")
PASSWORD = derive_new_password(CREDS.stuid, CREDS.uispsw)


class NoCredentials:
    available = False

    def load(self, settings):
        return None


def populate(path):
    db = Database(str(path))
    for cid in ("1", "2", "empty"):
        db.upsert_course(cid, f"课程 {cid}", "老师")
    for sid, cid in (("10", "1"), ("11", "1"), ("20", "2")):
        db.insert_lecture(sid, cid, f"课次 {sid}", "2026-10-01")
        db.update_transcript(sid, "材料")
        db.update_summary(sid, f"笔记 {sid}", "test/model")
        with db.conn:
            db.conn.execute("INSERT INTO ppt_pages(sub_id,page_num,created_sec,text) VALUES (?,1,0,'课件')", (sid,))
            db.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, '{}')", (f"local:{sid}:audio",))
    with db.conn:
        db.conn.execute("INSERT INTO all_courses(course_id,term,title) VALUES ('1','2026-秋','目录课程')")
        db.conn.execute("INSERT OR REPLACE INTO meta VALUES ('subscribed_course_ids','2')")
    db.conn.close()


def sync_remote(manager, path, root, commit):
    output = root / commit
    index = shard_database(str(path), str(output), PASSWORD)
    blobs = {"index": (output / "icourse-index.enc").read_bytes()}
    blobs.update({item["name"]: (output / "shards" / item["name"]).read_bytes() for item in index["shards"]})
    client = Mock()
    client.data_manifest.return_value = DataManifest(commit, BlobEntry("index", f"index:{commit}", 0),
        tuple(BlobEntry(item["name"], f"{item['name']}:{commit}", 0) for item in index["shards"]))
    client.blob.side_effect = lambda sha: blobs[sha.split(":")[0]]
    manager.sync(client, "data", CREDS)


@pytest.fixture
def library(tmp_path):
    manager = DatabaseManager(tmp_path / "cache")
    populate(manager.db_path)
    source = tmp_path / "source.db"
    populate(source)
    manager.accept_run_note(source, "10", CREDS)
    manager.accept_run_note(source, "20", CREDS)
    try:
        yield manager, source
    finally:
        manager.close()


def test_unsubscribed_course_is_removed_from_library_overlay_restart_and_stale_sync(library, tmp_path):
    manager, source = library
    manager.delete_course_data(["1", "empty"], [], CREDS)
    assert [row["course_id"] for row in manager.courses()] == ["2"]
    assert manager.lecture("10") is None
    assert manager.lecture("20")["summary"] == "笔记 20"
    # Removing collected material preserves the catalog and subscription.
    assert manager.subscription_ids() == ["2"]
    assert manager.subscription_catalog()["total"] == 1
    with sqlite3.connect(manager.db_path) as conn:
        for table in ("ppt_pages", "summary_versions"):
            assert not conn.execute(f"SELECT 1 FROM {table} WHERE sub_id IN ('10','11')").fetchone()
        assert not conn.execute("SELECT 1 FROM meta WHERE key='local:10:audio'").fetchone()
    overlay = tmp_path / "overlay.db"
    overlay.write_bytes(decrypt(manager.overlay_path.read_bytes(), PASSWORD))
    with sqlite3.connect(overlay) as conn:
        assert conn.execute("SELECT course_id FROM courses").fetchall() == [("2",)]
    # A late result from the old job and a changed, stale cloud commit cannot undo deletion.
    manager.accept_run_note(source, "10", CREDS)
    sync_remote(manager, source, tmp_path, "stale")
    assert manager.lecture("10") is None
    restored = DatabaseManager(tmp_path / "cache")
    try:
        assert restored.unlock_persistent(CREDS)
        assert [row["course_id"] for row in restored.courses()] == ["2"]
        sync_remote(restored, source, tmp_path, "stale-after-restart")
        assert restored.lecture("10") is None
    finally:
        restored.close()
    # Once the cloud confirms removal, a later intentional run can re-add the course.
    DatabaseManager._delete_course_data(source, [("1", ""), ("empty", "")])
    sync_remote(manager, source, tmp_path, "deleted")
    assert manager._pending_deletions(manager.db_path) == []
    new_source = tmp_path / "fresh.db"
    populate(new_source)
    manager.accept_run_note(new_source, "10", CREDS)
    assert manager.lecture("10")["summary"] == "笔记 10"


def test_lecture_deletion_is_scoped_and_last_lecture_removes_course(library, tmp_path):
    manager, source = library
    manager.delete_course_data(["1"], ["10", "20"], CREDS)
    assert manager.lecture("10") is None
    assert manager.lecture("11") is not None
    assert manager.lecture("20") is not None
    sync_remote(manager, source, tmp_path, "stale-lecture")
    assert manager.lecture("10") is None
    assert manager.lecture("11") is not None
    manager.delete_course_data(["1"], ["11"], CREDS)
    assert "1" not in {row["course_id"] for row in manager.courses()}


def test_dispatch_deletes_local_only_after_remote_accepts_and_without_subscription(library, tmp_path):
    manager, _ = library
    state = RuntimeState(store=SettingsStore(tmp_path / "config"), credential_store=NoCredentials())
    state.subscription_ids = ["2"]
    app = create_app(state=state, database=manager, talk_store=TalkStore(tmp_path / "talks"),
                     notebook=Notebook(tmp_path / "notes"))
    state.credentials = CREDS
    gh = Mock()
    gh.repository_variable.return_value = "{}"
    route = "/api/local/workflows/delete_course.yml/dispatch"
    payload = {"ref": "main", "inputs": {"course_ids": "1", "sub_ids": ""}}
    with patch("local_web.server.GitHubClient", return_value=gh), TestClient(app) as client:
        gh.dispatch_workflow.side_effect = GitHubAPIError(403, "fixture rejection")
        assert client.post(route, json=payload).status_code == 502
        assert manager.lecture("10") is not None
        gh.dispatch_workflow.side_effect = None
        app.state.local_runs.jobs["busy"] = {"id": "busy", "status": "in_progress", "course_ids": ["1"]}
        assert client.post(route, json=payload).status_code == 409
        assert manager.lecture("10") is not None
        app.state.local_runs.jobs.clear()
        assert client.post(route, json={"inputs": {"course_ids": ""}}).status_code == 400
        result = client.post(route, json=payload)
        assert result.status_code == 200
        assert result.json()["local_deleted"]
        assert client.get("/api/local/courses").json()[0]["course_id"] != "1"
        assert manager.lecture("10") is None
        assert state.subscription_ids == ["2"]


def test_removing_last_subscription_stays_empty_after_restart(library, tmp_path):
    manager, _ = library
    config = tmp_path / "config"
    state = RuntimeState(store=SettingsStore(config), credential_store=NoCredentials())
    gh = Mock()
    gh.repository_variable.return_value = "{}"
    with patch("local_web.server.GitHubClient", return_value=gh):
        app = create_app(state=state, database=manager, talk_store=TalkStore(tmp_path / "talks"),
                         notebook=Notebook(tmp_path / "notes"))
        state.credentials = CREDS
        with TestClient(app) as client:
            assert client.get("/api/local/subscriptions").json()["course_ids"] == ["2"]
            result = client.put("/api/local/subscriptions", json={"course_ids": []})
            assert result.status_code == 200
            assert result.json()["course_ids"] == []
        restored = RuntimeState(store=SettingsStore(config), credential_store=NoCredentials())
        app = create_app(state=restored, database=manager, talk_store=TalkStore(tmp_path / "talks"),
                         notebook=Notebook(tmp_path / "notes"))
        restored.credentials = CREDS
        with TestClient(app) as client:
            result = client.get("/api/local/subscriptions").json()
            assert result["course_ids"] == []
            assert result["source"] == "local-save"
