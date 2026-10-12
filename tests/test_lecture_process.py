"""Verify real process deadlines, retained checkpoints, and continuation."""
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from src.data.database import Database
from src.runtime import lecture_process


def test_timeout_kills_work_group_retains_material_and_continues_queue(tmp_path, monkeypatch, capsys):
    db = Database(str(tmp_path/'job.db'))
    for sid in ('10','11'):
        db.upsert_course('1','课','老师')
        db.insert_lecture(sid,'1','第一讲','2026-09-01')
    heartbeat = tmp_path/'heartbeat'
    helper = tmp_path/'worker.py'
    helper.write_text('''import json, pathlib, sqlite3, subprocess, sys, time
p = json.loads(sys.stdin.readline())
sid = str(p['lecture']['sub_id'])
with sqlite3.connect(p['db_path']) as db:
    db.execute("UPDATE lectures SET transcript='已提交材料' WHERE sub_id=?", (sid,))
if sid == '10':
    subprocess.Popen([sys.executable, '-c', "import pathlib,time,sys; p=pathlib.Path(sys.argv[1]);\\nwhile True: p.write_text(str(time.monotonic())); time.sleep(.02)", sys.argv[1]])
    time.sleep(60)
else:
    with sqlite3.connect(p['db_path']) as db:
        db.execute("UPDATE lectures SET summary='后续课次笔记' WHERE sub_id=?", (sid,))
''')
    execute = lecture_process.run_lecture_process
    def run_child(payload, _timeout):
        return execute(payload, 1, command=[sys.executable, '-u', str(helper), str(heartbeat)])
    monkeypatch.setattr(lecture_process, 'run_lecture_process', run_child)
    monkeypatch.setenv('LOCAL_RUN_EVENTS','1')
    monkeypatch.setenv('PUBLISH_NOTE_PROGRESS','0')
    client = SimpleNamespace(vpn=SimpleNamespace(session=requests.Session()))
    try:
        assert lecture_process.drive_lectures(client, db, Mock(),
            [('1','课',{'sub_id':'10'}), ('1','课',{'sub_id':'11'})], lambda _:None) == (1,1)
        first, second = db.get_lecture('10'), db.get_lecture('11')
        assert first['transcript'] == '已提交材料' and first['error_stage'] == 'process_timeout'
        assert first['retry_after'] and not first['processed_at']
        assert second['summary'] == '后续课次笔记' and second['processed_at']
        events = [json.loads(line.removeprefix('ICOURSE_EVENT ')) for line in capsys.readouterr().out.splitlines() if line.startswith('ICOURSE_EVENT ')]
        assert [e['sub_id'] for e in events if e['kind']=='note'] == ['11']
        assert heartbeat.is_file()
        frozen = heartbeat.read_text()
        time.sleep(.1)
        assert heartbeat.read_text() == frozen
        for _ in range(4): db.update_error('10','process_timeout','超时')
        assert '10' not in db.get_exhausted_sub_ids('1')
        assert [row['sub_id'] for row in db.get_unprocessed_lectures('1',include_deferred=True)] == ['10']
    finally:
        db.conn.close()


def test_session_transfer_preserves_cookie_scope_without_relogging_in():
    from scripts.run_lecture_job import restore_client
    session = requests.Session()
    session.cookies.set('vpn','synthetic',domain='webvpn.fudan.edu.cn',path='/https/',secure=True)
    restored = restore_client({'session':lecture_process.session_payload(SimpleNamespace(vpn=SimpleNamespace(session=session)))})
    try:
        cookie = next(iter(restored.vpn.session.cookies))
        assert (cookie.name,cookie.value,cookie.domain,cookie.path,cookie.secure) == (
            'vpn','synthetic','webvpn.fudan.edu.cn','/https/',True)
    finally:
        restored.vpn.session.close()


def test_real_worker_exits_after_saving_note_despite_a_stuck_native_pool(tmp_path, monkeypatch):
    path = str(tmp_path/'job.db')
    db = Database(path)
    db.upsert_course('1','课','老师')
    db.insert_lecture('10','1','第一讲','2026-09-01')
    helper = tmp_path/'native_worker.py'
    helper.write_text('''import sys, time
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from scripts import run_lecture_job
from src.ai import summarizer, transcriber
from src.runtime import scheduler
class Client:
    vpn = SimpleNamespace(session=SimpleNamespace(close=lambda:None))
    def get_transcript_segments(self, *_):
        return [{'text':'完整官方转录'*80,'start_ms':0,'end_ms':60000}]
    def get_recording_duration(self, *_): return None
    def get_ppt_list(self, *_): return []
class Summary:
    def summarize(self, *_): return '# 标题\\n\\n实际工作进程保存的笔记', 'test/model'
run_lecture_job.restore_client = lambda _:Client()
summarizer.Summarizer = Summary
transcriber.Transcriber = lambda:SimpleNamespace()
initialize = scheduler.Scheduler.__init__
def stalled_pool(self, *args, **kwargs):
    initialize(self, *args, **kwargs)
    self.ocr_pool.submit(time.sleep, 60)
scheduler.Scheduler.__init__ = stalled_pool
run_lecture_job.worker_main()
''')
    monkeypatch.setenv('USE_OFFICIAL_TRANSCRIPT','1')
    try:
        code = lecture_process.run_lecture_process({'db_path':path,'course_id':'1','course_title':'课',
            'lecture':{'sub_id':'10'}, 'local':False}, 10,
            command=[sys.executable,'-u',str(helper),str(lecture_process.ROOT)])
        assert code == 0
        row = db.get_lecture('10')
        assert row['summary'] == '实际工作进程保存的笔记' and row['processed_at']
        assert row['transcript_source'] == 'official' and row['recording_duration'] is None
    finally:
        db.conn.close()


def test_old_duration_rejection_cooldowns_are_released_once(tmp_path):
    path = str(tmp_path/'old.db')
    db = Database(path)
    db.upsert_course('1','课','老师')
    for sid in ('10','11'):
        db.insert_lecture(sid,'1','第一讲','2026-09-01')
    db.update_error('10','video_access','未选课；缺少带明确单位的独立录制时长，官方转录待核验')
    db.update_error('11','video_access','官方转录为空或不可读取')
    untouched = db.get_lecture('11')['retry_after']
    with db.conn:
        db.conn.execute("DELETE FROM meta WHERE key='official_transcript_independent_v1'")
    db.conn.close()
    db = Database(path)
    try:
        assert db.get_lecture('10')['retry_after'] is None
        assert db.get_lecture('11')['retry_after'] == untouched
        db.update_error('10','video_access','仍未提供转录')
        cooldown = db.get_lecture('10')['retry_after']
    finally:
        db.conn.close()
    db = Database(path)
    try:
        assert db.get_lecture('10')['retry_after'] == cooldown
    finally:
        db.conn.close()


def test_optional_ppt_timeout_still_generates_official_text_note(tmp_path, monkeypatch):
    from src.pipeline import lecture_runner
    from src.runtime.material_deadline import material_deadline
    from src.runtime import config
    monkeypatch.setattr(config,'USE_OFFICIAL_TRANSCRIPT',True)
    monkeypatch.setattr(lecture_runner,'material_deadline',lambda _:material_deadline(.05))
    db = Database(str(tmp_path/'job.db'))
    try:
        db.upsert_course('1','课','老师')
        db.insert_lecture('10','1','第一讲','2026-09-01')
        client = Mock()
        client.get_recording_duration.return_value = None
        client.get_transcript_segments.return_value = [{'text':'完整课堂文字'*80,'start_ms':0,'end_ms':60000}]
        summary = Mock()
        summary.summarize.return_value = ('# 第一讲\n\n已生成笔记','test/model')
        value = lecture_runner.LectureRunner(client,db,SimpleNamespace(audio_downloader=Mock()),Mock(),summary,Mock())
        value._ppt = Mock()
        value._ppt.submit.return_value.drain.side_effect = lambda:time.sleep(30)
        assert value.run('1','课',{'sub_id':'10'}) == '已生成笔记'
        assert '超时' in db.get_lecture('10')['ppt_warning']
        assert db.get_lecture('10')['summary'] and db.get_lecture('10')['processed_at']
    finally:
        db.conn.close()
