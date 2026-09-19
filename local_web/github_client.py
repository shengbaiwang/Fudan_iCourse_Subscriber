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
        self._default_timeout = timeout
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._actions_public_key: dict[str, str] | None = None

    def set_timeout(self, timeout: int) -> None:
        """Temporarily widen the socket timeout (e.g. big chunk PUTs)."""
        self.timeout = max(1, int(timeout))

    def reset_timeout(self) -> None:
        self.timeout = self._default_timeout

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
        except (TimeoutError, OSError) as exc:
            raise GitHubAPIError(0, str(exc)) from exc

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

    def default_branch(self) -> str:
        return str(self._json(self._repo_path)["default_branch"])

    def talk_workflow_run(self, talk_id: str, request_id: str) -> dict | None:
        title = f"talk-{talk_id}-{request_id}"
        # Exact run-name correlation avoids attributing another talk's failure.
        for page in range(1, 6):
            data = self._json(
                f"{self._repo_path}/actions/workflows/talk_transcribe.yml/runs"
                f"?event=workflow_dispatch&per_page=100&page={page}"
            )
            rows = data.get("workflow_runs", [])
            for run in rows:
                if run.get("display_title") == title:
                    return run
            if len(rows) < 100:
                break
        return None

    def workflow_file_on_ref(self, workflow: str, ref: str = "main") -> bool:
        """True when ``workflow`` exists on ``ref`` (i.e. dispatch can work).

        Dispatching a workflow that has never been merged to the target ref
        is GitHub's classic silent-404.  Callers use this to surface the
        real cause ("push main first") instead of a bare 404.
        """
        safe_workflow = urllib.parse.quote(workflow, safe=".-_")
        safe_ref = urllib.parse.quote(ref, safe="")
        try:
            self._request(
                f"{self._repo_path}/contents/.github/workflows/"
                f"{safe_workflow}?ref={safe_ref}"
            )
        except GitHubAPIError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def latest_workflow_run(
        self, workflow: str, *, branch: str = "",
    ) -> dict[str, Any] | None:
        """Newest run of ``workflow`` (optionally filtered by branch)."""
        safe_workflow = urllib.parse.quote(workflow, safe=".-_")
        query = urllib.parse.urlencode(
            {"per_page": 1, **({"branch": branch} if branch else {})}
        )
        data = self._json(
            f"{self._repo_path}/actions/workflows/"
            f"{safe_workflow}/runs?{query}"
        )
        runs = data.get("workflow_runs") or []
        return dict(runs[0]) if runs else None

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

    # ── Talk audio branch: small-file reads/writes via Contents API ──────
    # Talk recordings + transcripts are a handful of large blobs (up to
    # ~500 MB).  The git-database API used for shards would require building
    # trees/commits by hand; the Contents API handles create/update/delete
    # of one file per call and is enough here.

    def _contents_path(self, branch: str, path: str) -> str:
        safe_branch = urllib.parse.quote(branch, safe="")
        safe_path = "/".join(
            urllib.parse.quote(part, safe="") for part in path.split("/")
        )
        return f"{self._repo_path}/contents/{safe_path}?ref={safe_branch}"

    def get_branch_sha(self, branch: str) -> str | None:
        """Return the branch head SHA, or None when the branch is missing."""
        encoded = urllib.parse.quote(branch, safe="")
        try:
            ref = self._json(f"{self._repo_path}/git/ref/heads/{encoded}")
        except GitHubAPIError as exc:
            if exc.status == 404:
                return None
            raise
        return str(ref["object"]["sha"])

    def create_branch(self, branch: str, from_sha: str) -> None:
        self._request(
            f"{self._repo_path}/git/refs",
            method="POST",
            payload={"ref": f"refs/heads/{branch}", "sha": from_sha},
        )

    def ensure_branch(self, branch: str, default_branch: str = "main") -> str:
        """Return the branch head SHA, creating it from default when absent."""
        sha = self.get_branch_sha(branch)
        if sha is not None:
            return sha
        source = self.get_branch_sha(default_branch)
        if source is None:
            raise GitHubAPIError(404, f"默认分支 {default_branch} 不存在")
        try:
            self.create_branch(branch, source)
        except GitHubAPIError as exc:
            # Lost a creation race with another client — re-read the head.
            if exc.status != 422:
                raise
            sha = self.get_branch_sha(branch)
            if sha is None:
                raise
            return sha
        return source

    def read_branch_file(self, branch: str, path: str) -> bytes | None:
        """Return raw file bytes, or None when branch/path is missing."""
        try:
            data = self._json(self._contents_path(branch, path))
        except GitHubAPIError as exc:
            if exc.status == 404:
                return None
            raise
        if isinstance(data, list) or data.get("type") != "file":
            return None
        content = str(data.get("content") or "")
        if data.get("encoding") != "base64":
            return self._request(self._contents_path(branch, path),
                                 accept="application/vnd.github.raw+json")
        return base64.b64decode(content)

    def write_branch_file(
        self, branch: str, path: str, data: bytes, message: str
    ) -> None:
        """Create or replace one file on a branch (≤ ~100 MB per call)."""
        try:
            current = self._json(self._contents_path(branch, path))
            sha = current.get("sha") if isinstance(current, dict) else None
        except GitHubAPIError as exc:
            if exc.status != 404:
                raise
            sha = None
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(data).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        encoded_path = "/".join(
            urllib.parse.quote(part, safe="") for part in path.split("/")
        )
        self._request(
            f"{self._repo_path}/contents/{encoded_path}",
            method="PUT",
            payload=payload,
        )

    def delete_branch_file(
        self, branch: str, path: str, message: str
    ) -> bool:
        """Delete one file; False when it was already absent."""
        try:
            current = self._json(self._contents_path(branch, path))
        except GitHubAPIError as exc:
            if exc.status == 404:
                return False
            raise
        if not isinstance(current, dict) or not current.get("sha"):
            return False
        encoded_path = "/".join(
            urllib.parse.quote(part, safe="") for part in path.split("/")
        )
        self._request(
            f"{self._repo_path}/contents/{encoded_path}",
            method="DELETE",
            payload={
                "message": message,
                "sha": current["sha"],
                "branch": branch,
            },
        )
        return True

    def delete_branch_dir(self, branch: str, directory: str,
                          message: str) -> int:
        """Delete every file under ``directory``; returns the count removed.

        The Contents API has no recursive delete, so list the tree once and
        delete file-by-file.  Used to clean up a talk's chunked upload dir
        (≤ ~25 chunks + manifest); failures on individual files are ignored.
        """
        prefix = directory.strip("/").rstrip("/") + "/"
        try:
            tree = self.branch_tree(branch)
        except GitHubAPIError as exc:
            if exc.status == 404:
                return 0
            raise
        removed = 0
        for path in sorted(tree):
            if not path.startswith(prefix):
                continue
            try:
                if self.delete_branch_file(branch, path, message):
                    removed += 1
            except GitHubAPIError:
                continue
        return removed

    def branch_tree(self, branch: str) -> dict[str, str]:
        """Map blob path → sha for every file on ``branch`` (recursive)."""
        encoded = urllib.parse.quote(branch, safe="")
        ref = self._json(f"{self._repo_path}/git/ref/heads/{encoded}")
        commit = self._json(
            f"{self._repo_path}/git/commits/{ref['object']['sha']}"
        )
        tree = self._json(
            f"{self._repo_path}/git/trees/{commit['tree']['sha']}?recursive=1"
        )["tree"]
        return {
            str(item["path"]): str(item["sha"])
            for item in tree
            if item.get("type") == "blob" and item.get("path")
        }
