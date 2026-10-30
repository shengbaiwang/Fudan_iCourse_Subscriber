"""Single-location note folders, including lossless migration from shared topics."""
from __future__ import annotations

import re
from uuid import uuid4


def folder_id(value, allow_root=False):
    if allow_root and value == "":
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("文件夹标识无效")
    return value


def folder_name(value):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= 60 or any(c in value for c in "/\\\n\r\x00"):
        raise ValueError("文件夹名称需为 1–60 个字，不能包含斜杠或换行")
    return value.strip()


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS note_folders (
            id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE,
            parent_id TEXT NOT NULL DEFAULT '', UNIQUE(parent_id, name)
        );
        CREATE TABLE IF NOT EXISTS note_locations (
            reference TEXT PRIMARY KEY, folder_id TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS note_folder_candidates (
            reference TEXT NOT NULL, folder_id TEXT NOT NULL,
            PRIMARY KEY(reference, folder_id)
        );
        CREATE TABLE IF NOT EXISTS note_organization_state (key TEXT PRIMARY KEY);
    """)
    with db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM note_organization_state WHERE key='folders-v1'").fetchone():
            topics = [dict(row) for row in db.execute("SELECT * FROM note_topics")]
            memberships = [dict(row) for row in db.execute("SELECT * FROM note_topic_memberships")]
            import_legacy(db, topics, memberships, {})
            db.execute("INSERT INTO note_organization_state VALUES ('folders-v1')")


def validate_tree(folders):
    if not isinstance(folders, list) or len(folders) > 10000:
        raise ValueError("文件夹内容无效")
    by_id, names = {}, set()
    ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    for row in folders:
        key, name, parent = folder_id(row['id']), folder_name(row['name']), folder_id(row.get('parent_id', ''), True)
        if key in by_id or (parent, name.translate(ascii_lower)) in names:
            raise ValueError("存在重复文件夹")
        by_id[key] = {'id': key, 'name': name, 'parent_id': parent}
        names.add((parent, name.translate(ascii_lower)))
    ordered = []
    def visit(key, chain):
        if key in chain:
            raise ValueError("文件夹不能放入自身或子文件夹")
        if len(chain) >= 32:
            raise ValueError("文件夹最多支持 32 层")
        row = by_id[key]
        parent = row['parent_id']
        if parent and parent not in by_id:
            raise ValueError("上级文件夹不存在")
        if parent:
            visit(parent, chain | {key})
        if key not in seen:
            seen.add(key)
            ordered.append(row)
    seen = set()
    for key in by_id:
        visit(key, set())
    return ordered


def remap_reference(db, key, ids):
    from .note_library import reference
    kind, source_id = key.split(':', 1)
    if kind == 'personal':
        source_id = ids.get(source_id, source_id)
        if not db.execute("SELECT 1 FROM notes WHERE id=?", (source_id,)).fetchone():
            return None
    return reference(kind, source_id)


def import_legacy(db, topics, memberships, ids):
    """A multi-topic note waits in the inbox with all original choices intact."""
    if not all(isinstance(items, list) and len(items) <= 100000 for items in (topics, memberships)):
        raise ValueError("备份归类无效")
    mapping = {}
    for row in topics:
        key = folder_id(row['id'])
        original_name = row['name']
        if not isinstance(original_name, str):
            raise ValueError("备份归类无效")
        name = folder_name(original_name.replace('\x00', '�').replace('/', '／').replace('\\', '＼').replace('\n', ' ').replace('\r', ' '))
        existing = db.execute("SELECT id FROM note_folders WHERE parent_id='' AND name=? COLLATE NOCASE", (name,)).fetchone()
        target = existing[0] if existing else key
        if not existing:
            if db.execute("SELECT 1 FROM note_folders WHERE id=?", (target,)).fetchone():
                target = uuid4().hex
            db.execute("INSERT INTO note_folders VALUES (?,?,'')", (target, name))
        mapping[key] = target
    choices = {}
    for row in memberships:
        key = remap_reference(db, row['reference'], ids)
        if row['topic_id'] not in mapping:
            raise ValueError("备份归类无效")
        if key:
            choices.setdefault(key, set()).add(mapping[row['topic_id']])
    for key, options in choices.items():
        if db.execute("SELECT 1 FROM note_locations WHERE reference=?", (key,)).fetchone():
            continue
        db.execute("INSERT INTO note_locations VALUES (?,?,1)", (key, next(iter(options)) if len(options) == 1 else ''))
        if len(options) > 1:
            db.executemany("INSERT OR IGNORE INTO note_folder_candidates VALUES (?,?)", [(key, value) for value in options])


def import_folders(db, document, ids):
    ordered = validate_tree(document.get('folders', []))
    mapping = {'': ''}
    for row in ordered:
        parent = mapping[row['parent_id']]
        existing = db.execute("SELECT id FROM note_folders WHERE parent_id=? AND name=? COLLATE NOCASE", (parent, row['name'])).fetchone()
        target = existing[0] if existing else row['id']
        if not existing:
            if db.execute("SELECT 1 FROM note_folders WHERE id=?", (target,)).fetchone():
                target = uuid4().hex
            db.execute("INSERT INTO note_folders VALUES (?,?,?)", (target, row['name'], parent))
        mapping[row['id']] = target
    locations, candidates = document.get('locations', []), document.get('folder_candidates', [])
    if not all(isinstance(rows, list) and len(rows) <= 100000 for rows in (locations, candidates)):
        raise ValueError("备份文件夹归属无效")
    imported = set()
    for row in locations:
        key = remap_reference(db, row['reference'], ids)
        if row['folder_id'] not in mapping or int(row.get('revision', 1)) < 1:
            raise ValueError("备份文件夹归属无效")
        if key and not db.execute("SELECT 1 FROM note_locations WHERE reference=?", (key,)).fetchone():
            db.execute("INSERT INTO note_locations VALUES (?,?,1)", (key, mapping[row['folder_id']]))
            imported.add(key)
    for row in candidates:
        key = remap_reference(db, row['reference'], ids)
        if not row['folder_id'] or row['folder_id'] not in mapping:
            raise ValueError("备份待整理归属无效")
        if key in imported:
            db.execute("INSERT OR IGNORE INTO note_folder_candidates VALUES (?,?)", (key, mapping[row['folder_id']]))
