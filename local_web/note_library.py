"""A live view of personal, course and talk notes, with local folders.

Generated text stays in its owning store. Only references and organization are
saved in the notebook, so sync and regeneration cannot leave copied notes stale.
"""
from __future__ import annotations

from uuid import uuid4

from .database import _center_snippet, auto_lecture_title
from .talks import auto_talk_title
from .notebook import NoteConflict
from .note_folders import folder_id, folder_name, validate_tree
from . import note_edits


KINDS = {"personal", "course", "talk"}


def reference(kind, source_id):
    if kind not in KINDS | {"course-group"} or not isinstance(source_id, str) or not source_id or len(source_id) > 200:
        raise ValueError("无效的笔记来源")
    return f"{kind}:{source_id}"


class NoteLibrary:
    def __init__(self, notebook, database, talks, lecture_names=lambda: {}):
        self.notebook, self.database, self.talks = notebook, database, talks
        self.lecture_names = lecture_names
        self.shared = False
        self.shared_deleted = set()
        self.shared_paths = {}

    def _personal(self, row):
        return {**row, "kind": "personal", "source_id": row["id"],
                "id": reference("personal", row["id"]), "source_label": "个人笔记"}

    def _course(self, row):
        source_id = str(row["sub_id"])
        return {"id": reference("course", source_id), "kind": "course", "source_id": source_id,
                "title": self.lecture_names().get(source_id) or row.get("ai_title")
                         or auto_lecture_title(row.get("summary"), row.get("sub_title")),
                "body": row.get("summary") or "", "updated_at": row.get("processed_at") or row.get("date") or "",
                "source_label": row.get("course_title") or "课程", "course_id": str(row["course_id"]),
                "lecture_date": row.get("date") or "", "lecture_label": row.get("sub_title") or "",
                "starred": False, "deleted": False}

    def _talk(self, row):
        return {"id": reference("talk", row["id"]), "kind": "talk", "source_id": row["id"],
                "title": row.get("display_title") or row.get("title") or row.get("ai_title")
                         or auto_talk_title(row.get("summary"), "未命名讲座"),
                "body": row.get("summary") or "", "updated_at": row.get("updated_at") or "",
                "source_label": "讲座", "starred": False, "deleted": False}

    def _migrate_course_locations(self):
        # Resolve old per-lecture placements only when source data is available.
        # Unresolved references stay intact until a later source sync.
        with self.notebook.connect() as db:
            legacy = db.execute("SELECT 1 FROM note_locations WHERE reference LIKE 'course:%' LIMIT 1").fetchone()
        if not legacy or not self.database.db_path.is_file():
            return
        course_ids = self.database.note_course_ids()
        with self.notebook.lock, self.notebook.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            groups = {}
            for row in db.execute("SELECT * FROM note_locations WHERE reference LIKE 'course:%'"):
                course_id = course_ids.get(row['reference'].split(':', 1)[1])
                if course_id is not None:
                    groups.setdefault(course_id, []).append(dict(row))
            for course_id, rows in groups.items():
                key = reference('course-group', course_id)
                if not db.execute("SELECT 1 FROM note_locations WHERE reference=?", (key,)).fetchone():
                    choices = {row['folder_id'] for row in rows}
                    for row in rows:
                        choices.update(item[0] for item in db.execute("SELECT folder_id FROM note_folder_candidates WHERE reference=?", (row['reference'],)))
                    target = next(iter(choices)) if len(choices) == 1 else ''
                    revision = max(row['revision'] for row in rows) + 1
                    db.execute("INSERT INTO note_locations VALUES (?,?,?)", (key, target, revision))
                    if len(choices) > 1:
                        db.executemany("INSERT INTO note_folder_candidates VALUES (?,?)", [(key, value) for value in sorted(choices) if value])
                # Existing course-wide choices are authoritative on backup merge.
                for row in rows:
                    db.execute("DELETE FROM note_locations WHERE reference=?", (row['reference'],))
                    db.execute("DELETE FROM note_folder_candidates WHERE reference=?", (row['reference'],))

    def metadata(self):
        if not self.shared:
            self._migrate_course_locations()
        with self.notebook.connect() as db:
            db.execute("BEGIN")
            folders = [dict(row) for row in db.execute("SELECT * FROM note_folders ORDER BY name COLLATE NOCASE, id")]
            locations = {row["reference"]: dict(row) for row in db.execute("SELECT * FROM note_locations")}
            candidates = {}
            for row in db.execute("SELECT * FROM note_folder_candidates"):
                candidates.setdefault(row["reference"], []).append(row["folder_id"])
            stars = {row[0] for row in db.execute("SELECT reference FROM source_note_stars")}
        return {"folders": folders, "locations": locations, "candidates": candidates, "stars": stars}

    def _location(self, row, metadata):
        key = reference("course-group", row["course_id"]) if row["kind"] == "course" and not self.shared else row["id"]
        row["location_reference"] = key
        location = metadata["locations"].get(key, {})
        row["folder_id"] = location.get("folder_id", "")
        row["location_revision"] = location.get("revision", 0)
        row["folder_candidates"] = metadata["candidates"].get(key, [])
        return row

    def list(self, query="", kind="all", folder=None, starred=False, trash=False):
        if kind not in KINDS | {"all"}:
            raise ValueError("未知笔记类型")
        rows = []
        if kind in {"all", "personal"}:
            rows.extend(self._personal(row) for row in self.notebook.list(trash=trash))
        if not trash or self.shared:
            if kind in {"all", "course"} and self.database.db_path.is_file():
                rows.extend(self._course(row) for row in self.database.obsidian_notes())
            if kind in {"all", "talk"}:
                rows.extend(self._talk(row) for row in self.talks.note_summaries())
        metadata = self.metadata()
        terms = query.casefold().split()
        edits = note_edits.entries(self.notebook)
        entries = []
        for row in rows:
            if self.shared:
                row['deleted'] = bool(row['deleted'] or row['id'] in self.shared_deleted)
                if row['deleted'] != trash:
                    continue
            if row['kind'] != 'personal':
                note_edits.apply(row, edits.get(row['id']))
            row["starred"] = bool(row["starred"] or row["id"] in metadata["stars"])
            self._location(row, metadata)
            if (starred and not row["starred"]) or (folder is not None and folder != row["folder_id"]):
                continue
            haystack = f"{row['title']} {row['body']} {row['source_label']}".casefold()
            if not all(term in haystack for term in terms):
                continue
            row["excerpt"] = _center_snippet(row.pop("body"), terms, radius=60)
            entries.append(row)
        entries.sort(key=lambda row: (row["starred"], row["updated_at"], row["id"]), reverse=True)
        return {"notes": entries, "folders": metadata["folders"]}

    def get(self, kind, source_id, *, with_edits=True):
        key = reference(kind, source_id)
        if kind == "course-group":
            courses = self.database.courses() if self.database.db_path.is_file() else []
            course = next((row for row in courses if str(row['course_id']) == source_id), None)
            if course is None:
                raise KeyError(key)
            row = {"id": key, "kind": kind, "source_id": source_id, "course_id": source_id,
                   "title": course['title'], "source_label": course['title'], "deleted": False}
            return self._location(row, self.metadata())
        if kind == "personal":
            result = self._personal(self.notebook.get(source_id))
        else:
            if kind == "course":
                row = self.database.lecture(source_id) if self.database.db_path.is_file() else None
                result = self._course(row) if row else None
                versions = row.get("summary_versions", []) if row else []
                generated_at = row.get("processed_at") if row else ""
            else:
                row = self.talks.get_talk(source_id)
                result = self._talk(row) if row else None
                versions = row.get("versions", []) if row else []
                generated_at = row.get("updated_at") if row else ""
            if not result or not result["body"].strip():
                raise KeyError(key)
            # The active source text is always the default, even if an older
            # schema or a model comparison lists a different version first.
            result["versions"] = [{"key": "active", "model": row.get("summary_model") or "未记录模型",
                                   "summary": result["body"], "generated_at": generated_at, "active": True}]
            for index, version in enumerate(versions):
                if not str(version.get("summary") or "").strip():
                    continue
                if any(item["summary"] == version["summary"] and item["model"] == version.get("model")
                       for item in result["versions"]):
                    continue
                result["versions"].append({**version, "key": f"version-{index}", "active": False})
            result["transcript"] = row.get("transcript") or ""
            if with_edits:
                edit = note_edits.get(self.notebook, key)
                note_edits.apply(result, edit)
                if edit:
                    result['versions'][0].update(key='source-active', active=False)
                    result['versions'].insert(0, {'key': 'active', 'model': '手动修订', 'summary': result['body'],
                                                 'generated_at': result['updated_at'], 'active': True})
        metadata = self.metadata()
        result["starred"] = bool(result["starred"] or key in metadata["stars"])
        if self.shared and result['id'] in self.shared_deleted:
            result['deleted'] = True
        return self._location(result, metadata)

    def save_edit(self, kind, source_id, title, body, revision):
        if kind not in {'course', 'talk'}:
            raise ValueError('仅课程和讲座使用来源修订')
        row = self.get(kind, source_id, with_edits=False)
        note_edits.save(self.notebook, row, title, body, revision)
        return self.get(kind, source_id)

    def edit_versions(self, kind, source_id):
        row = self.get(kind, source_id)
        return note_edits.versions(self.notebook, row['id'])

    def set_star(self, kind, source_id, value):
        row = self.get(kind, source_id)
        if kind == "personal":
            self.notebook.save(source_id, row["title"], row["body"], row["revision"], starred=value)
        else:
            with self.notebook.lock, self.notebook.connect() as db, db:
                db.execute("BEGIN IMMEDIATE")
                if value:
                    db.execute("INSERT OR IGNORE INTO source_note_stars VALUES (?)", (row["id"],))
                else:
                    db.execute("DELETE FROM source_note_stars WHERE reference=?", (row["id"],))
        return self.get(kind, source_id)

    def save_folder(self, name, key=None, parent_id=None):
        name = folder_name(name)
        with self.notebook.lock, self.notebook.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM note_folders WHERE id=?", (key,)).fetchone() if key else None
            if key and not existing:
                raise KeyError(key)
            parent = existing["parent_id"] if parent_id is None and existing else parent_id or ""
            folder_id(parent, True)
            key = key or uuid4().hex
            rows = [dict(row) for row in db.execute("SELECT * FROM note_folders WHERE id!=?", (key,))]
            candidate = {"id": key, "name": name, "parent_id": parent}
            validate_tree(rows + [candidate])
            db.execute("INSERT INTO note_folders VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,parent_id=excluded.parent_id", (key, name, parent))
        return candidate

    def move_folder(self, key, parent, expected_parent):
        with self.notebook.lock, self.notebook.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM note_folders WHERE id=?", (key,)).fetchone()
            if not row:
                raise KeyError(key)
            if row["parent_id"] != expected_parent:
                raise NoteConflict("文件夹已在另一窗口移动，请刷新后重试")
            folder_id(parent, True)
            rows = [dict(item) for item in db.execute("SELECT * FROM note_folders WHERE id!=?", (key,))]
            result = {"id": key, "name": row["name"], "parent_id": parent}
            validate_tree(rows + [result])
            db.execute("UPDATE note_folders SET parent_id=? WHERE id=?", (parent, key))
        return result

    def move_note(self, kind, source_id, target, revision, candidates=None):
        row = self.get(kind, source_id)
        if row["deleted"]:
            raise ValueError("请先恢复回收站中的笔记")
        key = row["location_reference"]
        folder_id(target, True)
        with self.notebook.lock, self.notebook.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            if target and not db.execute("SELECT 1 FROM note_folders WHERE id=?", (target,)).fetchone():
                raise KeyError(target)
            candidates = candidates or []
            if candidates and target:
                raise ValueError("待整理笔记应放在收件箱")
            for candidate in candidates:
                folder_id(candidate)
                if not db.execute("SELECT 1 FROM note_folders WHERE id=?", (candidate,)).fetchone():
                    raise KeyError(candidate)
            previous = db.execute("SELECT * FROM note_locations WHERE reference=?", (key,)).fetchone()
            current_revision = previous["revision"] if previous else 0
            if current_revision != revision:
                raise NoteConflict("笔记已在另一窗口移动，请刷新后重试")
            current_folder = previous["folder_id"] if previous else ""
            pending = bool(db.execute("SELECT 1 FROM note_folder_candidates WHERE reference=?", (key,)).fetchone())
            changed = current_folder != target or pending or bool(candidates)
            if changed:
                db.execute("INSERT INTO note_locations VALUES (?,?,?) ON CONFLICT(reference) DO UPDATE SET folder_id=excluded.folder_id,revision=excluded.revision", (key, target, revision + 1))
                db.execute("DELETE FROM note_folder_candidates WHERE reference=?", (key,))
                db.executemany("INSERT OR IGNORE INTO note_folder_candidates VALUES (?,?)", [(key, candidate) for candidate in candidates])
        return {**self.get(kind, source_id), "location_changed": changed}

    def delete_folder(self, key):
        if not self.shared:
            self._migrate_course_locations()
        with self.notebook.lock, self.notebook.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = [dict(row) for row in db.execute("SELECT * FROM note_folders")]
            if not any(row['id'] == key for row in rows):
                raise KeyError(key)
            keys = {key}
            while True:
                children = {row['id'] for row in rows if row['parent_id'] in keys}
                if children <= keys:
                    break
                keys |= children
            locations = [dict(row) for row in db.execute("SELECT * FROM note_locations") if row['folder_id'] in keys]
            candidates = [dict(row) for row in db.execute("SELECT c.*,l.revision AS location_revision FROM note_folder_candidates c JOIN note_locations l ON l.reference=c.reference") if row['folder_id'] in keys]
            for row in locations:
                db.execute("UPDATE note_locations SET folder_id='',revision=revision+1 WHERE reference=?", (row['reference'],))
            db.executemany("DELETE FROM note_folder_candidates WHERE folder_id=?", [(value,) for value in keys])
            db.executemany("DELETE FROM note_folders WHERE id=?", [(value,) for value in keys])
        return {"folders": [row for row in rows if row['id'] in keys], "locations": locations, "folder_candidates": candidates}

    def restore_folder(self, snapshot):
        folders = snapshot['folders']
        locations, candidates = snapshot['locations'], snapshot['folder_candidates']
        if not folders or not all(isinstance(rows, list) and len(rows) <= 100000 for rows in (locations, candidates)):
            raise ValueError("文件夹恢复内容无效")
        keys = {folder_id(row['id']) for row in folders}
        with self.notebook.lock, self.notebook.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = [dict(row) for row in db.execute("SELECT * FROM note_folders")]
            ordered = validate_tree(existing + folders)
            for row in locations:
                kind, source_id = row['reference'].split(':', 1)
                reference(kind, source_id)
                current = db.execute("SELECT * FROM note_locations WHERE reference=?", (row['reference'],)).fetchone()
                if row['folder_id'] not in keys or not current or current['folder_id'] != '' or current['revision'] != row['revision'] + 1:
                    raise NoteConflict("笔记位置已变化，未覆盖后续整理；可重新创建文件夹")
            for row in ordered:
                if row['id'] in keys:
                    db.execute("INSERT INTO note_folders VALUES (?,?,?)", (row['id'], row['name'], row['parent_id']))
            for row in locations:
                db.execute("UPDATE note_locations SET folder_id=?,revision=revision+1 WHERE reference=?", (row['folder_id'], row['reference']))
            for row in candidates:
                kind, source_id = row['reference'].split(':', 1)
                reference(kind, source_id)
                if row['folder_id'] not in keys:
                    raise ValueError("文件夹恢复内容无效")
                # A later explicit choice must remain authoritative.
                current = db.execute("SELECT folder_id,revision FROM note_locations WHERE reference=?", (row['reference'],)).fetchone()
                if current and current['folder_id'] == '' and current['revision'] == row.get('location_revision'):
                    db.execute("INSERT OR IGNORE INTO note_folder_candidates VALUES (?,?)", (row['reference'], row['folder_id']))
        return {"restored": len(keys)}
