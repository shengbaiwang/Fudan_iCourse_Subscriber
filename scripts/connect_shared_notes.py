"""Preview or connect the local notebook to a Markdown vault, without network I/O.

Reads the existing local credentials only to unlock the encrypted course library.
Never prints secrets and never calls a cloud service. Apply backs up notebook
SQLite before materializing files; source media and encrypted course stores stay
in their original locations.
"""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_web.database import DatabaseManager
from local_web.notebook import Notebook
from local_web.note_library import NoteLibrary
from local_web.shared_notes import SharedNotes
from local_web.state import RuntimeState, default_config_dir
from local_web.talks import TalkStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.directory.expanduser().absolute()
    if not root.is_dir() or root.is_symlink():
        raise SystemExit('目标必须是已经存在的本地目录')
    profile = default_config_dir()
    source = profile / 'library/notebook.sqlite3'
    backup = None
    if args.apply and source.is_file():
        backup = root / '.sea/backups' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-notebook.sqlite3')
        for parent in (root / '.sea', root / '.sea/backups'):
            if parent.is_symlink():
                raise SystemExit('备份目录不能是符号链接')
            parent.mkdir(exist_ok=True)
        with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as original, sqlite3.connect(backup) as destination:
            original.backup(destination)
        backup.chmod(0o600)
    runtime = RuntimeState()
    database = DatabaseManager()
    try:
        if database.persistent_db_path.exists():
            if runtime.credentials is None:
                raise SystemExit('课程资料库尚未解锁，请先在 Dev 中连接本机资料')
            database.unlock_persistent(runtime.credentials)
        book = Notebook(profile)
        library = NoteLibrary(book, database, TalkStore(), lambda: dict(runtime.lecture_names))
        shared = SharedNotes(library, root)
        rows = shared.rows()
        result = {'directory': str(root), 'notes': len(rows), 'sources': dict(Counter(row['kind'] for row in rows.values())),
                  'existing_markdown': len(list(root.glob('**/*.md'))), 'applied': args.apply}
        if args.apply:
            shared.call(lambda: None)
            result.update(status=shared.status(), published=len(library.shared_paths), backup=str(backup or ''))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        database.close()


if __name__ == '__main__':
    main()
