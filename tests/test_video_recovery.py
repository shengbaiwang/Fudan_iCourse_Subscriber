"""Availability failures must not strand lectures or persist partial transcripts."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from src.api.icourse import ICourseClient, VideoAccessError, VideoLookupError, VideoNotReadyError
from src.data.database import Database
from src.data.sharder import shard_database, reassemble_database
from src.pipeline.lecture_runner import LectureRunner
from src.runtime import config
from src.runtime.scheduler import AudioDownloader
from scripts.merge_db import merge


def client(info, detail=None):
    value = ICourseClient(Mock())
    value.get_sub_info = Mock(return_value=info)
    value.get_sub_detail = Mock(return_value=detail or {})
    value.sign_video_url = Mock(side_effect=lambda url, now: url)
    return value


@pytest.mark.parametrize('source', [
    {'video_list': [{'preview_url': 'https://cdn.example/lecture.MP4?token=abc'}]},
    {'video_list': json.dumps({'1': {'preview_url': '//cdn.example/lecture.MP4?token=abc'}})},
    {'playurl': ['https://cdn.example/lecture.MP4?token=abc']},
    {'content': json.dumps({'playback': [{'url': 'https://cdn.example/lecture.MP4?token=abc'}]})},
])
def test_video_query_list_and_json_variants(source):
    value = client(dict(source, now='invalid-clock'))
    assert value.get_video_url('1', '10') == 'https://cdn.example/lecture.MP4?token=abc'
    value.sign_video_url.assert_called_once_with('https://cdn.example/lecture.MP4?token=abc', now=None)
    value.get_sub_detail.assert_not_called()


def test_permission_denial_is_explicit_and_never_signed():
    value = client({'can_watch': False, 'watch_rule': 'not_student',
                    'content': {'playback': {'url': 'https://cdn.example/lecture.mp4'}}})
    with pytest.raises(VideoAccessError, match='未选'):
        value.get_video_url('1', '10')
    value.get_sub_detail.assert_not_called()
    value.sign_video_url.assert_not_called()


def test_empty_sources_differ_from_failed_metadata():
    value = client({'video_list': {'now': 123}, 'playurl': {'now': 123},
                    'content': {'process_type': 'processing', 'playback': []}})
    with pytest.raises(VideoNotReadyError, match='处理中'):
        value.get_video_url('1', '10')
    value.get_sub_info.side_effect = ValueError('secret signed URL must not be logged')
    value.get_sub_detail.side_effect = RuntimeError('private-cookie')
    with pytest.raises(VideoLookupError) as error:
        value.get_video_url('1', '10')
    assert 'secret' not in str(error.value) and 'private-cookie' not in str(error.value)
    assert 'ValueError' in str(error.value)


def test_detail_can_recover_a_transient_info_failure():
    value = client({}, {'content': {'playback': {'url': 'https://cdn.example/lecture.mp4'}}})
    value.get_sub_info.side_effect = TimeoutError()
    assert value.get_video_url('1', '10') == 'https://cdn.example/lecture.mp4'


def test_audio_spawn_preserves_failure_and_does_not_refetch(tmp_path):
    downloader = AudioDownloader(str(tmp_path), max_concurrent=1)
    value = SimpleNamespace(get_video_url=Mock(side_effect=VideoAccessError('未选课')))
    downloader.schedule(value, '1', '10')
    with pytest.raises(VideoAccessError):
        downloader.get('10', timeout=2)
    downloader.schedule(value, '1', '10')
    with pytest.raises(VideoAccessError):
        downloader.get('10', timeout=2)
    assert value.get_video_url.call_count == 1
    downloader.release('10')
    value.get_video_url = Mock(return_value=None)
    downloader.schedule(value, '1', '11')
    assert downloader.get('11', timeout=2) is None
    downloader.release('11')


@pytest.fixture
def db(tmp_path):
    value = Database(str(tmp_path / 'course.db'))
    value.upsert_course('1', '课程', '老师')
    value.insert_lecture('10', '1', '第一课', '2026-09-11')
    yield value
    value.conn.close()


@pytest.mark.parametrize('paused', [True, False])
def test_legacy_no_video_backlog_revives_and_cooldown_cannot_be_bypassed(db, monkeypatch, paused):
    monkeypatch.setattr(config, 'COURSE_IDS', ['1'])
    monkeypatch.setattr(config, 'AUTO_CHECK_PAUSES', {'1': 'paused'} if paused else {})
    monkeypatch.setattr(config, 'LECTURE_ORDER', 'api')
    with db.conn:
        db.conn.execute("UPDATE lectures SET error_stage='no_video', error_count=78")
    if paused:
        db.write_meta('auto_check_pause_scans', '{"1":"paused"}')
    value = Mock()
    value.check_alive.return_value = True
    value.get_course_detail.return_value = {'title':'课程', 'teacher':'老师', 'lectures':[
        {'sub_id':'10', 'sub_title':'第一课', 'date':'2026-09-11', 'has_playback':True}]}
    assert [row[2]['sub_id'] for row in main._enumerate_lectures(value, db, Mock())] == ['10']
    db.update_error('10', 'no_video', '等待发布')
    retry_at = db.get_lecture('10')['retry_after']
    assert db.get_exhausted_sub_ids('1') == set()
    assert main._enumerate_lectures(value, db, Mock()) == []
    monkeypatch.setattr('src.data.database.time.time', lambda: retry_at + 1)
    assert [row[2]['sub_id'] for row in main._enumerate_lectures(value, db, Mock())] == ['10']


def make_runner(db, error, segments, duration=3600):
    with db.conn:
        db.conn.execute("INSERT INTO ppt_pages(sub_id,page_num,created_sec,pptimgurl) VALUES ('10',1,?,'https://ppt.example/1.jpg')", (duration,))
    downloader = Mock()
    downloader.get.side_effect = error
    value = LectureRunner(SimpleNamespace(get_transcript_segments=lambda _:segments), db,
        SimpleNamespace(audio_downloader=downloader), Mock(), Mock(), Mock())
    return value


def full_segments():
    return [{'start_ms':n*60000, 'end_ms':(n+1)*60000, 'text':'课堂完整文字内容' * 10} for n in range(60)]


def test_restricted_video_uses_checked_official_text_with_asr_preferred(db, monkeypatch):
    monkeypatch.setattr(config, 'USE_OFFICIAL_TRANSCRIPT', False)
    value = make_runner(db, VideoAccessError('未选课'), full_segments())
    text, segments = value._get_transcript(db.get_lecture('10'), '1', '10')
    assert text and len(segments) == 60
    assert db.get_lecture('10')['transcript'] == text
    value._transcriber.transcribe_tail.assert_not_called()


@pytest.mark.parametrize('kind', ['empty','short','tail','head','gap','no_duration'])
def test_recovery_rejects_incomplete_or_unverifiable_official_text(db, monkeypatch, kind):
    monkeypatch.setattr(config, 'USE_OFFICIAL_TRANSCRIPT', False)
    segments = full_segments()
    duration = 3600
    if kind == 'empty': segments = []
    if kind == 'short': segments = [{**s, 'text':''} for s in segments]
    if kind == 'tail': segments = segments[:30]
    if kind == 'head': segments = segments[10:]
    if kind == 'gap': segments = segments[:10] + segments[35:]
    if kind == 'no_duration': duration = 0
    value = make_runner(db, VideoAccessError('未选课'), segments, duration)
    assert value._get_transcript(db.get_lecture('10'), '1', '10') == (None, None)
    row = db.get_lecture('10')
    assert not row['transcript'] and row['error_stage'] == 'video_access' and row['retry_after']


def test_api_failure_is_recorded_as_retryable_video_error(db, monkeypatch):
    monkeypatch.setattr(config, 'USE_OFFICIAL_TRANSCRIPT', False)
    value = make_runner(db, VideoLookupError('临时异常'), full_segments())
    assert value._get_transcript(db.get_lecture('10'), '1', '10') == (None, None)
    assert db.get_lecture('10')['error_stage'] == 'video'


def test_cooldown_survives_shards_merge_and_success_clears_it(db, tmp_path):
    db.update_error('10', 'no_video', '等待发布')
    retry_at = db.get_lecture('10')['retry_after']
    directory = tmp_path / 'shards'
    index = shard_database(db.db_path, str(directory), 'synthetic-password')
    restored = tmp_path / 'restored.db'
    reassemble_database(index, str(directory / 'shards'), str(restored), 'synthetic-password')
    merge(db.db_path, str(restored))
    with sqlite3.connect(restored) as conn:
        assert conn.execute('SELECT retry_after FROM lectures').fetchone()[0] == retry_at
    db.update_transcript('10', '完整转录')
    db.update_summary('10', '恢复笔记', 'test/model')
    db.mark_processed('10')
    db.clear_error('10')
    merge(db.db_path, str(restored))
    with sqlite3.connect(restored) as conn:
        assert conn.execute('SELECT error_stage, error_count, retry_after FROM lectures').fetchone() == (None, 0, None)
        assert conn.execute('SELECT summary FROM lectures').fetchone()[0] == '恢复笔记'


def test_class_break_does_not_look_like_head_or_tail_truncation(db, monkeypatch):
    monkeypatch.setattr(config, 'USE_OFFICIAL_TRANSCRIPT', False)
    segments = full_segments()
    segments = segments[:20] + segments[28:]
    value = make_runner(db, VideoAccessError('未选课'), segments)
    assert value._get_transcript(db.get_lecture('10'), '1', '10')[0]


def test_local_recovery_persists_official_source_checkpoint(db, monkeypatch):
    from scripts.local_pipeline import LocalLectureRunner
    monkeypatch.setattr(config, 'USE_OFFICIAL_TRANSCRIPT', False)
    make_runner(db, VideoAccessError('未选课'), [])  # register the duration hint
    metadata = {}
    local = SimpleNamespace(
        meta=lambda _db, sid, kind:metadata.get((sid,kind)),
        save_meta=lambda _db, sid, kind, value:metadata.update({(sid,kind):value}))
    downloader = Mock()
    downloader.get.side_effect = VideoAccessError('未选课')
    checkpoint = Mock()
    value = LocalLectureRunner(SimpleNamespace(get_transcript_segments=lambda _:full_segments()), db,
        SimpleNamespace(audio_downloader=downloader), Mock(), Mock(),
        checkpoint=checkpoint, runner=local, summary_worker=None)
    assert value._get_transcript(db.get_lecture('10'), '1', '10')[0]
    assert metadata['10','audio']['asr_backend'] == 'official'
    assert len(metadata['10','segments']) == 60
    checkpoint.assert_called_once()
    value._transcriber.transcribe_tail.assert_not_called()


@pytest.mark.parametrize('cancelled', [False, True])
def test_local_spawn_failures_retry_but_cancellation_propagates(db, cancelled):
    from scripts.local_pipeline import LocalLectureRunner
    downloader = Mock()
    downloader.get.side_effect = InterruptedError('停止') if cancelled else OSError('private-url')
    checkpoint = Mock()
    value = LocalLectureRunner(Mock(), db, SimpleNamespace(audio_downloader=downloader), Mock(), Mock(),
        checkpoint=checkpoint, runner=SimpleNamespace(meta=lambda *_:None), summary_worker=None)
    if cancelled:
        with pytest.raises(InterruptedError):
            value._get_transcript(db.get_lecture('10'), '1', '10')
        checkpoint.assert_not_called()
    else:
        assert value._get_transcript(db.get_lecture('10'), '1', '10') == (None, None)
        assert db.get_lecture('10')['error_stage'] == 'video'
        assert 'private-url' not in db.get_lecture('10')['error_msg']
        checkpoint.assert_called_once()


def test_new_processing_stage_gets_its_own_attempt_budget(db, tmp_path):
    remote = tmp_path / 'remote.db'
    for _ in range(4): db.update_error('10', 'no_video', '等待录播')
    with sqlite3.connect(remote) as conn:
        db.conn.backup(conn)
    db.update_error('10', 'summarize', '模型暂时失败')
    assert db.get_lecture('10')['error_count'] == 1
    merge(db.db_path, str(remote))
    with sqlite3.connect(remote) as conn:
        assert conn.execute('SELECT error_stage,error_count,retry_after FROM lectures').fetchone() == ('summarize',1,None)


def test_resigning_retains_business_query_and_replaces_old_auth():
    from urllib.parse import urlparse, parse_qs
    value = ICourseClient(Mock())
    value.get_userinfo = lambda: {'id':123,'tenant_id':4,'phone':'12345'}
    url = value.sign_video_url('https://cdn.example/lecture.mp4?quality=hd&t=old&clientUUID=old#marker', now=100)
    query = parse_qs(urlparse(url).query)
    assert query['quality'] == ['hd']
    assert len(query['t']) == len(query['clientUUID']) == 1
    assert query['t'][0].startswith('123-100-')
    assert query['clientUUID'] != ['old']


def test_explicit_manual_processing_can_recheck_before_cooldown(db, monkeypatch):
    monkeypatch.setattr(config, 'COURSE_IDS', ['1'])
    monkeypatch.setattr(config, 'AUTO_CHECK_PAUSES', {})
    monkeypatch.setattr(config, 'LECTURE_ORDER', 'api')
    db.update_error('10','video_access','等待观看权限')
    value = Mock()
    value.check_alive.return_value = True
    value.get_course_detail.return_value = {'title':'课程','teacher':'老师','lectures':[
        {'sub_id':'10','sub_title':'第一课','date':'2026-09-11','has_playback':True}]}
    assert main._enumerate_lectures(value, db, Mock()) == []
    assert [row[2]['sub_id'] for row in main._enumerate_lectures(value, db, Mock(), force_video_recheck=True)] == ['10']
