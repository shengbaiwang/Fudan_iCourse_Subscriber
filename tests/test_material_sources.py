"""Independent media/text/slide availability, including integrity failures."""
import json
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.api.icourse import ICourseClient, VideoAccessError
from src.api.materials import recording_duration, transcript_problem
from src.data.database import Database
from src.pipeline.lecture_runner import LectureRunner
from src.runtime import config


def full_segments():
    return [{'start_ms':n*60000, 'end_ms':(n+1)*60000, 'text':'完整课堂文字' * 20} for n in range(60)]


@pytest.mark.parametrize('payload,seconds', [
    ({'video_duration_seconds': 3600}, 3600),
    ({'video_duration_ms': 3600000}, 3600),
    ({'content': json.dumps({'playback': {'duration_sec': 3600}})}, 3600),
    ({'video_list': {'1': {'duration': '01:00:00.500'}}}, 3600.5),
    ({'video_list': [{'duration': 3600000, 'duration_unit': 'ms'}]}, 3600),
    ({'video_list': [{'duration_seconds': 3500}, {'duration_seconds': 3600}]}, 3600),
])
def test_independent_duration_units_and_multiple_streams(payload, seconds):
    evidence = recording_duration(payload)
    assert evidence['seconds'] == seconds and evidence['source'].startswith('metadata.')


@pytest.mark.parametrize('payload', [
    {'duration': 3600}, {'start_time': 100, 'end_time': 3700},
    {'video_list': [{'duration': 3600000}]},
    {'all_content': [{'BeginSec': 0, 'EndSec': 3600}]},
    {'video_duration_seconds': True}, {'video_duration_seconds': 'nan'},
    {'video_duration_seconds': -1}, {'video_duration_seconds': 'inf'},
    {'content': {'playback': {'duration': '00:99:00'}}},
])
def test_unknown_units_schedules_and_transcript_tail_are_not_duration(payload):
    assert recording_duration(payload) is None


def test_metadata_can_verify_text_when_video_is_denied_without_signing():
    value = ICourseClient(Mock())
    value.get_sub_info = Mock(return_value={'can_watch': False, 'watch_rule':'not_student',
        'content': {'playback': {'duration_ms':3600000, 'url':'https://example/secret.mp4'}}})
    value.get_sub_detail = Mock()
    value.sign_video_url = Mock()
    with pytest.raises(VideoAccessError):
        value.get_video_url('1', '10')
    assert value.get_recording_duration('1', '10')['seconds'] == 3600
    value.get_sub_info.assert_called_once()
    value.get_sub_detail.assert_not_called()
    value.sign_video_url.assert_not_called()


@pytest.mark.parametrize('kind', ['tail', 'head', 'gap', 'beyond', 'negative', 'zero', 'unordered', 'nan'])
def test_bad_official_timing_never_passes_independent_duration(kind):
    segments = full_segments()
    if kind == 'tail': segments = segments[:30]
    if kind == 'head': segments = segments[10:]
    if kind == 'gap': segments = segments[:10] + segments[35:]
    if kind == 'beyond': segments[-1]['end_ms'] = 7200000
    if kind == 'negative': segments[0]['start_ms'] = -1
    if kind == 'zero': segments[0]['end_ms'] = 0
    if kind == 'unordered': segments[0], segments[1] = segments[1], segments[0]
    if kind == 'nan': segments[0]['start_ms'] = float('nan')
    assert transcript_problem(segments, 3600)


def test_screenshot_duration_cannot_rescue_unknown_recording_length(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'USE_OFFICIAL_TRANSCRIPT', False)
    db = Database(str(tmp_path/'test.db'))
    try:
        db.upsert_course('1','课','老师')
        db.insert_lecture('10','1','第一讲','2026-09-01')
        db.insert_ppt_pages_pending('10', [{'page_num':1, 'created_sec':3600, 'pptimgurl':'https://example/1.png'}])
        audio = Mock()
        audio.get.side_effect = VideoAccessError('未选课')
        value = LectureRunner(SimpleNamespace(get_transcript_segments=lambda _:full_segments(),
            get_recording_duration=lambda *_:None), db, SimpleNamespace(audio_downloader=audio), Mock(), Mock(), Mock())
        assert value._get_transcript(db.get_lecture('10'),'1','10') == (None,None)
        row = db.get_lecture('10')
        assert row['transcript'] is None and '独立录制时长' in row['error_msg']
    finally:
        db.conn.close()


@pytest.mark.parametrize('ppt_error', ['prepare', 'drain', 'partial'])
def test_actions_generates_verified_text_note_despite_optional_slide_failures(tmp_path, monkeypatch, ppt_error):
    monkeypatch.setattr(config, 'USE_OFFICIAL_TRANSCRIPT', False)
    db = Database(str(tmp_path/'test.db'))
    try:
        db.upsert_course('1','课','老师')
        db.insert_lecture('10','1','第一讲','2026-09-01')
        audio = Mock()
        audio.get.side_effect = VideoAccessError('未选课')
        summarizer = Mock()
        summarizer.summarize.return_value = ('# 标题\n\n完整笔记', 'test/model')
        value = LectureRunner(SimpleNamespace(get_transcript_segments=lambda _:full_segments(),
            get_recording_duration=lambda *_:{'seconds':3600,'source':'sub-info.video_duration_seconds'}),
            db, SimpleNamespace(audio_downloader=audio), Mock(), summarizer, Mock())
        value._ppt = Mock()
        if ppt_error == 'prepare': value._ppt.submit.side_effect = RuntimeError('secret-cookie')
        elif ppt_error == 'drain': value._ppt.submit.return_value.drain.side_effect = TimeoutError('private-url')
        else: value._ppt.submit.return_value.drain.return_value = SimpleNamespace(list_error=None, failed=2)
        result = value.run('1','课',{'sub_id':'10','sub_title':'第一讲','has_playback':False})
        row = db.get_lecture('10')
        assert result == '完整笔记' and row['processed_at'] and row['error_stage'] is None
        assert row['transcript_source'] == 'official' and row['ppt_warning']
        assert 'secret' not in row['ppt_warning'] and 'private' not in row['ppt_warning']
        value._transcriber.transcribe_tail.assert_not_called()
    finally:
        db.conn.close()


def test_available_video_prefers_asr_and_does_not_probe_official_text(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'USE_OFFICIAL_TRANSCRIPT', False)
    db = Database(str(tmp_path/'test.db'))
    try:
        db.upsert_course('1','课','老师')
        db.insert_lecture('10','1','第一讲','2026-09-01')
        client = Mock()
        transcriber = Mock()
        transcriber.transcribe_tail.return_value = ('完整音频转录', [])
        value = LectureRunner(client, db, SimpleNamespace(audio_downloader=Mock()), transcriber, Mock(), Mock())
        assert value._get_transcript(db.get_lecture('10'),'1','10')[0] == '完整音频转录'
        client.get_transcript_segments.assert_not_called()
        assert db.get_lecture('10')['transcript_source'] == 'asr'
    finally:
        db.conn.close()


@pytest.mark.parametrize('source', ['official', None])
def test_pending_legacy_text_cannot_skip_independent_validation(tmp_path, monkeypatch, source):
    monkeypatch.setattr(config, 'USE_OFFICIAL_TRANSCRIPT', False)
    db = Database(str(tmp_path/'test.db'))
    try:
        db.upsert_course('1','课','老师')
        db.insert_lecture('10','1','第一讲','2026-09-01')
        db.update_transcript('10', '旧版官方转录' * 100, source=source)
        audio = Mock()
        audio.get.side_effect = VideoAccessError('未选课')
        value = LectureRunner(SimpleNamespace(get_transcript_segments=lambda _:full_segments(),
            get_recording_duration=lambda *_:None), db, SimpleNamespace(audio_downloader=audio), Mock(), Mock(), Mock())
        assert value._needs_audio('10')
        assert value._get_transcript(db.get_lecture('10'),'1','10') == (None,None)
        assert '独立录制时长' in db.get_lecture('10')['error_msg']
    finally:
        db.conn.close()


@pytest.mark.parametrize('kind', ['verified', 'no_duration', 'tail', 'changed_text'])
def test_local_official_checkpoint_guard_applies_to_summary_entry_points(tmp_path, kind):
    from scripts import local_course
    db = Database(str(tmp_path/'test.db'))
    try:
        db.upsert_course('1','课','老师')
        db.insert_lecture('10','1','第一讲','2026-09-01')
        segments = full_segments()[:30] if kind == 'tail' else full_segments()
        text = ' '.join(s['text'] for s in segments)
        # Legacy official checkpoints had no transcript_source column.
        db.update_transcript('10', text, source=None)
        if kind != 'no_duration':
            db.update_recording_duration('10', 3600, 'sub-info.video_duration_seconds')
        local_course.save_meta(db, '10', 'segments', segments)
        local_course.save_meta(db, '10', 'audio', {'asr_backend':'official',
            'expected_seconds':3600, 'duration_source':'sub-info.video_duration_seconds',
            'transcript_sha256':hashlib.sha256(text.encode()).hexdigest()})
        local_course.save_meta(db, '10', 'ocr', {'status':'unavailable', 'warning':'无法读取课件'})
        if kind == 'changed_text':
            db.update_transcript('10', text+'更改内容', source=None)
        if kind == 'verified':
            assert local_course.meta(db, '10', 'audio')
            assert local_course.summary_request(db, {'sub_id':'10'})
        else:
            assert local_course.meta(db, '10', 'audio') is None
            with pytest.raises(ValueError, match='先完成并验证'):
                local_course.summary_request(db, {'sub_id':'10'})
            summarizer = Mock()
            with pytest.raises(ValueError, match='先完成并验证'):
                local_course.summarize(db, Mock(), {'sub_id':'10'}, summarizer)
            summarizer.summarize.assert_not_called()
    finally:
        db.conn.close()
