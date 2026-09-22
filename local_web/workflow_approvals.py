"""Approve recent owner-triggered Single Run jobs through GitHub's approval API."""
from __future__ import annotations

from datetime import datetime
import threading
import time

from .github_client import GitHubAPIError

WORKFLOWS = {".github/workflows/single_run.yml"}


def eligible(run, owner, repo, now):
    try:
        age = now - datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError):
        return False
    return (
        type(run.get("id")) is int and run["id"] > 0
        and run.get("status") == "completed"
        and run.get("conclusion") == "action_required"
        and run.get("run_attempt") == 1
        and run.get("event") == "workflow_dispatch"
        and run.get("head_branch") == "main"
        and run.get("path") in WORKFLOWS
        and run.get("pull_requests") == []
        and (run.get("repository") or {}).get("full_name", "").lower() == f"{owner}/{repo}".lower()
        and (run.get("head_repository") or {}).get("full_name", "").lower() == f"{owner}/{repo}".lower()
        and all((run.get(field) or {}).get("login", "").lower() == owner.lower()
                for field in ("actor", "triggering_actor"))
        and 0 <= age <= 86400
    )


def reconcile(client, can_approve=lambda: True, now=None):
    """Never rerun jobs; only approve a first attempt that has not started."""
    now = time.time() if now is None else now
    base = client._repo_path
    rows = client._json(f"{base}/actions/runs?branch=main&per_page=100")["workflow_runs"]
    pending = [r for r in rows if eligible(r, client.owner, client.repo, now)]
    result = {"approved": [], "errors": []}
    if not pending:
        return result
    if client.authenticated_user()["login"].lower() != client.owner.lower():
        return result
    for row in pending:
        try:
            # Re-read both the run and main just before approval. A list response
            # may be stale after manual approval, cancellation or a branch update.
            run = client._json(f"{base}/actions/runs/{row['id']}")
            if not eligible(run, client.owner, client.repo, now) or run["id"] != row["id"]:
                continue
            head = client._json(f"{base}/git/ref/heads/main")["object"]["sha"]
            if not head or run.get("head_sha") != head or not can_approve():
                continue
            client._request(f"{base}/actions/runs/{run['id']}/approve", method="POST")
            result["approved"].append(run["id"])
        except GitHubAPIError as exc:
            result["errors"].append({"id": row["id"], "message": str(exc)})
    return result


class WorkflowApprovals:
    """One worker per local console, independent of whether a browser is open."""
    def __init__(self, make_client, identity):
        self.make_client, self.identity = make_client, identity
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._scope = None
        self._next_check = 0
        self._result = {"approved": [], "errors": []}

    def check(self):
        with self._lock:
            scope = self.identity()
            if scope != self._scope:
                self._scope, self._next_check = scope, 0
                self._result = {"approved": [], "errors": []}
            if not scope or time.monotonic() < self._next_check:
                return self._result
            try:
                self._result = reconcile(self.make_client(), lambda: self.identity() == scope)
            except Exception as exc:
                # A logout race or malformed API response must not kill the
                # background worker; retain the error for the console to show.
                self._result = {"approved": [], "errors": [{"message": str(exc)}]}
            self._next_check = time.monotonic() + (300 if self._result["errors"] else 30)
            return self._result

    def start(self):
        def worker():
            while not self._stop.is_set():
                self.check()
                self._stop.wait(30)
        self._thread = threading.Thread(target=worker, name="workflow-approvals", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
