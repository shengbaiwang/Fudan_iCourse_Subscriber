"""Workflow inspection and explicit dispatch keep GitHub review in GitHub."""
import json
from unittest.mock import Mock, call, patch

from fastapi.testclient import TestClient

from local_web.database import DatabaseManager
from local_web.github_client import GitHubClient
from local_web.notebook import Notebook
from local_web.server import create_app
from local_web.state import RuntimeCredentials, RuntimeState, SettingsStore
from local_web.talks import TalkStore


class NoCredentials:
    available = False

    def load(self, settings):
        return None


def test_blocked_runs_stay_visible_and_only_explicit_dispatch_writes(tmp_path):
    state = RuntimeState(store=SettingsStore(tmp_path), credential_store=NoCredentials())
    database = DatabaseManager(tmp_path / "cache")
    app = create_app(state=state, database=database, talk_store=TalkStore(tmp_path / "talks"),
                     notebook=Notebook(tmp_path))
    state.credentials = RuntimeCredentials("fixture-token", "student", "password")
    github = GitHubClient(state.settings.owner, state.settings.repo, state.credentials.token)
    blocked = {"id": 42, "name": "iCourse Check", "status": "completed",
               "conclusion": "action_required", "event": "schedule",
               "created_at": "2026-10-11T00:00:00Z",
               "html_url": "https://github.com/owner/repo/actions/runs/42"}
    github._request = Mock(return_value=json.dumps({"workflow_runs": [blocked]}).encode())
    try:
        with patch("local_web.server.GitHubClient", return_value=github), TestClient(app) as client:
            response = client.post("/api/local/workflow-approvals")
            assert response.status_code in (404, 405)
            github._request.assert_not_called()

            response = client.get("/api/local/workflows")
            assert response.status_code == 200
            assert response.json()[0]["conclusion"] == "action_required"
            assert response.json()[0]["html_url"] == blocked["html_url"]

            response = client.post("/api/local/workflows/single_run.yml/dispatch",
                                   json={"ref": "main", "inputs": {"course_ids": "1"}})
            assert response.status_code == 200
            assert github._request.call_args_list == [
                call(f"{github._repo_path}/actions/runs?per_page=12"),
                call(f"{github._repo_path}/actions/workflows/single_run.yml/dispatches",
                     method="POST", payload={"ref": "main", "inputs": {"course_ids": "1"}}),
            ]
    finally:
        database.close()
