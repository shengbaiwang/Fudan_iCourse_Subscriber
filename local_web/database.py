from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

from src.data.crypto_box import decrypt, derive_new_password, encrypt, is_json_obj, is_sqlite
from src.data.sharder import reassemble_database

from .github_client import DataManifest, GitHubClient
from .state import RuntimeCredentials, default_cache_dir, default_config_dir


class DatabaseNotReady(RuntimeError):
    pass


class DatabaseManager:
    """Maintain an encrypted local library with a process-private SQLite copy.

    The encrypted shard cache and an encrypted reassembled database persist
    between launches.  Plaintext SQLite is materialized only under a private
    temporary directory for the running process, then removed on close.
    """

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or default_cache_dir()
        self.blob_dir = self.cache_dir / "encrypted-blobs"
        # macOS may clear Library/Caches.  The user-facing encrypted library
        # belongs under application support; custom/test cache directories
        # keep all artifacts together for easy isolation.
        self.library_dir = (
            (cache_dir / "library") if cache_dir is not None else default_config_dir() / "library"
        )
        self.persistent_db_path = self.library_dir / "icourse.db.enc"
        self.persistent_state_path = self.library_dir / "icourse.db-state.json"
        self._temp_dir = Path(tempfile.mkdtemp(prefix="icourse-local-web-"))
        try:
            os.chmod(self._temp_dir, 0o700)
        except OSError:
            pass
        self.db_path = self._temp_dir / "icourse.db"
        self.commit_sha: str | None = None
        self._lock = threading.RLock()

    def close(self) -> None:
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def unlock_persistent(self, credentials: RuntimeCredentials) -> bool:
        """Restore the last encrypted local library for immediate browsing."""
        with self._lock:
            if not self.persistent_db_path.is_file():
                return False
            password = derive_new_password(credentials.stuid, credentials.uispsw)
            plaintext = decrypt(self.persistent_db_path.read_bytes(), password)
            if not is_sqlite(plaintext):
                raise ValueError("本地加密资料库无法解锁，请重新同步")
            next_db = self._temp_dir / "icourse.restore.db"
            next_db.write_bytes(plaintext)
            try:
                os.chmod(next_db, 0o600)
            except OSError:
                pass
            next_db.replace(self.db_path)
            try:
                state = json.loads(self.persistent_state_path.read_text("utf-8"))
                self.commit_sha = str(state.get("commit_sha") or "") or None
            except (OSError, ValueError, TypeError):
                self.commit_sha = None
            return True

    def _persist_encrypted(self, credentials: RuntimeCredentials) -> None:
        if not self.db_path.is_file():
            raise DatabaseNotReady("没有可持久化的数据库")
        self.library_dir.mkdir(parents=True, exist_ok=True)
        password = derive_new_password(credentials.stuid, credentials.uispsw)
        encrypted = encrypt(self.db_path.read_bytes(), password)
        temporary = self.persistent_db_path.with_suffix(".next")
        temporary.write_bytes(encrypted)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.persistent_db_path)
        state_temp = self.persistent_state_path.with_suffix(".tmp")
        state_temp.write_text(
            json.dumps({"commit_sha": self.commit_sha or ""}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(state_temp, 0o600)
        except OSError:
            pass
        state_temp.replace(self.persistent_state_path)

    def _cached_blob(self, client: GitHubClient, sha: str) -> tuple[Path, bool]:
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        path = self.blob_dir / sha
        if path.is_file():
            return path, False
        data = client.blob(sha)
        temp = path.with_suffix(".tmp")
        temp.write_bytes(data)
        temp.replace(path)
        return path, True

    def sync(
        self,
        client: GitHubClient,
        branch: str,
        credentials: RuntimeCredentials,
    ) -> dict[str, Any]:
        with self._lock:
            manifest = client.data_manifest(branch)
            if manifest.commit_sha == self.commit_sha and self.db_path.is_file():
                return {**self.stats(), "downloaded_blobs": 0, "unchanged": True}

            password = derive_new_password(credentials.stuid, credentials.uispsw)
            index_path, index_downloaded = self._cached_blob(
                client, manifest.index.sha
            )
            index_plain = decrypt(index_path.read_bytes(), password)
            if not is_json_obj(index_plain):
                raise ValueError("索引解密结果不是 JSON，请检查学号和 UIS 密码")
            index = json.loads(index_plain.decode("utf-8"))
            self._validate_manifest(index, manifest)

            shard_dir = self._temp_dir / "shards"
            shard_dir.mkdir(exist_ok=True)
            downloaded = int(index_downloaded)
            manifest_by_name = {entry.name: entry for entry in manifest.shards}
            for shard in index.get("shards", []):
                name = str(shard["name"])
                entry = manifest_by_name[name]
                cached, fetched = self._cached_blob(client, entry.sha)
                downloaded += int(fetched)
                target = shard_dir / name
                if target.exists():
                    target.unlink()
                try:
                    os.link(cached, target)
                except OSError:
                    shutil.copyfile(cached, target)

            next_db = self._temp_dir / "icourse.next.db"
            reassemble_database(index, str(shard_dir), str(next_db), password)
            next_db.replace(self.db_path)
            self.commit_sha = manifest.commit_sha
            self._persist_encrypted(credentials)
            return {
                **self.stats(),
                "downloaded_blobs": downloaded,
                "unchanged": False,
            }

    @staticmethod
    def _validate_manifest(index: dict, manifest: DataManifest) -> None:
        expected = {str(item["name"]) for item in index.get("shards", [])}
        available = {item.name for item in manifest.shards}
        missing = sorted(expected - available)
        if missing:
            raise ValueError("数据分支缺少分片：" + ", ".join(missing[:5]))

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise DatabaseNotReady("数据库尚未同步")
        uri = f"file:{self.db_path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def stats(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            courses = db.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
            lectures = db.execute("SELECT COUNT(*) FROM lectures").fetchone()[0]
            ready = db.execute(
                "SELECT COUNT(*) FROM lectures WHERE summary IS NOT NULL"
            ).fetchone()[0]
            failed = db.execute(
                "SELECT COUNT(*) FROM lectures WHERE error_stage IS NOT NULL"
            ).fetchone()[0]
        return {
            "commit_sha": self.commit_sha,
            "courses": courses,
            "lectures": lectures,
            "ready": ready,
            "failed": failed,
        }

    def courses(self) -> list[dict[str, Any]]:
        sql = """
            SELECT c.course_id, c.title, c.teacher,
                   COUNT(l.sub_id) AS total_count,
                   SUM(CASE WHEN l.summary IS NOT NULL THEN 1 ELSE 0 END) AS summary_count,
                   MAX(l.processed_at) AS last_updated
            FROM courses c
            LEFT JOIN lectures l ON l.course_id = c.course_id
            GROUP BY c.course_id, c.title, c.teacher
            ORDER BY last_updated DESC
        """
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(sql)]

    def lectures(self, course_id: str) -> list[dict[str, Any]]:
        sql = """
            SELECT sub_id, course_id, sub_title, date, processed_at,
                   error_stage, error_msg, summary_model,
                   CASE WHEN summary IS NOT NULL THEN 1 ELSE 0 END AS has_summary
            FROM lectures WHERE course_id = ?
            ORDER BY COALESCE(date, ''), sub_title
        """
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(sql, (course_id,))]

    def lecture(self, sub_id: str) -> dict[str, Any] | None:
        sql = """
            SELECT l.*, c.title AS course_title, c.teacher
            FROM lectures l JOIN courses c ON c.course_id = l.course_id
            WHERE l.sub_id = ?
        """
        with closing(self._connect()) as db:
            row = db.execute(sql, (sub_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["ppt_pages"] = [
                dict(page)
                for page in db.execute(
                    """SELECT page_num, created_sec, text FROM ppt_pages
                       WHERE sub_id = ? AND ocr_status = 'done' AND text != ''
                       ORDER BY created_sec""",
                    (sub_id,),
                )
            ]
            return result

    def obsidian_notes(
        self,
        *,
        include_transcript: bool = False,
        include_ocr: bool = False,
    ) -> list[dict[str, Any]]:
        """Return generated notes in one read-only snapshot for Vault export.

        Only lectures that have an actual generated summary are candidates.
        Transcript and OCR text are deliberately opt-in because they can make
        a Vault substantially larger and are not needed for the usual note.
        """
        transcript_column = ", l.transcript" if include_transcript else ""
        sql = f"""
            SELECT l.sub_id, l.course_id, l.sub_title, l.date, l.processed_at,
                   l.summary, l.summary_model, c.title AS course_title,
                   c.teacher{transcript_column}
            FROM lectures l
            JOIN courses c ON c.course_id = l.course_id
            WHERE TRIM(COALESCE(l.summary, '')) != ''
            ORDER BY COALESCE(c.title, ''), c.course_id,
                     COALESCE(l.date, ''), COALESCE(l.sub_title, ''), l.sub_id
        """
        with closing(self._connect()) as db:
            notes = [dict(row) for row in db.execute(sql)]
            if not include_ocr or not notes:
                return notes
            pages_by_sub_id: dict[str, list[dict[str, Any]]] = {
                str(note["sub_id"]): [] for note in notes
            }
            page_rows = db.execute(
                """
                SELECT p.sub_id, p.page_num, p.created_sec, p.text
                FROM ppt_pages p
                JOIN lectures l ON l.sub_id = p.sub_id
                WHERE p.ocr_status = 'done' AND TRIM(COALESCE(p.text, '')) != ''
                  AND TRIM(COALESCE(l.summary, '')) != ''
                ORDER BY p.sub_id, p.created_sec, p.page_num
                """
            )
            for page in page_rows:
                item = dict(page)
                pages = pages_by_sub_id.get(str(item.pop("sub_id")))
                if pages is not None:
                    pages.append(item)
        for note in notes:
            note["ocr_pages"] = pages_by_sub_id[str(note["sub_id"])]
        return notes

    def search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        needle = f"%{query.strip()}%"
        if needle == "%%":
            return []
        sql = """
            SELECT l.sub_id, l.sub_title, l.course_id, c.title AS course_title,
                   CASE
                     WHEN l.summary LIKE ? THEN 'summary'
                     WHEN l.transcript LIKE ? THEN 'transcript'
                     ELSE 'ocr'
                   END AS hit_field,
                   substr(COALESCE(l.summary, l.transcript, ''), 1, 240) AS snippet
            FROM lectures l JOIN courses c ON c.course_id = l.course_id
            WHERE l.summary LIKE ? OR l.transcript LIKE ? OR EXISTS (
                SELECT 1 FROM ppt_pages p
                WHERE p.sub_id = l.sub_id AND p.text LIKE ?
            )
            ORDER BY l.processed_at DESC
            LIMIT ?
        """
        params = (needle, needle, needle, needle, needle, max(1, min(limit, 100)))
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(sql, params)]

    def subscription_ids(self) -> list[str]:
        """Return the last subscription snapshot published with the data DB.

        GitHub deliberately does not reveal Actions Secret values.  The
        sharder publishes this non-secret mirror after a workflow run so a
        fresh local console can still present a useful starting selection.
        """
        with closing(self._connect()) as db:
            try:
                row = db.execute(
                    """SELECT value FROM meta
                       WHERE key IN ('subscribed_course_ids', 'course_ids')
                       ORDER BY CASE key WHEN 'subscribed_course_ids' THEN 0 ELSE 1 END
                       LIMIT 1"""
                ).fetchone()
            except sqlite3.OperationalError:
                return []
        if not row:
            return []
        return [item.strip() for item in str(row[0] or "").split(",") if item.strip()]

    def subscription_terms(self) -> list[str]:
        with closing(self._connect()) as db:
            try:
                rows = db.execute(
                    "SELECT DISTINCT term FROM all_courses "
                    "WHERE TRIM(COALESCE(term, '')) != '' ORDER BY term DESC"
                )
            except sqlite3.OperationalError:
                return []
            return [str(row[0]) for row in rows]

    def subscription_catalog(
        self, query: str = "", term: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Search the course catalog without loading its full contents into JS."""
        needle = f"%{query.strip()}%"
        clauses = [
            "(title LIKE ? OR teacher LIKE ? OR dept LIKE ? OR course_id LIKE ?)",
        ]
        params: list[Any] = [needle, needle, needle, needle]
        if term.strip():
            clauses.append("term = ?")
            params.append(term.strip())
        params.append(max(1, min(limit, 200)))
        sql = f"""
            SELECT course_id, term, title, teacher, dept
            FROM all_courses
            WHERE {' AND '.join(clauses)}
            ORDER BY term DESC, title COLLATE NOCASE, course_id
            LIMIT ?
        """
        with closing(self._connect()) as db:
            try:
                return [dict(row) for row in db.execute(sql, params)]
            except sqlite3.OperationalError:
                return []

    def subscription_courses(self, course_ids: list[str]) -> list[dict[str, Any]]:
        """Resolve chosen IDs using the catalog, with course history as fallback."""
        normalized = list(dict.fromkeys(str(item).strip() for item in course_ids if str(item).strip()))
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        sql = f"""
            WITH catalog AS (
                SELECT course_id, term, title, teacher, dept,
                       ROW_NUMBER() OVER (PARTITION BY course_id ORDER BY term DESC) AS ranking
                FROM all_courses
                WHERE course_id IN ({placeholders})
            )
            SELECT requested.course_id,
                   COALESCE(catalog.term, '') AS term,
                   COALESCE(catalog.title, courses.title, requested.course_id) AS title,
                   COALESCE(catalog.teacher, courses.teacher, '') AS teacher,
                   COALESCE(catalog.dept, '') AS dept
            FROM (SELECT ? AS course_id) AS requested
            LEFT JOIN catalog ON catalog.course_id = requested.course_id AND catalog.ranking = 1
            LEFT JOIN courses ON courses.course_id = requested.course_id
        """
        # SQLite cannot bind a VALUES list through one placeholder, so ask for
        # each selected ID with a compact UNION query and preserve caller order.
        requested = " UNION ALL ".join("SELECT ? AS course_id" for _ in normalized)
        sql = sql.replace("SELECT ? AS course_id", requested, 1)
        with closing(self._connect()) as db:
            try:
                return [dict(row) for row in db.execute(sql, [*normalized, *normalized])]
            except sqlite3.OperationalError:
                fallback = db.execute(
                    f"SELECT course_id, '' AS term, title, teacher, '' AS dept "
                    f"FROM courses WHERE course_id IN ({placeholders})",
                    normalized,
                )
                known = {str(row["course_id"]): dict(row) for row in fallback}
        return [
            known.get(course_id, {"course_id": course_id, "term": "", "title": course_id, "teacher": "", "dept": ""})
            for course_id in normalized
        ]
