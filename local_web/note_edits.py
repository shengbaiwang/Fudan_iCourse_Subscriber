"""Durable manual revisions of generated notes, separate from source refreshes."""
from datetime import datetime, timezone
import hashlib
import json

from .notebook import NoteConflict


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS source_note_edits (
            reference TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
            revision INTEGER NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_note_edit_versions (
            reference TEXT NOT NULL, revision INTEGER NOT NULL, title TEXT NOT NULL,
            body TEXT NOT NULL, saved_at TEXT NOT NULL, PRIMARY KEY(reference, revision)
        );
    """)


def entries(book):
    with book.connect() as db:
        return {row['reference']: dict(row) for row in db.execute('SELECT * FROM source_note_edits')}


def get(book, key):
    with book.connect() as db:
        row = db.execute('SELECT * FROM source_note_edits WHERE reference=?', (key,)).fetchone()
        return dict(row) if row else None


def apply(row, edit):
    # Include the live source in the token: a source refresh while typing must
    # never silently replace the base the user actually read.
    row['edit_revision'] = hashlib.sha256(json.dumps(
        [row['title'], row['body'], edit], sort_keys=True, ensure_ascii=False
    ).encode()).hexdigest()
    row['manually_edited'] = bool(edit)
    if edit:
        row.update({key: edit[key] for key in ('title', 'body', 'updated_at')})
    return row


def save(book, row, title, body, token):
    title = title.strip()[:200] or '未命名笔记'
    if len(body) > 2_000_000:
        raise ValueError('笔记最多支持 200 万字（含嵌入图片）')
    key = row['id']
    now = datetime.now(timezone.utc).isoformat()
    with book.lock, book.connect() as db, db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT * FROM source_note_edits WHERE reference=?', (key,)).fetchone()
        current = apply(dict(row), dict(previous) if previous else None)
        if current['edit_revision'] != token:
            raise NoteConflict('这篇笔记已在另一窗口修改或来源已更新，当前草稿已保留')
        if (current['title'], current['body']) == (title, body):
            return
        revision = previous['revision'] if previous else 0
        db.execute('INSERT INTO source_note_edit_versions VALUES (?,?,?,?,?)',
                   (key, revision, current['title'], current['body'], now))
        db.execute('INSERT OR REPLACE INTO source_note_edits VALUES (?,?,?,?,?)',
                   (key, title, body, revision + 1, now))
        db.execute('DELETE FROM source_note_edit_versions WHERE reference=? AND revision NOT IN '
                   '(SELECT revision FROM source_note_edit_versions WHERE reference=? ORDER BY revision DESC LIMIT 50)', (key, key))


def versions(book, key):
    with book.connect() as db:
        return [dict(row) for row in db.execute(
            'SELECT * FROM source_note_edit_versions WHERE reference=? ORDER BY revision DESC', (key,))]


def restore(db, document):
    """Merge revisions; preserve conflicting imported edits as personal notes."""
    from uuid import uuid4
    from .note_library import reference
    edits = document.get('source_edits', [])
    history = document.get('source_edit_versions', [])
    if not isinstance(edits, list) or not isinstance(history, list) or len(edits) > 10000 or len(history) > 500000:
        raise ValueError('备份笔记修订无效')
    imported, accepted, copies = 0, set(), {}
    for row in edits + history:
        kind, source_id = row['reference'].split(':', 1)
        if kind not in {'course', 'talk'} or reference(kind, source_id) != row['reference']:
            raise ValueError('备份笔记修订来源无效')
        if not isinstance(row['title'], str) or not isinstance(row['body'], str) or len(row['body']) > 2_000_000 or int(row['revision']) < 0:
            raise ValueError('备份笔记修订内容无效')
        datetime.fromisoformat(row.get('updated_at') or row['saved_at'])
    for row in edits:
        key = row['reference']
        old = db.execute('SELECT * FROM source_note_edits WHERE reference=?', (key,)).fetchone()
        if old and (old['title'], old['body']) != (row['title'], row['body']):
            # Do not overwrite newer local work during backup import.
            if not db.execute('SELECT 1 FROM notes WHERE title=? AND body=?', (row['title'], row['body'])).fetchone():
                copy_id = uuid4().hex
                db.execute('INSERT INTO notes VALUES (?,?,?,?,?,?,?,?)',
                           (copy_id, row['title'], row['body'], 0, 0, int(row['revision']) + 1, row['updated_at'], row['updated_at']))
                copies[key] = copy_id
                imported += 1
        elif not old:
            db.execute('INSERT INTO source_note_edits VALUES (?,?,?,?,?)',
                       (key, row['title'], row['body'], int(row['revision']), row['updated_at']))
            accepted.add(key)
            imported += 1
    for row in history:
        if row['reference'] in accepted:
            db.execute('INSERT OR IGNORE INTO source_note_edit_versions VALUES (?,?,?,?,?)',
                       (row['reference'], int(row['revision']), row['title'], row['body'], row['saved_at']))
        elif row['reference'] in copies:
            db.execute('INSERT OR IGNORE INTO versions VALUES (?,?,?,?,?)',
                       (copies[row['reference']], int(row['revision']) + 1, row['title'], row['body'], row['saved_at']))
    return imported
