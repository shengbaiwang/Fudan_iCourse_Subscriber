"""Exercise the console's real orchestration with saved/official materials."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.mark.parametrize("cached", [True, False])
def test_console_worker_honors_order_and_emits_each_note(tmp_path, monkeypatch, capsys, cached):
    import main
    from scripts import local_course, local_audio, run_console_job
    from src.ai import summarizer, transcriber
    from src.data import database
    from src.runtime import config

    path = tmp_path / "job.db"
    db = database.Database(str(path))
    for cid, sid, date in (("1", "10", "2026-09-01"), ("1", "11", "2026-09-02"), ("2", "20", "2026-09-01")):
        db.upsert_course(cid, "课程" + cid, "老师")
        db.insert_lecture(sid, cid, "课次" + sid, date)
        if cached:
            db.update_transcript(sid, "完整转录材料")
            local_course.save_meta(db, sid, "audio", {"verified": True})
    db.conn.close()
    monkeypatch.setattr(config, "COURSE_IDS", ["2", "1"])
    monkeypatch.setattr(config, "LECTURE_ORDER", "newest")
    monkeypatch.setattr(config, "DB_PATH", str(path))
    monkeypatch.setattr(config, "USE_OFFICIAL_TRANSCRIPT", not cached)

    class Client:
        vpn = SimpleNamespace(session=SimpleNamespace(close=lambda: None))
        def check_alive(self): return True
        def get_course_detail(self, cid):
            ids = ["10", "11"] if cid == "1" else ["20"]
            return {"title": "课程" + cid, "teacher": "老师", "lectures": [{"sub_id": sid, "sub_title": "课次" + sid,
                "date": "2026-09-02" if sid == "11" else "2026-09-01", "has_playback": True} for sid in ids]}
        def get_ppt_list(self, *_): return []
        def get_transcript_segments(self, *_): return [{"start_ms": 0, "end_ms": 3600000, "text": "完整官方转录"}]
    class Audio:
        active_count = 0
        def __init__(self, *_args, **_kwargs): pass
        def schedule(self, *_): raise AssertionError("verified/official transcript must not download audio")
        def release(self, *_args, **_kwargs): pass
        def shutdown(self): pass
    calls = []
    class Summary:
        def summarize(self, title, text):
            calls.append(title)
            return "# 新标题\n\n处理完成的笔记", "test/model"
    monkeypatch.setattr(local_course, "login", lambda _: Client())
    monkeypatch.setattr(local_audio, "LocalAudioDownloader", Audio)
    monkeypatch.setattr(summarizer, "Summarizer", Summary)
    monkeypatch.setattr(transcriber, "Transcriber", lambda: SimpleNamespace())
    monkeypatch.setenv("LOCAL_RUN_EVENTS", "1")
    monkeypatch.setenv("PUBLISH_NOTE_PROGRESS", "0")
    with patch.dict("os.environ"):
        assert run_console_job.run({"credentials":{"stuid":"synthetic","uispsw":"synthetic"}, "db_path":str(path),
            "kind":"process", "course_ids":["2","1"], "providers":{}, "api_keys":{}, "lecture_order":"newest",
            "provider":"test", "model":"model", "use_official_transcript":not cached}) == 0
    events = [json.loads(line.removeprefix("ICOURSE_EVENT ")) for line in capsys.readouterr().out.splitlines() if line.startswith("ICOURSE_EVENT ")]
    assert [row["sub_id"] for row in events if row["kind"] == "note"] == ["20", "11", "10"]
    assert calls == ["课程2", "课程1", "课程1"]
    result = database.Database(str(path))
    try:
        for sid in ("10", "11", "20"):
            assert result.get_lecture(sid)["summary"] == "处理完成的笔记"
            assert result.get_lecture(sid)["processed_at"].endswith("+00:00")
    finally: result.conn.close()
