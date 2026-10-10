from contextlib import contextmanager
import json
import sqlite3

import pytest

from local_web.state import RepositorySettings, RuntimeCredentials
from scripts import local_course as runner
from scripts import sync_local_course as sync
from src.data.database import Database


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'RUN_DIR', tmp_path)
    lecture = {'sub_id': '101945', 'date': '2024-04-10',
               'sub_title': '第一讲', 'has_playback': True}
    pending = {**lecture, 'sub_id': '101947', 'date': '2024-04-24'}
    manifest = {'course_id': '4048', 'title': runner.TITLE, 'teacher': runner.TEACHER,
                'lectures': [lecture, pending]}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    db = Database(str(tmp_path / 'source.db'))
    db.upsert_course('4048', runner.TITLE, runner.TEACHER)
    db.upsert_course('2', '无关课程', '其他老师')
    db.insert_lecture('101945', '4048', '第一讲', '2024-04-10')
    db.insert_lecture('101947', '4048', '未完成', '2024-04-24')
    db.insert_lecture('20', '2', '无关笔记', '2024-04-10')
    db.update_transcript('101945', '完整转录')
    db.update_summary('101945', '原笔记正文', 'Mimo/model')
    db.update_summary('20', '无关笔记正文', 'other/model')
    runner.save_meta(db, '101945', 'audio', {'expected_seconds': 100, 'decoded_seconds': 100})
    runner.save_meta(db, '101945', 'ocr', {'source_pages': 0, 'kept_pages': 0})
    request = runner.summary_request(db, lecture)
    receipt = {**request, 'text': '# 笔记标题\n\n原笔记正文',
               'model': 'Mimo/model', 'finish_reason': 'stop'}
    results = tmp_path / 'summary-results'
    results.mkdir()
    (results / '101945.json').write_text(json.dumps(receipt))
    try:
        yield db, manifest, receipt
    finally:
        db.conn.close()


def test_sync_delta_contains_only_verified_completed_course_notes(source, tmp_path):
    db, manifest, _ = source
    target = tmp_path / 'delta.db'
    notes, fingerprint = sync.verified_delta(db, manifest['lectures'], target)
    assert [row['sub_id'] for row in notes] == ['101945']
    assert len(fingerprint) == 64
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT sub_id FROM lectures').fetchall() == [('101945',)]
        assert conn.execute('SELECT course_id FROM courses').fetchall() == [('4048',)]
    assert db.conn.execute('SELECT COUNT(*) FROM lectures').fetchone()[0] == 3


def test_sync_rejects_a_receipt_for_different_source_material(source, tmp_path):
    db, manifest, receipt = source
    receipt['prompt_sha256'] = 'wrong'
    (tmp_path / 'summary-results/101945.json').write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='材料不符'):
        sync.verified_delta(db, manifest['lectures'], tmp_path / 'delta.db')


def test_unchanged_notes_publish_and_refresh_only_once(source, monkeypatch):
    db, _, _ = source
    settings = RepositorySettings('test', 'repo', 'data')
    calls = []

    @contextmanager
    def snapshot(_):
        yield db

    def local_json(base, path, method='GET'):
        calls.append((path, method))
        if path.endswith('/status'):
            return {'configured': True, 'repository': settings.as_dict()}
        if path.endswith('/sync'):
            return {}
        return {'summary': '原笔记正文', 'summary_model': 'Mimo/model',
                'summary_versions': [], 'transcript': '完整转录'}

    def publish(*args, **kwargs):
        calls.append(('publish', kwargs['branch']))
        return 'test-commit'

    monkeypatch.setattr(runner, 'read_snapshot', snapshot)
    monkeypatch.setattr(sync, 'local_json', local_json)
    monkeypatch.setattr(sync, 'publish_database', publish)
    creds = RuntimeCredentials('test-token', 'student', 'password')
    first = sync.sync_once(creds, settings, 'http://127.0.0.1:8766', object())
    second = sync.sync_once(creds, settings, 'http://127.0.0.1:8766', object())
    assert first['changed'] and not second['changed']
    assert calls.count(('publish', 'data')) == 1
    assert calls.count(('/api/local/sync', 'POST')) == 1


def test_cloud_commit_survives_local_refresh_timeout(source, monkeypatch):
    db, _, _ = source
    settings = RepositorySettings('test', 'repo', 'data')
    calls = []

    @contextmanager
    def snapshot(_):
        yield db

    def local_json(base, path, method='GET'):
        if path.endswith('/status'):
            return {'configured': True, 'repository': settings.as_dict()}
        if path.endswith('/sync'):
            calls.append('refresh')
            if calls.count('refresh') == 1:
                raise TimeoutError('network')
            return {}
        return {'summary': '原笔记正文', 'summary_model': 'Mimo/model',
                'summary_versions': [], 'transcript': '完整转录'}

    def publish(*args, **kwargs):
        calls.append('publish')
        return 'test-commit'

    monkeypatch.setattr(runner, 'read_snapshot', snapshot)
    monkeypatch.setattr(sync, 'local_json', local_json)
    monkeypatch.setattr(sync, 'publish_database', publish)
    monkeypatch.setattr(sync.time, 'sleep', lambda _: None)
    result = sync.sync_with_retries(RuntimeCredentials('token', 'student', 'password'),
                                    settings, 'http://127.0.0.1:8766', object())
    assert result['changed']
    assert calls.count('publish') == 1
    assert calls.count('refresh') == 2


def test_sync_validation_errors_are_not_retried(monkeypatch):
    calls = []

    def invalid(*args):
        calls.append('invalid')
        raise ValueError('材料不符')

    monkeypatch.setattr(sync, 'sync_once', invalid)
    with pytest.raises(ValueError, match='材料不符'):
        sync.sync_with_retries(None, None, '', None)
    assert calls == ['invalid']
