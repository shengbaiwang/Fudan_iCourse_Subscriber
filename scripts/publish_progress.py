"""Publish an encrypted, atomic data-branch checkpoint with optimistic retries.

Newer note timestamps win during merging and all historical versions are
retained. Git refs are never force-updated here.
"""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_web.github_client import GitHubAPIError, GitHubClient
from src.data.crypto_box import decrypt, derive_new_password
from src.data.sharder import reassemble_database, shard_database
from scripts.merge_db import merge


def snapshot_database(source: str | Path, target: str | Path) -> None:
    with closing(sqlite3.connect(f"file:{Path(source).resolve()}?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(str(target))) as dest:
            src.backup(dest)


def publish_database(db_path: str, *, client=None, password=None, branch="data") -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if client is None:
        owner, repo = repository.split("/", 1)
        client = GitHubClient(owner, repo, os.environ["GITHUB_TOKEN"])
    password = password or derive_new_password(
        os.environ.get("STUID") or os.environ["StuId"],
        os.environ.get("UISPSW") or os.environ["UISPsw"],
    )
    repo_path = client._repo_path
    with tempfile.TemporaryDirectory(prefix="icourse-progress-") as directory:
        directory = Path(directory)
        local = directory / "local.db"
        snapshot_database(db_path, local)
        for attempt in range(3):
            head = client.get_branch_sha(branch)
            merged = directory / "merged.db"
            merged.unlink(missing_ok=True)
            if head:
                # Read one immutable commit throughout reassembly.
                commit = client._json(f"{repo_path}/git/commits/{head}")
                tree = client._json(f"{repo_path}/git/trees/{commit['tree']['sha']}?recursive=1")
                entries = {row["path"]: row["sha"] for row in tree["tree"] if row["type"] == "blob"}
                if "data/icourse-index.enc" in entries:
                    index = json.loads(decrypt(client.blob(entries["data/icourse-index.enc"]), password))
                    shards = directory / "remote-shards"
                    shards.mkdir(exist_ok=True)
                    for shard in index["shards"]:
                        name = shard["name"]
                        (shards / name).write_bytes(client.blob(entries[f"data/shards/{name}"]))
                    reassemble_database(index, str(shards), str(merged), password)
                    merge(str(local), str(merged))
                else:
                    snapshot_database(local, merged)
            else:
                entries = {}
                snapshot_database(local, merged)
            output = directory / "output"
            output.mkdir(exist_ok=True)
            index = shard_database(str(merged), str(output), password)
            paths = ["icourse-index.enc"] + [f"shards/{row['name']}" for row in index["shards"]]
            changes = []
            for path in paths:
                raw = (output / path).read_bytes()
                sha = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
                remote_path = f"data/{path}"
                if entries.get(remote_path) == sha:
                    continue
                blob = client._json(f"{repo_path}/git/blobs", method="POST", payload={
                    "content": base64.b64encode(raw).decode(), "encoding": "base64"})
                changes.append({"path": remote_path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            if not changes:
                return head or ""
            tree_payload = {"tree": changes}
            if head:
                tree_payload["base_tree"] = commit["tree"]["sha"]
            new_tree = client._json(f"{repo_path}/git/trees", method="POST", payload=tree_payload)
            new_commit = client._json(f"{repo_path}/git/commits", method="POST", payload={
                "message": "chore: publish note progress (encrypted)",
                "tree": new_tree["sha"], "parents": [head] if head else []})
            try:
                if head:
                    client._json(f"{repo_path}/git/refs/heads/{branch}", method="PATCH",
                                 payload={"sha": new_commit["sha"], "force": False})
                else:
                    client._json(f"{repo_path}/git/refs", method="POST",
                                 payload={"ref": f"refs/heads/{branch}", "sha": new_commit["sha"]})
                print("[Progress] 新笔记已发布。", flush=True)
                return new_commit["sha"]
            except GitHubAPIError as exc:
                if exc.status not in (409, 422) or attempt == 2:
                    raise
        raise RuntimeError("数据分支持续更新，请稍后重试")


if __name__ == "__main__":
    publish_database(sys.argv[1] if len(sys.argv) > 1 else "data/icourse.db")
