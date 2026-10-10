from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from local_web.database import DatabaseManager
from local_web.github_client import BlobEntry, DataManifest, GitHubAPIError
from local_web.runs import LocalRuns, LocalRunBusy
from local_web.server import AutoCheckRequest, RunPreferencesRequest, RunRequest, create_app
from local_web.state import RuntimeCredentials, RuntimeState, SettingsStore
from local_web.talks import TalkStore
from scripts.publish_progress import publish_database, snapshot_database
from src.data.crypto_box import decrypt, derive_new_password
from src.data.database import Database
from src.data.sharder import shard_database
from src.runtime.run_order import ordered_ids, ordered_lectures


CREDS = RuntimeCredentials("token", "student", "password")
PASSWORD = derive_new_password(CREDS.stuid, CREDS.uispsw)
PROVIDER = {"name": "test", "base_url": "https://example.test/v1", "api_key_env": "LLM_TEST_API_KEY",
            "enabled": True, "models": ["model"]}


def fixture(path, summary="old", timestamp="2026-09-01", extra=False):
    db = Database(str(path))
    db.upsert_course("1", "课程一", "老师")
    db.upsert_course("2", "课程二", "老师")
    db.insert_lecture("10", "1", "第一课", "2026-09-01")
    db.insert_lecture("11", "1", "第二课", "2026-09-02")
    db.insert_lecture("20", "2", "第三课", "2026-09-03")
    for sid in ("10", "11", "20"):
        db.update_transcript(sid, "转录材料")
    db.update_summary("10", summary, "test/model")
    with db.conn:
        db.conn.execute("UPDATE lectures SET processed_at=? WHERE sub_id='10'", (timestamp,))
    if extra:
        db.update_summary("20", "unrelated cloud", "test/model")
    db.conn.close()


class EmptyKeychain:
    available = False
    def load(self, _settings): return None


class FakeGitHub:
    def __init__(self): self.dispatches = []; self.secrets = {}; self.variables = {}
    def repository_variable(self, name):
        return json.dumps({"version": 1, "providers": [PROVIDER]}) if name == "MODEL_PROVIDERS_JSON" else self.variables.get(name)
    def repository_secret_names(self): return {"LLM_TEST_API_KEY"}
    def dispatch_workflow(self, workflow, **kwargs): self.dispatches.append((workflow, kwargs))
    def upsert_repository_secret(self, name, value): self.secrets[name] = value
    def upsert_repository_variable(self, name, value): self.variables[name] = value


class RunEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        self.db = DatabaseManager(path / "cache")
        fixture(self.db.db_path)
        self.state = RuntimeState(store=SettingsStore(path), credential_store=EmptyKeychain())
        self.app = create_app(state=self.state, database=self.db, talk_store=TalkStore(path / "talks"))
        self.state.credentials = CREDS
        self.state.subscription_ids = ["1", "2"]
        self.gh = FakeGitHub()
        self.mock = patch("local_web.server.GitHubClient", return_value=self.gh)
        self.mock.start()

    def tearDown(self):
        self.app.state.local_runs.close(); self.mock.stop(); self.db.close(); self.temp.cleanup()

    def call(self, path, method, payload):
        endpoint = next(r.endpoint for r in self.app.routes if getattr(r, "path", None) == path and method in r.methods)
        return asyncio.run(endpoint(payload))

    def test_cloud_course_and_lecture_priority_reach_workflow(self):
        self.call("/api/local/runs", "POST", RunRequest(kind="rerun", course_ids=["1", "2"], priority_course_ids=["2", "1"], lecture_order="newest", provider="test", model="model"))
        workflow, request = self.gh.dispatches[-1]
        self.assertEqual(workflow, "single_run.yml")
        self.assertEqual(request["inputs"]["course_ids"], "2,1")
        self.assertEqual(request["inputs"]["resummarize_sub_ids"], "20,11,10")
        self.assertEqual(request["inputs"]["lecture_order"], "newest")

    def test_titles_are_scoped_to_selected_courses(self):
        self.call("/api/local/runs", "POST", RunRequest(kind="titles", course_ids=["2"], provider="test", model="model"))
        self.assertEqual(self.gh.dispatches[-1][1]["inputs"]["backfill_titles"], "true")
        self.assertEqual(self.gh.dispatches[-1][1]["inputs"]["course_ids"], "2")

    def test_daily_reorder_preserves_subscriptions(self):
        result = self.call("/api/local/run-preferences", "PUT", RunPreferencesRequest(course_ids=["2", "1"], lecture_order="oldest"))
        self.assertEqual(result["course_ids"], ["2", "1"])
        self.assertEqual(self.gh.secrets["COURSE_IDS"], "2,1")
        self.assertEqual(self.gh.variables["LECTURE_ORDER"], "oldest")
        with self.assertRaises(HTTPException) as exc:
            self.call("/api/local/run-preferences", "PUT", RunPreferencesRequest(course_ids=["2"]))
        self.assertEqual(exc.exception.status_code, 409)

    def test_pause_resume_and_reorder_preserve_notes_and_subscriptions(self):
        path = "/api/local/subscriptions/auto-check"
        paused = self.call(path, "PUT", AutoCheckRequest(course_id="1", paused=True))
        self.assertEqual(paused["course_ids"], ["1", "2"])
        self.assertEqual(paused["paused_course_ids"], ["1"])
        self.assertTrue(paused["courses"][0]["pause_scan_pending"])
        self.assertEqual(paused["courses"][0]["pending_count"], 1)
        first_token = json.loads(self.gh.variables["COURSE_AUTO_CHECK_JSON"])["1"]
        self.assertEqual(self.db.lecture("10")["summary"], "old")
        self.assertNotIn("COURSE_IDS", self.gh.secrets)
        self.call("/api/local/run-preferences", "PUT", RunPreferencesRequest(course_ids=["2", "1"]))
        self.assertEqual(self.state.subscription_store.load_auto_check_pauses(), {"1": first_token})
        resumed = self.call(path, "PUT", AutoCheckRequest(course_id="1", paused=False))
        self.assertEqual(resumed["paused_course_ids"], [])
        self.call(path, "PUT", AutoCheckRequest(course_id="1", paused=True))
        self.assertNotEqual(json.loads(self.gh.variables["COURSE_AUTO_CHECK_JSON"])["1"], first_token)

    def test_pause_rejects_unsubscribed_course_and_failed_remote_save(self):
        path = "/api/local/subscriptions/auto-check"
        with self.assertRaises(HTTPException) as exc:
            self.call(path, "PUT", AutoCheckRequest(course_id="missing", paused=True))
        self.assertEqual(exc.exception.status_code, 409)
        with patch.object(self.gh, "upsert_repository_variable", side_effect=GitHubAPIError(403, "synthetic")):
            with self.assertRaises(HTTPException) as exc:
                self.call(path, "PUT", AutoCheckRequest(course_id="1", paused=True))
        self.assertEqual(exc.exception.status_code, 502)
        self.assertEqual(self.state.auto_check_pauses, {})

    def test_local_key_and_order_are_sent_privately_to_worker(self):
        runs = self.app.state.local_runs
        with patch.object(runs, "capabilities", return_value={"process_ready": True, "summary_ready": True}), patch.object(runs, "start", return_value={"id": "job"}) as start:
            result = self.call("/api/local/runs", "POST", RunRequest(target="local", course_ids=["1", "2"], priority_course_ids=["2", "1"], provider="test", model="model", api_key="private-key"))
        self.assertEqual(result["target"], "local")
        self.assertNotIn("private-key", json.dumps(result))
        self.assertEqual(start.call_args.args[0]["course_ids"], ["2", "1"])
        self.assertEqual(start.call_args.args[3], {"LLM_TEST_API_KEY": "private-key"})

    def test_empty_or_invalid_queue_cannot_dispatch(self):
        for ids in ([], ["bad,id"]):
            with self.assertRaises(HTTPException):
                self.call("/api/local/runs", "POST", RunRequest(course_ids=ids, provider="test", model="model"))
        self.assertEqual(self.gh.dispatches, [])


class LocalNotePersistenceTest(unittest.TestCase):
    def test_latest_note_is_chosen_by_time_not_timezone_string(self):
        from scripts.merge_db import merge
        with tempfile.TemporaryDirectory() as directory:
            source, remote = Path(directory) / "local.db", Path(directory) / "remote.db"
            fixture(source, "older local", "2026-10-09T12:00:00+08:00")
            fixture(remote, "newer cloud", "2026-10-09T06:00:00+00:00")
            merge(str(source), str(remote))
            with sqlite3.connect(remote) as conn:
                self.assertEqual(conn.execute("SELECT summary FROM lectures WHERE sub_id='10'").fetchone()[0], "newer cloud")

    def test_note_survives_sync_restart_and_newer_cloud_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); manager = DatabaseManager(path / "cache")
            source = path / "source.db"; remote = path / "remote.db"
            try:
                fixture(manager.db_path)
                fixture(source, "local live", "2026-09-03")
                with sqlite3.connect(source) as conn:
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('local:10:audio','{}')")
                manager.accept_run_note(source, "10", CREDS)
                self.assertEqual(manager.lecture("10")["summary"], "local live")
                self.assertEqual(manager.revision, 1)
                self.assertNotIn(b"local live", manager.overlay_path.read_bytes())
                fixture(remote, "older cloud", "2026-09-02", extra=True)
                def synchronize(commit):
                    output = path / commit
                    index = shard_database(str(remote), str(output), PASSWORD)
                    blobs = {"index": (output / "icourse-index.enc").read_bytes()}
                    blobs.update({row["name"]: (output / "shards" / row["name"]).read_bytes() for row in index["shards"]})
                    class Client:
                        def data_manifest(self, _branch): return DataManifest(commit, BlobEntry("index", "index" + commit, 0), tuple(BlobEntry(row["name"], row["name"] + commit, 0) for row in index["shards"]))
                        def blob(self, sha): return blobs[sha.removesuffix(commit)]
                    manager.sync(Client(), "data", CREDS)
                synchronize("commit1")
                self.assertEqual(manager.lecture("10")["summary"], "local live")
                self.assertEqual(manager.lecture("20")["summary"], "unrelated cloud")
                with sqlite3.connect(manager.db_path) as conn:
                    self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='local:10:audio'").fetchone()[0], "{}")
                restored = DatabaseManager(path / "cache")
                try:
                    self.assertTrue(restored.unlock_persistent(CREDS))
                    self.assertEqual(restored.lecture("10")["summary"], "local live")
                finally: restored.close()
                fixture(remote, "newest cloud", "2026-09-04", extra=True)
                synchronize("commit2")
                self.assertEqual(manager.lecture("10")["summary"], "newest cloud")
                self.assertGreaterEqual(len(manager.lecture("10")["summary_versions"]), 3)
            finally: manager.close()

    def test_real_child_protocol_imports_live_results_and_cancels_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "scripts").mkdir()
            # A real subprocess exercises stdin, WAL visibility, progress,
            # redaction and cancellation without contacting a model or school.
            (root / "scripts/run_console_job.py").write_text('''
import json,sqlite3,sys,time
p=json.loads(sys.stdin.readline())
print(p['api_keys']['LLM_TEST_API_KEY'],flush=True)
print('ICOURSE_EVENT '+json.dumps({'kind':'queue','total':2}),flush=True)
with sqlite3.connect(p['db_path']) as db:
    db.execute("UPDATE lectures SET summary='child note', processed_at='2026-10-01' WHERE sub_id='10'")
print('ICOURSE_EVENT '+json.dumps({'kind':'note','sub_id':'10'}),flush=True)
time.sleep(30)
''', encoding="utf-8")
            manager = DatabaseManager(root / "cache"); fixture(manager.db_path)
            runs = LocalRuns(manager)
            request = {"kind":"rerun", "course_ids":["1"], "sub_ids":["10"], "lecture_order":"newest", "provider":"test", "model":"model"}
            try:
                with patch("local_web.runs.ROOT", root):
                    job = runs.start(request, CREDS, {"version":1,"providers":[PROVIDER]}, {"LLM_TEST_API_KEY":"secret-for-child"})
                    deadline = time.monotonic() + 10
                    while runs.list()[0]["completed"] != 1 and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertEqual(manager.lecture("10")["summary"], "child note")
                    self.assertEqual(runs.list()[0]["status"], "in_progress")
                    self.assertNotIn("secret-for-child", json.dumps(runs.list()))
                    with self.assertRaises(LocalRunBusy): runs.start(request, CREDS, {}, {})
                    runs.cancel(job["id"]); runs.close()
                    self.assertEqual(runs.list()[0]["status"], "cancelled")
                    self.assertEqual(list(manager._temp_dir.glob("run-*")), [])
                    self.assertEqual(runs.processes, {})
            finally: runs.close(); manager.close()


class PublisherTest(unittest.TestCase):
    def test_atomic_encrypted_publish_retries_conflict_and_keeps_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.db"; fixture(path)
            class Client:
                _repo_path = "/repos/test/repo"
                def __init__(self): self.head = None; self.blobs = {}; self.trees = {}; self.commits = {}; self.conflicts = 1; self.forces = []
                def get_branch_sha(self, _branch): return self.head
                def blob(self, sha): return self.blobs[sha]
                def _json(self, url, method="GET", payload=None):
                    if method == "GET":
                        if "/commits/" in url: return self.commits[url.rsplit("/", 1)[-1]]
                        return {"tree": list(self.trees[url.rsplit("/", 1)[-1].split("?")[0]].values())}
                    if url.endswith("/blobs"):
                        raw = base64.b64decode(payload["content"]); sha = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest(); self.blobs[sha] = raw; return {"sha": sha}
                    if url.endswith("/trees"):
                        tree = dict(self.trees.get(payload.get("base_tree"), {}))
                        tree.update({row["path"]: row for row in payload["tree"]})
                        sha = "tree" + str(len(self.trees)); self.trees[sha] = tree; return {"sha": sha}
                    if url.endswith("/commits"):
                        sha = "commit" + str(len(self.commits)); self.commits[sha] = {"tree": {"sha": payload["tree"]}}; return {"sha": sha}
                    if self.conflicts:
                        self.conflicts -= 1
                        raise GitHubAPIError(422, "concurrent update")
                    self.forces.append(payload.get("force", False)); self.head = payload["sha"]; return {}
            client = Client()
            publish_database(str(path), client=client, password=PASSWORD)
            first = client.head
            entries = client.trees[client.commits[first]["tree"]["sha"]]
            index = json.loads(decrypt(client.blob(entries["data/icourse-index.enc"]["sha"]), PASSWORD))
            self.assertTrue(index["shards"])
            self.assertFalse(any(client.forces))
            entries["README.md"] = {"path": "README.md", "sha": "readme", "type": "blob"}
            client.blobs["readme"] = b"preserve"
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE lectures SET summary='changed',processed_at='2026-10-01' WHERE sub_id='10'")
            publish_database(str(path), client=client, password=PASSWORD)
            self.assertIn("README.md", client.trees[client.commits[client.head]["tree"]["sha"]])

    def test_snapshot_includes_uncheckpointed_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source.db", Path(directory) / "target.db"
            db = Database(str(source))
            try:
                db.upsert_course("1", "WAL result", "teacher")
                snapshot_database(source, target)
                with sqlite3.connect(target) as conn:
                    self.assertEqual(conn.execute("SELECT title FROM courses").fetchone()[0], "WAL result")
            finally: db.conn.close()


class OrderingTest(unittest.TestCase):
    def test_order_is_stable_and_validated(self):
        rows = [{"sub_id": "a", "date": "2026-09-01"}, {"sub_id": "b", "date": "2026-09-02"}, {"sub_id": "c", "date": "2026-09-02"}]
        self.assertEqual([r["sub_id"] for r in ordered_lectures(rows, "newest")], ["b", "c", "a"])
        self.assertEqual(ordered_ids([" 2", "1", "2"]), ["2", "1"])
        with self.assertRaises(ValueError): ordered_ids(["a,b"])


if __name__ == "__main__": unittest.main()
