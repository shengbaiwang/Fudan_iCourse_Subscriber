"""Local Markdown vault with a recoverable SQLite index and optimistic writes.

The vault owns text and placement. Existing stores retain source media, generated
candidates and history. Every HTTP edit first imports external changes so stale
editors receive the notebook's existing revision conflict instead of overwriting.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import quote, unquote
from uuid import uuid4

import yaml

from . import note_edits
from .note_folders import validate_tree
from .notebook import NoteConflict
from .obsidian_appearance import AppearanceSync


MARKER = '.sea-folder.json'
MAX_BYTES = 12_000_000


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def content_hash(row):
    return digest(json.dumps([row['title'], row['body']], ensure_ascii=False))


def component(value):
    value = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '＿', value).strip(' .')[:180]
    while len(value.encode('utf-8')) > 200:
        value = value[:-1]
    return value or '未命名笔记'


class SharedNotes:
    def __init__(self, library, root=None):
        self.library, self.book = library, library.notebook
        configured = os.environ.get('SEA_SHARED_NOTES_DIR')
        if root is None:
            # Isolated test/config profiles must never touch the real vault.
            from .state import default_config_dir
            default = Path.home() / 'Documents/丰饶之海'
            root = configured or (default if not os.environ.get('ICOURSE_WEB_CONFIG_DIR')
                                  and self.book.directory.parent == default_config_dir() and default.is_dir() else None)
        self.root = Path(root).expanduser().absolute() if root else None
        self.enabled = self.root is not None
        self.appearance = AppearanceSync(self.root) if self.enabled else None
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.error = ''
        self.conflicts = []
        self.generation = 0
        self.state = None
        self.file_cache = {}
        self.library.shared = self.enabled

    def status(self):
        return {'enabled': self.enabled, 'path': str(self.root or ''), 'error': self.error,
                'conflicts': self.conflicts, 'generation': self.generation}

    def start(self):
        if not self.enabled or self.thread:
            return
        def watch():
            while not self.stop.is_set():
                self.appearance.refresh()
                try:
                    self.call(lambda: None)
                except (OSError, ValueError, NoteConflict) as exc:
                    self.error = str(exc)
                self.stop.wait(2)
        self.thread = threading.Thread(target=watch, name='shared-notes', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
            self.thread = None

    def safe(self, path):
        path = Path(path)
        path.relative_to(self.root)
        current = self.root
        if current.is_symlink():
            raise ValueError('共享笔记目录不能是符号链接')
        for part in path.relative_to(self.root).parts:
            if part in ('', '.', '..'):
                raise ValueError('共享笔记路径无效')
            current = current / part
            if current.is_symlink():
                raise ValueError(f'共享目录含符号链接，已暂停：{current.name}')
        return path

    @contextmanager
    def exclusive(self):
        if not self.root.is_dir():
            raise ValueError('共享笔记目录暂不可用；请恢复目录后重试')
        self.safe(self.root)
        internal = self.safe(self.root / '.sea')
        internal.mkdir(exist_ok=True)
        # Serialize application instances as well as HTTP/background threads.
        import fcntl
        with self.safe(internal / 'lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                state = self.safe(internal / 'state.json')
                self.state = json.loads(state.read_text('utf-8')) if state.exists() else {'version': 1, 'notes': {}, 'folders': {}}
                if not isinstance(self.state, dict):
                    raise ValueError('共享笔记索引无效，已暂停写入')
                if self.state.get('version') != 1 or not isinstance(self.state.get('notes'), dict) or not isinstance(self.state.get('folders'), dict):
                    raise ValueError('共享笔记索引无效，已暂停写入')
                course_folders = self.state.get('course_folders', {})
                if not isinstance(course_folders, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in course_folders.items()):
                    raise ValueError('课程文件夹索引无效，已暂停写入')
                for key, value in self.state['notes'].items():
                    if not isinstance(value, dict) or not all(isinstance(value.get(name), str) for name in ('path', 'raw_hash', 'app_hash')):
                        raise ValueError('共享笔记索引条目无效，已暂停写入')
                    self.safe(self.root / value['path'])
                for key, value in self.state['folders'].items():
                    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', key):
                        raise ValueError('共享文件夹索引无效，已暂停写入')
                    self.safe(self.root / value)
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def call(self, function, *args, **kwargs):
        if not self.enabled:
            return function(*args, **kwargs)
        with self.lock, self.book.lock:
            try:
                with self.exclusive():
                    self.reconcile()
                    key = None
                    if function == self.save_personal and args and args[0]:
                        key = 'personal:' + args[0]
                    elif function == self.library.save_edit and len(args) > 1:
                        key = args[0] + ':' + args[1]
                    if key and any(item['id'] == key for item in self.conflicts):
                        raise NoteConflict('笔记已在另一窗口或 Obsidian 修改，请先比较并合并；当前草稿已保留')
                    result = function(*args, **kwargs)
                    if function.__name__.startswith(('save', 'move', 'delete', 'restore', 'set_', 'resolve')):
                        self.reconcile()
                    self.error = ''
                    if isinstance(result, dict) and 'body' in result and 'id' in result:
                        if result.get('kind') in {'personal', 'course', 'talk'}:
                            result = {**result, **self.library.get(result['kind'], result['source_id'])}
                        elif re.fullmatch('[a-f0-9]{32}', str(result['id'])):
                            result = self.book.get(result['id'])
                    return result
            except NoteConflict:
                raise
            except (OSError, ValueError) as exc:
                self.error = str(exc)
                raise ValueError(f'共享笔记已暂停：{exc}') from exc

    def atomic(self, path, text, expected=None):
        path = self.safe(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        current = path.read_text('utf-8') if path.exists() else None
        if current != expected:
            raise NoteConflict('笔记已在另一窗口或 Obsidian 修改，当前草稿已保留，请重新打开后合并')
        descriptor, temporary = tempfile.mkstemp(prefix='.sea-write-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            self.safe(path)
            if (path.read_text('utf-8') if path.exists() else None) != current:
                raise NoteConflict('Obsidian 正在修改此笔记，当前草稿已保留')
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def scan(self, include_files=True):
        directories, files, ids = {}, {}, set()
        for base, dirs, names in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if not name.startswith('.'))
            for name in dirs:
                self.safe(Path(base) / name)
            path = Path(base)
            if path != self.root:
                relative = path.relative_to(self.root).as_posix()
                marker = self.safe(path / MARKER)
                marker_value = json.loads(marker.read_text('utf-8')) if marker.exists() else {}
                if not isinstance(marker_value, dict):
                    raise ValueError(f'文件夹标识格式无效：{relative}')
                key = marker_value.get('id')
                if marker.exists() and key is None:
                    raise ValueError(f'文件夹标识缺失：{relative}')
                if key is not None and (not isinstance(key, str) or not re.fullmatch('[a-f0-9]{32}', key) or key in ids):
                    raise ValueError(f'文件夹标识重复或无效：{relative}')
                if key:
                    ids.add(key)
                directories[relative] = key
            for name in sorted(names) if include_files else []:
                if name.startswith('.') or Path(name).suffix.lower() not in ('.md', '.markdown'):
                    continue
                target = self.safe(path / name)
                stat = target.stat()
                if stat.st_size > MAX_BYTES:
                    raise ValueError(f'笔记过大，已暂停：{name}')
                relative = target.relative_to(self.root).as_posix()
                signature = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                cached = self.file_cache.get(relative)
                if cached and cached[0] == signature:
                    files[relative] = dict(cached[1])
                    continue
                raw = target.read_text('utf-8')
                front, body = self.parse(raw)
                key = front.get('sea_id')
                if key is not None and (not isinstance(key, str) or not re.fullmatch(r'(personal|course|talk):[^\n\r]{1,200}', key)):
                    raise ValueError(f'笔记标识无效：{name}')
                relative = target.relative_to(self.root).as_posix()
                files[relative] = {'raw': raw, 'front': front, 'body': body, 'id': key, 'title': target.stem, 'path': relative}
                self.file_cache[relative] = (signature, dict(files[relative]))
        present = [item['id'] for item in files.values() if item['id']]
        if len(present) != len(set(present)):
            raise ValueError('检测到重复笔记标识，请移走副本或删除副本的 sea_id 后重试')
        return directories, files

    @staticmethod
    def parse(raw):
        if raw.startswith('---\n') or raw.startswith('---\r\n'):
            match = re.match(r'\A---\r?\n(.*?)\r?\n(?:---|\.\.\.)\s*\r?\n?', raw, re.S)
            if not match:
                raise ValueError('笔记属性区尚未写完，请完成编辑后重试')
            try:
                front = yaml.safe_load(match[1]) or {}
            except yaml.YAMLError as exc:
                raise ValueError('笔记属性格式尚未完成，请检查 YAML 后重试') from exc
            if not isinstance(front, dict):
                raise ValueError('笔记属性应为 YAML 键值')
            return front, raw[match.end():]
        return {}, raw

    def render(self, row, previous=None):
        front = dict(previous['front']) if previous else {}
        front['sea_id'] = row['id']
        if row['kind'] != 'personal':
            front['sea_source'] = row['kind']
            front['sea_source_id'] = row['source_id']
            if row.get('course_id'):
                front['sea_course_id'] = row['course_id']
        return '---\n' + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).rstrip() + '\n---\n' + row['body']

    def rows(self):
        rows = [self.library._personal(row) for row in self.book.list()]
        if self.library.database.db_path.is_file():
            rows += [self.library._course(row) for row in self.library.database.obsidian_notes()]
        rows += [self.library._talk(row) for row in self.library.talks.note_summaries()]
        edits = note_edits.entries(self.book)
        metadata = self.library.metadata()
        for row in rows:
            if row['kind'] != 'personal':
                note_edits.apply(row, edits.get(row['id']))
            self.library._location(row, metadata)
        return {row['id']: row for row in rows}

    def save_text(self, row, title, body):
        if row['kind'] == 'personal':
            return self.book.save(row['source_id'], title, body, row['revision'])
        source = self.library.get(row['kind'], row['source_id'], with_edits=False)
        # Current source + manual edit token prevents overwriting a refresh.
        current = self.library.get(row['kind'], row['source_id'])
        note_edits.save(self.book, source, title, body, current['edit_revision'])
        if not note_edits.get(self.book, row['id']):
            with self.book.connect() as db, db:
                db.execute('INSERT OR IGNORE INTO source_note_edits VALUES (?,?,?,?,?)',
                           (row['id'], title, body, 1, datetime.now(timezone.utc).isoformat()))

    def location(self, key, folder):
        with self.book.connect() as db, db:
            previous = db.execute('SELECT * FROM note_locations WHERE reference=?', (key,)).fetchone()
            if not previous or previous['folder_id'] != folder:
                db.execute('INSERT INTO note_locations VALUES (?,?,?) ON CONFLICT(reference) DO UPDATE SET folder_id=excluded.folder_id,revision=excluded.revision',
                           (key, folder, previous['revision'] + 1 if previous else 1))
                db.execute('DELETE FROM note_folder_candidates WHERE reference=?', (key,))

    @staticmethod
    def folder_paths(folders):
        by_id = {row['id']: row for row in validate_tree(folders)}
        paths = {'': ''}
        def path(key):
            if key not in paths:
                row = by_id[key]
                paths[key] = '/'.join(filter(None, [path(row['parent_id']), row['name']]))
            return paths[key]
        for key in by_id:
            path(key)
        return paths

    def folders(self):
        directories, _ = self.scan(False)
        current = self.folder_paths(self.library.metadata()['folders'])
        baseline = self.state['folders']
        on_disk = {key: path for path, key in directories.items() if key}
        # App folder moves rename the whole directory, carrying attachments too.
        for key, old in sorted(baseline.items(), key=lambda pair: pair[1].count('/')):
            wanted, actual = current.get(key), on_disk.get(key)
            if wanted is not None and wanted != old and actual:
                if actual not in (old, wanted):
                    raise ValueError('同一文件夹在两端移动，请先统一位置')
                if actual != wanted:
                    target = self.safe(self.root / wanted)
                    if target.exists():
                        raise ValueError(f'移动目标已存在：{wanted}')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self.safe(self.root / actual).rename(target)
                    directories, _ = self.scan(False)
                    on_disk = {value: path for path, value in directories.items() if value}
        retiring = {key for key in baseline if key not in current}
        # External folder structure is authoritative unless this call deleted it.
        path_ids = {'': ''}
        disk_rows = []
        for path, key in sorted(directories.items(), key=lambda pair: (pair[0].count('/'), pair[0])):
            if key in retiring:
                continue
            key = key or uuid4().hex
            parent = path.rpartition('/')[0]
            if parent not in path_ids:
                continue
            path_ids[path] = key
            disk_rows.append({'id': key, 'name': Path(path).name, 'parent_id': path_ids[parent]})
        validate_tree(disk_rows)
        with self.book.connect() as db, db:
            for row in disk_rows:
                db.execute('INSERT INTO note_folders VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,parent_id=excluded.parent_id', (row['id'], row['name'], row['parent_id']))
            # Missing disk folders are external deletions; new DB folders still
            # need materialization below. Do not interpret a missing vault as deletion.
            missing = set(baseline) - set(on_disk) - retiring
            for key in missing:
                db.execute("UPDATE note_locations SET folder_id='',revision=revision+1 WHERE folder_id=?", (key,))
                db.execute('DELETE FROM note_folders WHERE id=?', (key,))
        current = self.folder_paths(self.library.metadata()['folders'])
        for key, path in current.items():
            if not key:
                continue
            target = self.safe(self.root / path)
            target.mkdir(parents=True, exist_ok=True)
            marker = self.safe(target / MARKER)
            if not marker.exists():
                self.atomic(marker, json.dumps({'id': key}), None)
        return current, retiring

    def seed_courses(self):
        # Preserve the existing per-course placements before switching to files.
        # Remember folder identity so newly generated lectures follow a course
        # folder even after the user has renamed or moved that directory.
        initial = not self.state.get('initialized')
        metadata = self.library.metadata()
        rows = self.rows()
        courses = self.state.setdefault('course_folders', {})
        folders = {folder['id']: folder for folder in metadata['folders']}
        baseline = self.state['notes']
        if not courses and not initial:
            counts = {}
            for row in rows.values():
                folder = baseline.get(row['id'], {}).get('folder_id')
                if row['kind'] == 'course' and folder in folders:
                    counts.setdefault(row['course_id'], {}).setdefault(folder, 0)
                    counts[row['course_id']][folder] += 1
            courses.update({course: max(options, key=options.get) for course, options in counts.items()})
        for row in rows.values():
            if row['kind'] != 'course':
                continue
            if row['folder_id'] or not initial and row['id'] in baseline:
                continue
            course = row['course_id']
            if courses.get(course) not in folders:
                parent = metadata['locations'].get('course-group:' + course, {}).get('folder_id', '')
                if parent not in folders:
                    parent = ''
                name = component(row['source_label'])[:60]
                folder = folders[parent] if parent and folders[parent]['name'] == name else None
                folder = folder or next((item for item in folders.values() if item['parent_id'] == parent and item['name'] == name), None)
                folder = folder or self.library.save_folder(name, parent_id=parent)
                courses[course] = folder['id']
                folders[folder['id']] = folder
            self.location(row['id'], courses[course])

    def reconcile(self):
        self.conflicts = []
        self.seed_courses()
        paths, retiring = self.folders()
        _, files = self.scan()
        rows = self.rows()
        baseline = self.state['notes']
        seen = set()
        moves = {}
        path_ids = {path: key for key, path in paths.items()}
        for path, item in files.items():
            key = item['id']
            if not key:
                # Recover identity if an editor removed only the properties.
                key = next((key for key, old in baseline.items() if old['path'] == path and not old.get('deleted')), None)
            if key in seen:
                raise ValueError('笔记标识重复，已暂停写入')
            if key is None:
                created = self.book.save(None, item['title'], item['body'])
                key = 'personal:' + created['id']
                row = self.library.get('personal', created['id'])
                rows[key] = row
            elif key not in rows:
                # A source can be temporarily unavailable. Never turn its note
                # into a personal duplicate or erase it during a cloud refresh.
                if key.startswith('personal:'):
                    try:
                        deleted = self.book.get(key.split(':', 1)[1])
                    except KeyError:
                        deleted = None
                    if deleted is None and re.fullmatch('[a-f0-9]{32}', key.split(':', 1)[1]):
                        now = datetime.now(timezone.utc).isoformat()
                        with self.book.connect() as db, db:
                            db.execute('INSERT INTO notes (id,title,body,created_at,updated_at) VALUES (?,?,?,?,?)',
                                       (key.split(':', 1)[1], item['title'], item['body'], now, now))
                        rows[key] = self.library.get('personal', key.split(':', 1)[1])
                    if deleted and deleted['deleted']:
                        if baseline.get(key, {}).get('deleted'):
                            self.book.save(deleted['id'], deleted['title'], deleted['body'], deleted['revision'], deleted=False)
                            rows[key] = self.library.get('personal', deleted['id'])
                        else:
                            seen.add(key)
                            self.retire_file(key, item, baseline)
                            continue
                if key not in rows:
                    self.conflicts.append({'id': key, 'path': path, 'reason': '来源暂不可用，文件已保留'})
                    seen.add(key)
                    continue
            row = rows[key]
            seen.add(key)
            old = baseline.get(key)
            if old and path == old['path']:
                item['title'] = old.get('title', item['title'])
            parent = path_ids.get(path.rpartition('/')[0])
            if parent is None:  # App removed this folder: notes will move below.
                parent = row['folder_id']
            # Older application builds may collapse per-lecture locations back
            # into course groups. A missing index row cannot move a vault file.
            if old and old.get('folder_id') and not row['folder_id'] and parent:
                with self.book.connect() as db:
                    indexed = db.execute('SELECT 1 FROM note_locations WHERE reference=?', (key,)).fetchone()
                if not indexed:
                    self.location(key, parent)
                    row = self.library.get(row['kind'], row['source_id'])
                    rows[key] = row
            disk_changed = not old or digest(item['raw']) != old['raw_hash'] or path != old['path']
            app_changed = old and content_hash(row) != old['app_hash']
            if disk_changed and app_changed and (row['title'], row['body']) != (item['title'], item['body']):
                self.conflicts.append({'id': key, 'path': path, 'reason': '两端均有修改，请比较后合并'})
                continue
            if disk_changed:
                if (row['title'], row['body']) != (item['title'], item['body']):
                    self.save_text(row, item['title'], item['body'])
                self.location(key, parent)
                row = self.library.get(row['kind'], row['source_id'])
                rows[key] = row
            # First publication freezes generated text. Later AI outputs remain
            # candidates in the source version selector rather than replacing it.
            if row['kind'] != 'personal' and not row.get('manually_edited'):
                self.save_text(row, row['title'], row['body'])
            wanted = self.note_path(row, paths)
            if old and not old.get('rename') and not disk_changed and row['title'] == old.get('title', Path(old['path']).stem) and row['folder_id'] == old.get('folder_id'):
                wanted = old['path']
            if disk_changed:
                wanted = path
            raw = item['raw'] if item['id'] == key and not app_changed else self.render(row, item)
            if item['id'] != key:
                raw = self.render(row, item)
            if wanted != path:
                target = self.safe(self.root / wanted)
                if target.exists():
                    wanted = self.unique_path(wanted, key)
                # Keep links inside a moved note pointing at their original files.
                row['body'] = self.rebase(row['body'], path, wanted)
                raw = self.render(row, item)
                self.atomic(self.root / wanted, raw, None)
                self.retire_file(key, item, {}, archive=True)
                self.save_text(row, row['title'], row['body'])
            elif raw != item['raw']:
                self.archive(key, item['raw'])
                self.atomic(self.root / path, raw, item['raw'])
            if old and old['path'] != wanted:
                moves[old['path']] = wanted
            self.archive(key, raw)
            baseline[key] = self.entry(row, wanted, raw)
        for key, row in rows.items():
            if key in seen:
                continue
            old = baseline.get(key)
            if old:
                if old.get('deleted'):
                    # A restore through the app marks the tombstone explicitly.
                    continue
                if content_hash(row) != old['app_hash']:
                    self.conflicts.append({'id': key, 'path': old['path'], 'reason': '文件已删除，应用中还有修改，双方版本已保留'})
                    continue
                baseline[key] = {**old, 'deleted': True}
                if row['kind'] == 'personal':
                    self.book.save(row['source_id'], row['title'], row['body'], row['revision'], deleted=True)
                continue
            path = self.unique_path(self.note_path(row, paths), key)
            if row['kind'] != 'personal' and not row.get('manually_edited'):
                self.save_text(row, row['title'], row['body'])
            raw = self.render(row)
            self.atomic(self.root / path, raw, None)
            self.archive(key, raw)
            baseline[key] = self.entry(row, path, raw)
        if moves:
            self.repair_links(moves)
        # App folder deletion removes only empty directories. Attachments survive.
        for key in sorted(retiring, key=lambda key: self.state['folders'][key].count('/'), reverse=True):
            directory = self.safe(self.root / self.state['folders'][key])
            marker = self.safe(directory / MARKER)
            if marker.exists():
                marker.unlink()
            if directory.is_dir():
                try:
                    directory.rmdir()
                except OSError:
                    pass
        self.state['folders'] = {key: path for key, path in paths.items() if key}
        self.state['initialized'] = True
        state_path = self.root / '.sea/state.json'
        text = json.dumps(self.state, ensure_ascii=False, indent=2)
        previous = state_path.read_text('utf-8') if state_path.exists() else None
        if text != previous:
            self.atomic(state_path, text, previous)
            self.generation += 1
        self.library.shared_deleted = {key for key, value in baseline.items() if value.get('deleted')}
        self.library.shared_paths = {key: value['path'] for key, value in baseline.items() if not value.get('deleted')}

    def repair_links(self, moves):
        import posixpath
        blocked = {row['id'] for row in self.conflicts}
        for key, entry in self.state['notes'].items():
            if entry.get('deleted') or key in blocked:
                continue
            path = self.safe(self.root / entry['path'])
            if not path.is_file():
                continue
            raw = path.read_text('utf-8')
            if digest(raw) != entry['raw_hash']:
                raise NoteConflict('笔记已在另一窗口修改，链接更新已暂停')
            front, body = self.parse(raw)
            current = entry['path']
            old_source = next((old for old, new in moves.items() if new == current), current)
            def segment(text):
                def markdown(match):
                    target = match[2]
                    if re.match(r'([a-z]+:|/|#)', target, re.I):
                        return match[0]
                    target, sep, fragment = unquote(target).partition('#')
                    absolute = posixpath.normpath(posixpath.join(posixpath.dirname(current), target))
                    if absolute not in moves and not (self.root / absolute).is_file() and old_source != current:
                        absolute = posixpath.normpath(posixpath.join(posixpath.dirname(old_source), target))
                    destination = moves.get(absolute, absolute)
                    if destination == absolute and absolute not in moves:
                        return match[0]
                    target = posixpath.relpath(destination, posixpath.dirname(current) or '.')
                    return match[1] + quote(target, safe='/._-') + (sep + fragment if sep else '') + match[3]
                text = re.sub(r'(!?\[[^\]]*\]\()([^\s)]+)(\))', markdown, text)
                def wiki(match):
                    target, alias_sep, alias = match[2].partition('|')
                    target, anchor_sep, anchor = target.partition('#')
                    choices = [new for old, new in moves.items() if target in (old, old.removesuffix('.md'), Path(old).stem)]
                    if len(choices) != 1:
                        return match[0]
                    destination = choices[0].removesuffix('.md')
                    return match[1] + '[[' + destination + (anchor_sep + anchor if anchor_sep else '') + (alias_sep + alias if alias_sep else '') + ']]'
                return re.sub(r'(!?)\[\[([^\]]+)\]\]', wiki, text)
            updated = ''.join(chunk if index % 2 else segment(chunk) for index, chunk in enumerate(re.split(r'(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)', body)))
            if updated == body:
                continue
            row = self.library.get(*key.split(':', 1))
            self.save_text(row, row['title'], updated)
            row = self.library.get(*key.split(':', 1))
            rendered = self.render(row, {'front': front})
            self.archive(key, raw)
            self.atomic(path, rendered, raw)
            self.state['notes'][key] = self.entry(row, current, rendered)

    def note_path(self, row, paths):
        return '/'.join(filter(None, [paths.get(row['folder_id'], ''), component(row['title']) + '.md']))

    def unique_path(self, path, key):
        target = self.safe(self.root / path)
        if not target.exists():
            return path
        return str(Path(path).with_name(Path(path).stem + ' [' + digest(key)[:8] + '].md'))

    @staticmethod
    def entry(row, path, raw):
        return {'path': path, 'raw_hash': digest(raw), 'app_hash': content_hash(row), 'folder_id': row['folder_id'], 'title': row['title'], 'deleted': False}

    def archive(self, key, raw):
        target = self.root / '.sea/history' / digest(key) / (digest(raw) + '.md')
        if not self.safe(target).exists():
            self.atomic(target, raw, None)

    def retire_file(self, key, item, baseline, archive=True):
        path = self.safe(self.root / item['path'])
        if archive:
            self.archive(key, item['raw'])
        if path.read_text('utf-8') != item['raw']:
            raise NoteConflict('Obsidian 正在修改此笔记，删除已暂停')
        path.unlink()
        if baseline.get(key):
            baseline[key]['deleted'] = True

    @staticmethod
    def rebase(body, old, new):
        # Rebase relative Markdown targets outside fenced/inline code. Folder
        # renames carry attachments in place and do not need this transformation.
        import posixpath
        def part(text):
            def link(match):
                target = match[2]
                if re.match(r'([a-z]+:|/|#)', target, re.I):
                    return match[0]
                path, separator, fragment = unquote(target).partition('#')
                absolute = posixpath.normpath(posixpath.join(posixpath.dirname(old), path))
                relative = posixpath.relpath(absolute, posixpath.dirname(new) or '.')
                return match[1] + quote(relative, safe='/._-') + (separator + fragment if separator else '') + match[3]
            return re.sub(r'(!?\[[^\]]*\]\()([^\s)]+)(\))', link, text)
        return ''.join(chunk if index % 2 else part(chunk) for index, chunk in enumerate(re.split(r'(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)', body)))

    def detail(self, row):
        if not self.enabled:
            return row
        path = self.library.shared_paths.get(row['id'])
        return {**row, 'shared_path': path, 'obsidian_url': 'obsidian://open?path=' + quote(str(self.root / path), safe='') if path else None}

    def restore(self, key):
        old = self.state['notes'].get(key)
        if not old or not old.get('deleted'):
            raise ValueError('笔记不在共享回收站')
        kind, source_id = key.split(':', 1)
        if kind == 'personal':
            row = self.book.get(source_id)
            self.book.save(source_id, row['title'], row['body'], row['revision'], deleted=False)
        self.library.shared_deleted.discard(key)
        row = self.library.get(kind, source_id)
        paths = self.folder_paths(self.library.metadata()['folders'])
        path = self.unique_path(self.note_path(row, paths), key)
        archived = self.safe(self.root / '.sea/history' / digest(key) / (old['raw_hash'] + '.md'))
        front = self.parse(archived.read_text('utf-8'))[0] if archived.is_file() else {}
        raw = self.render(row, {'front': front})
        self.atomic(self.root / path, raw, None)
        self.state['notes'][key] = self.entry(row, path, raw)
        self.reconcile()

    def asset(self, note_id, target):
        path = self.library.shared_paths.get(note_id)
        if not path:
            raise KeyError(note_id)
        candidate = self.root / Path(path).parent / unquote(target)
        candidate = Path(os.path.abspath(candidate))
        self.safe(candidate)
        if candidate.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
            raise ValueError('仅可读取共享目录内的位图附件')
        if not candidate.is_file():
            # Obsidian supports vault-wide shortest attachment names.
            matches = [self.safe(file) for file in self.root.rglob(candidate.name)
                       if file.is_file() and not any(part.startswith('.') for part in file.relative_to(self.root).parts)]
            if len(matches) != 1:
                raise KeyError(target)
            candidate = matches[0]
        return candidate

    def save_personal(self, note_id, title, body, revision=0, **flags):
        result = self.book.save(note_id, title, body, revision, **flags)
        key = 'personal:' + result['id']
        if self.enabled and flags.get('deleted') is False and self.state['notes'].get(key, {}).get('deleted'):
            del self.state['notes'][key]
            self.library.shared_deleted.discard(key)
        return result

    def delete(self, key):
        row = self.library.get(*key.split(':', 1))
        old = self.state['notes'].get(key)
        if not old or old.get('deleted'):
            raise ValueError('笔记已在回收站中')
        path = self.safe(self.root / old['path'])
        raw = path.read_text('utf-8')
        if digest(raw) != old['raw_hash']:
            raise NoteConflict('笔记已在另一窗口修改，删除已暂停')
        self.retire_file(key, {'path': old['path'], 'raw': raw}, self.state['notes'])
        self.library.shared_deleted.add(key)
        if row['kind'] == 'personal':
            self.book.save(row['source_id'], row['title'], row['body'], row['revision'], deleted=True)
        self.reconcile()
        return {'deleted': True}

    def conflict(self, key):
        row = self.library.get(*key.split(':', 1))
        entry = next((item for item in self.conflicts if item['id'] == key), None)
        if not entry:
            raise ValueError('该笔记没有待处理冲突')
        path = self.safe(self.root / entry['path'])
        if not path.is_file():
            raise ValueError('文件已删除；可从回收站恢复或在原路径放回文件')
        raw = path.read_text('utf-8')
        _, body = self.parse(raw)
        return {'id': key, 'path': entry['path'], 'file': {'title': path.stem, 'body': body},
                'app': {'title': row['title'], 'body': row['body']}, 'raw_hash': digest(raw), 'app_hash': content_hash(row)}

    def resolve(self, key, title, body, raw_hash, app_hash):
        conflict = self.conflict(key)
        if conflict['raw_hash'] != raw_hash or conflict['app_hash'] != app_hash:
            raise NoteConflict('笔记已在另一窗口修改，请重新比较后合并')
        row = self.library.get(*key.split(':', 1))
        path = self.safe(self.root / conflict['path'])
        raw = path.read_text('utf-8')
        front, _ = self.parse(raw)
        self.save_text(row, title, body)
        row = self.library.get(*key.split(':', 1))
        self.archive(key, raw)
        updated = self.render(row, {'front': front})
        self.atomic(path, updated, raw)
        self.state['notes'][key] = {**self.entry(row, conflict['path'], updated), 'rename': True}
        return row

    def link(self, note_id, target):
        import posixpath
        path = self.library.shared_paths.get(note_id)
        if not path:
            raise KeyError(note_id)
        value, _, heading = unquote(target).partition('#')
        value = value.removesuffix('.md')
        relative = posixpath.normpath(posixpath.join(posixpath.dirname(path), value))
        candidates = [(key, file) for key, file in self.library.shared_paths.items()
                      if file.removesuffix('.md') in (value, relative)]
        if not candidates:
            candidates = [(key, file) for key, file in self.library.shared_paths.items() if Path(file).stem == value]
        if len(candidates) != 1:
            raise ValueError('链接目标不存在或有重名，请在 Obsidian 中检查路径')
        return {'id': candidates[0][0], 'heading': heading}
