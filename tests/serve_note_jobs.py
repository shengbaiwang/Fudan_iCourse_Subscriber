"""Isolated synthetic API for browser_note_jobs.cjs; never uses real accounts."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_web.database import DatabaseManager
from local_web.github_client import GitHubAPIError
from local_web.server import create_app
from local_web.state import RuntimeCredentials, RuntimeState, SettingsStore
from local_web.talks import TalkStore, TalkLLMError
from src.data.database import Database
import uvicorn


class NoKeychain:
    available = False
    def load(self, _settings): return None


class Cloud:
    def __init__(self): self.files = {}
    def repository_variable(self, name):
        if name == "MODEL_PROVIDERS_JSON":
            return json.dumps({"version": 1, "providers": [{"name":"fixture", "base_url":"https://example.test/v1",
                "models":["alpha", "beta"], "api_key_env":"LLM_FIXTURE_API_KEY", "enabled":True}]})
        return ""
    def repository_secret_names(self): return {"LLM_FIXTURE_API_KEY"}
    def default_branch(self): return "main"
    def workflow_file_on_ref(self, *args): return True
    def ensure_branch(self, *args, **kwargs): return "sha"
    def read_branch_file(self, branch, path): return self.files.get(path)
    def write_branch_file(self, branch, path, data, message):
        time.sleep(.2)
        self.files[path] = data
    def set_timeout(self, value): pass
    def reset_timeout(self): pass
    def dispatch_workflow(self, name, **kwargs): pass
    def talk_workflow_run(self, *args): return {"status":"in_progress"}
    def data_manifest(self, branch): raise GitHubAPIError(404, "synthetic offline library")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="sea-note-jobs-") as directory:
        root = Path(directory)
        os.environ.update(SEA_DISABLE_KEYCHAIN="1", ICOURSE_WEB_CONFIG_DIR=directory,
                          ICOURSE_WEB_CACHE_DIR=str(root / "cache"), LLM_FIXTURE_API_KEY="synthetic-key")
        os.environ.pop("SEA_DESKTOP_TOKEN", None)
        manager = DatabaseManager(root / "cache")
        course = Database(str(manager.db_path))
        course.upsert_course("course", "测试课程", "教师")
        course.insert_lecture("lecture", "course", "测试课次", "2026-10-27")
        course.update_transcript("lecture", "同一份转写材料")
        course.update_summary("lecture", "历史笔记", "fixture/old")
        course.conn.close()
        state = RuntimeState(store=SettingsStore(root), credential_store=NoKeychain())
        cloud = Cloud()
        calls = {}
        def chat(base, model, key, system, user, **kwargs):
            time.sleep(.7)
            calls[model] = calls.get(model, 0) + 1
            if model == "beta" and calls[model] == 1:
                raise TalkLLMError("合成的暂时失败")
            return "# 测试笔记\n\n以相同材料生成的正文。"
        def once(provider, model, call, **kwargs): return call({}), {}
        with patch("local_web.server.GitHubClient", return_value=cloud), patch("local_web.talks.chat_complete", side_effect=chat), patch("local_web.talks.complete_with_recovery", side_effect=once):
            app = create_app(state=state, database=manager, talk_store=TalkStore(root / "library"))
            state.credentials = RuntimeCredentials("synthetic-token", "student", "password")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            print(f"READY http://127.0.0.1:{port}", flush=True)
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
