"""Serve synthetic notes in an isolated library; never opens real accounts."""
import os
from pathlib import Path
import socket
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_web.database import DatabaseManager
from local_web.notebook import Notebook
from local_web.note_library import NoteLibrary
from local_web.server import create_app
from local_web.state import RuntimeState, SettingsStore
from local_web.talks import TalkStore
from src.data.database import Database


class NoCredentials:
    available = False
    def load(self, _settings): return None


def fixture(root, course_groups=False):
    database = DatabaseManager(root / 'cache')
    source = Database(str(database.db_path))
    source.upsert_course('history', '近代中国史', '测试教师')
    source.insert_lecture('one', 'history', '第一讲', '2026-10-27')
    source.update_transcript('one', '课堂转写：口述史与档案如何相互印证。')
    source.update_summary('one', '课程旧版本正文', 'fixture/old')
    source.update_summary('one', '# 历史与记忆\n\n课程当前正文，跨来源检索词：档案。\n\n**市场定价未必理性。**他继续解释。\n\n## 口述材料\n\n- 记录\n- 比较', 'fixture/current')
    source.insert_lecture('pending', 'history', '尚未生成', '2026-10-27')
    if course_groups:
        source.insert_lecture('two', 'history', '第二讲', '2026-10-28')
        source.update_summary('two', '# 城市与工业\n\n第二课次关键词：工业化与城市。', 'fixture/current')
        source.upsert_course('other', '世界史', '另一位教师')
        source.insert_lecture('other-one', 'other', '第一讲', '2026-10-27')
        source.update_summary('other-one', '另一门课程的独立笔记。', 'fixture/current')
    source.conn.close()
    notebook = Notebook(root)
    personal = notebook.save(None, '我的研究札记', '个人的想法与档案线索。')
    talks = TalkStore(root / 'library')
    talk = talks.create_talk('城市空间讲座', '讲座转写：空间与社会关系。')
    talks.save_summary(talk['id'], '讲座旧版本正文', '城市空间', 'fixture/old')
    talks.save_summary(talk['id'], '讲座当前正文，关于档案与城市。', '城市空间', 'fixture/current')
    talks.create_talk('未生成笔记的讲座', '只有转写，还没有摘要。')
    state = RuntimeState(store=SettingsStore(root), credential_store=NoCredentials())
    library = NoteLibrary(notebook, database, talks, lambda: state.lecture_names)
    app = create_app(state=state, database=database, notebook=notebook, talk_store=talks)
    return app, library, personal, talk, database


if __name__ == '__main__':
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='sea-note-library-') as directory:
        root = Path(directory)
        os.environ.update(SEA_DISABLE_KEYCHAIN='1', ICOURSE_WEB_CONFIG_DIR=directory, ICOURSE_WEB_CACHE_DIR=str(root / 'cache'))
        os.environ.pop('SEA_DESKTOP_TOKEN', None)
        app, _, _, _, database = fixture(root, course_groups=os.environ.get('SEA_TEST_COURSE_GROUPS') == '1')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        print(f'READY http://127.0.0.1:{port}', flush=True)
        try:
            uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')
        finally:
            database.close()
