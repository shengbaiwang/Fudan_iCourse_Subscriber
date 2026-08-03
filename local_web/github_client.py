from __future__ import annotations

import json
import base64
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import certifi
from nacl.public import PublicKey, SealedBox


API_ROOT = "https://api.github.com"


class GitHubAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class BlobEntry:
    name: str
    sha: str
    size: int


@dataclass(frozen=True)
class DataManifest:
    commit_sha: str
    index: BlobEntry
    shards: tuple[BlobEntry, ...]


class GitHubClient:
    def __init__(self, owner: str, repo: str, token: str, timeout: int = 30):
        self.owner = owner
        self.repo = repo
        self.token = token
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._actions_public_key: dict[str, str] | None = None

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> bytes:
        url = path if path.startswith("https://") else API_ROOT + path
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Fudan-iCourse-Subscriber-local-web/0.2",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=self.ssl_context,
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            raise GitHubAPIError(exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise GitHubAPIError(0, str(exc.reason)) from exc

    def _json(self, path: str, **kwargs: Any) -> Any:
        return json.loads(self._request(path, **kwargs).decode("utf-8"))

    @property
    def _repo_path(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"

    def authenticated_user(self) -> dict[str, str]:
        data = self._json("/user")
        return {"login": str(data.get("login") or ""), "name": str(data.get("name") or "")}

    def data_manifest(self, branch: str = "data") -> DataManifest:
        repo_path = self._repo_path
        encoded_branch = urllib.parse.quote(branch, safe="")
        ref = self._json(f"{repo_path}/git/ref/heads/{encoded_branch}")
        commit_sha = ref["object"]["sha"]
        commit = self._json(f"{repo_path}/git/commits/{commit_sha}")
        tree_sha = commit["tree"]["sha"]
        tree = self._json(f"{repo_path}/git/trees/{tree_sha}?recursive=1")["tree"]
        by_path = {item["path"]: item for item in tree if item.get("type") == "blob"}
        index_item = by_path.get("data/icourse-index.enc")
        if not index_item:
            raise GitHubAPIError(404, "data 分支中没有 data/icourse-index.enc")
        shards = tuple(
            BlobEntry(
                name=path.removeprefix("data/shards/"),
                sha=item["sha"],
                size=int(item.get("size") or 0),
            )
            for path, item in sorted(by_path.items())
            if path.startswith("data/shards/")
        )
        return DataManifest(
            commit_sha=commit_sha,
            index=BlobEntry(
                name="icourse-index.enc",
                sha=index_item["sha"],
                size=int(index_item.get("size") or 0),
            ),
            shards=shards,
        )

    def blob(self, sha: str) -> bytes:
        return self._request(
            f"/repos/{self.owner}/{self.repo}/git/blobs/{sha}",
            accept="application/vnd.github.raw",
        )

    def workflow_runs(self, per_page: int = 12) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"per_page": max(1, min(per_page, 50))})
        data = self._json(
            f"/repos/{self.owner}/{self.repo}/actions/runs?{query}"
        )
        return [
            {
                "id": run["id"],
                "name": run.get("name") or run.get("display_title") or "Workflow",
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at"),
                "html_url": run.get("html_url"),
                "event": run.get("event"),
            }
            for run in data.get("workflow_runs", [])
        ]

    def dispatch_workflow(
        self,
        workflow: str,
        *,
        ref: str = "main",
        inputs: dict[str, str] | None = None,
    ) -> None:
        safe_workflow = urllib.parse.quote(workflow, safe=".-_")
        self._request(
            f"/repos/{self.owner}/{self.repo}/actions/workflows/{safe_workflow}/dispatches",
            method="POST",
            payload={"ref": ref, "inputs": inputs or {}},
        )

    def repository_variable(self, name: str) -> str | None:
        encoded = urllib.parse.quote(name, safe="")
        try:
            data = self._json(f"{self._repo_path}/actions/variables/{encoded}")
        except GitHubAPIError as exc:
            if exc.status == 404:
                return None
            raise
        return str(data.get("value") or "")

    def upsert_repository_variable(self, name: str, value: str) -> None:
        encoded = urllib.parse.quote(name, safe="")
        current = self.repository_variable(name)
        if current is None:
            self._request(
                f"{self._repo_path}/actions/variables",
                method="POST",
                payload={"name": name, "value": value},
            )
        else:
            self._request(
                f"{self._repo_path}/actions/variables/{encoded}",
                method="PATCH",
                payload={"name": name, "value": value},
            )

    def repository_secret_names(self) -> set[str]:
        data = self._json(f"{self._repo_path}/actions/secrets?per_page=100")
        return {
            str(item.get("name") or "").upper()
            for item in data.get("secrets", [])
            if item.get("name")
        }

    def actions_public_key(self) -> dict[str, str]:
        if self._actions_public_key is None:
            data = self._json(f"{self._repo_path}/actions/secrets/public-key")
            self._actions_public_key = {
                "key": str(data["key"]),
                "key_id": str(data["key_id"]),
            }
        return self._actions_public_key

    def upsert_repository_secret(self, name: str, value: str) -> None:
        """Encrypt a value with GitHub's public key and create/update Secret."""
        public = self.actions_public_key()
        key = PublicKey(base64.b64decode(public["key"]))
        encrypted = SealedBox(key).encrypt(value.encode("utf-8"))
        encoded_name = urllib.parse.quote(name, safe="")
        self._request(
            f"{self._repo_path}/actions/secrets/{encoded_name}",
            method="PUT",
            payload={
                "encrypted_value": base64.b64encode(encrypted).decode("ascii"),
                "key_id": public["key_id"],
            },
        )
