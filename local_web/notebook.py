"""Durable personal notes, independent of the course mirror and its cache."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

from .state import default_config_dir


class NoteConflict(ValueError):
    pass


class Notebook:
    def __init__(self, directory: Path | None = None):
        self.directory = (directory or default_config_dir()) / "library"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "notebook.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                    starred INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    note_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, saved_at TEXT NOT NULL,
                    PRIMARY KEY(note_id, revision)
                );
                CREATE TABLE IF NOT EXISTS note_topics (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE
                );
                CREATE TABLE IF NOT EXISTS note_topic_memberships (
                    reference TEXT NOT NULL, topic_id TEXT NOT NULL,
                    PRIMARY KEY(reference, topic_id)
                );
                CREATE TABLE IF NOT EXISTS source_note_stars (
                    reference TEXT PRIMARY KEY
                );
            """)
            from .note_folders import initialize
            initialize(db)
            from .note_edits import initialize as initialize_edits
            initialize_edits(db)
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return closing(db)

    def list(self, query: str = "", trash: bool = False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM notes WHERE deleted=? ORDER BY starred DESC, updated_at DESC", (int(trash),)).fetchall()
        terms = query.casefold().split()
        return [dict(row) for row in rows if all(t in (row["title"] + " " + row["body"]).casefold() for t in terms)]

    def get(self, note_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if row is None:
            raise KeyError(note_id)
        return dict(row)

    def save(self, note_id: str | None, title: str, body: str, revision: int = 0, **flags):
        title = title.strip()[:200] or "未命名笔记"
        if len(body) > 2_000_000:
            raise ValueError("笔记最多支持 200 万字（含嵌入图片）")
        now = datetime.now(timezone.utc).isoformat()
        with self.lock, self.connect() as db, db:
            if note_id is None:
                note_id = uuid4().hex
                db.execute("INSERT INTO notes (id,title,body,created_at,updated_at) VALUES (?,?,?,?,?)", (note_id, title, body, now, now))
            else:
                row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
                if row is None:
                    raise KeyError(note_id)
                if row["revision"] != revision:
                    raise NoteConflict("这篇笔记已在另一窗口修改，当前草稿已保留，请重新打开后合并")
                starred = int(flags.get("starred", row["starred"]))
                deleted = int(flags.get("deleted", row["deleted"]))
                if title == row["title"] and body == row["body"] and starred == row["starred"] and deleted == row["deleted"]:
                    return dict(row)
                if title != row["title"] or body != row["body"]:
                    db.execute("INSERT INTO versions VALUES (?,?,?,?,?)", (note_id, revision, row["title"], row["body"], now))
                    db.execute("DELETE FROM versions WHERE note_id=? AND revision NOT IN (SELECT revision FROM versions WHERE note_id=? ORDER BY revision DESC LIMIT 50)", (note_id, note_id))
                db.execute("UPDATE notes SET title=?,body=?,starred=?,deleted=?,revision=revision+1,updated_at=? WHERE id=?", (title, body, starred, deleted, now, note_id))
        return self.get(note_id)

    def versions(self, note_id):
        self.get(note_id)
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM versions WHERE note_id=? ORDER BY revision DESC", (note_id,))]

    def backup(self):
        with self.lock, self.connect() as db:
            db.execute("BEGIN")
            notes = [dict(row) for row in db.execute("SELECT * FROM notes ORDER BY created_at")]
            versions = [dict(row) for row in db.execute("SELECT * FROM versions")]
            folders = [dict(row) for row in db.execute("SELECT * FROM note_folders")]
            locations = [dict(row) for row in db.execute("SELECT * FROM note_locations")]
            candidates = [dict(row) for row in db.execute("SELECT * FROM note_folder_candidates")]
            stars = [row[0] for row in db.execute("SELECT reference FROM source_note_stars")]
            source_edits = [dict(row) for row in db.execute("SELECT * FROM source_note_edits")]
            source_versions = [dict(row) for row in db.execute("SELECT * FROM source_note_edit_versions")]
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr("丰饶之海.json", json.dumps({"format": "sea-notebook", "version": 2, "notes": notes, "versions": versions,
                                                      "folders": folders, "locations": locations, "folder_candidates": candidates, "source_stars": stars,
                                                      "source_edits": source_edits, "source_edit_versions": source_versions}, ensure_ascii=False, indent=2))
            for row in notes:
                if row["deleted"]:
                    continue
                name = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", row["title"]).strip(". ")[:100] or "未命名笔记"
                archive.writestr(f"个人笔记/{name}-{row['id'][:8]}.md", f"# {row['title']}\n\n{row['body']}\n")
        return stream.getvalue()

    def restore(self, data: bytes):
        if len(data) > 20_000_000:
            raise ValueError("备份文件不能超过 20 MB")
        try:
            with ZipFile(BytesIO(data)) as archive:
                info = archive.getinfo("丰饶之海.json")
                if info.file_size > 40_000_000:
                    raise ValueError("备份内容过大")
                document = json.loads(archive.read(info))
            if document.get("format") != "sea-notebook" or document.get("version") not in (1, 2):
                raise ValueError("不支持这个备份格式")
            notes, versions = document["notes"], document["versions"]
            if not isinstance(notes, list) or not isinstance(versions, list) or len(notes) > 10000 or len(versions) > 500000:
                raise ValueError("备份内容无效")
            for row in notes:
                if not isinstance(row["title"], str) or not isinstance(row["body"], str) or len(row["body"]) > 2_000_000:
                    raise ValueError("备份手记无效")
                if not re.fullmatch(r"[a-f0-9]{32}", row["id"]) or int(row["revision"]) < 1:
                    raise ValueError("备份手记标识无效")
                for name in ("created_at", "updated_at"):
                    datetime.fromisoformat(row[name])
            count = 0
            with self.lock, self.connect() as db, db:
                db.execute("BEGIN IMMEDIATE")
                ids = {}
                for row in notes:
                    existing = db.execute("SELECT * FROM notes WHERE id=?", (row["id"],)).fetchone()
                    if existing and existing["title"] == row["title"] and existing["body"] == row["body"]:
                        continue
                    new_id = uuid4().hex if existing else row["id"]
                    ids[row["id"]] = new_id
                    db.execute("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?)", (new_id, row["title"], row["body"], int(bool(row.get("starred"))), int(bool(row.get("deleted"))), int(row["revision"]), row["created_at"], row["updated_at"]))
                    count += 1
                for row in versions:
                    if row["note_id"] in ids:
                        if not isinstance(row["title"], str) or not isinstance(row["body"], str):
                            raise ValueError("备份历史版本无效")
                        db.execute("INSERT OR IGNORE INTO versions VALUES (?,?,?,?,?)", (ids[row["note_id"]], int(row["revision"]), row["title"], row["body"], row["saved_at"]))
                from .note_library import reference
                from .note_folders import import_folders, import_legacy
                if document["version"] == 2:
                    import_folders(db, document, ids)
                else:
                    import_legacy(db, document.get("topics", []), document.get("memberships", []), ids)
                stars = document.get("source_stars", [])
                if not isinstance(stars, list) or len(stars) > 100000:
                    raise ValueError("备份收藏无效")
                for key in stars:
                    if not isinstance(key, str):
                        raise ValueError("备份收藏来源无效")
                    kind, source_id = key.split(":", 1)
                    if kind == "personal":
                        raise ValueError("备份收藏来源无效")
                    db.execute("INSERT OR IGNORE INTO source_note_stars VALUES (?)", (reference(kind, source_id),))
                from .note_edits import restore as restore_edits
                count += restore_edits(db, document)
            return count
        except (KeyError, TypeError, AttributeError, OverflowError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise ValueError("备份内容不完整，未导入任何笔记") from exc
