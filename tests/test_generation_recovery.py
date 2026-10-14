"""Offline regressions for material retention, retries and full-course reruns."""
import io
import json
import random
import time
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from local_web import talks
from scripts import resummarize
from scripts.merge_db import merge
from src.ai import model_policy
from src.ai.ppt_dedup import dedup_dhash
from src.ai.summarizer import Summarizer
from src.data.database import Database
from src.runtime import config
from src.runtime.model_config import runtime_providers, validate_model_config


def provider(**settings):
    return {"name": "test", "base_url": "https://example.test/v1", "api_key": "synthetic-key",
            "api_key_env": "LLM_TEST_API_KEY", "models": ["saved-model"],
            "generation": settings}


def response(text="# 标题\n完整正文", reason="stop"):
    return SimpleNamespace(model="saved-model", choices=[SimpleNamespace(
        message=SimpleNamespace(content=text), finish_reason=reason)], usage=None)


@pytest.fixture
def db(tmp_path):
    value = Database(str(tmp_path / "course.db"))
    value.upsert_course("1", "课程", "老师")
    value.insert_lecture("10", "1", "第一课", "2026-09-01")
    yield value
    value.conn.close()


def test_long_ppt_keeps_all_distinct_pages_and_same_duplicate_rule():
    rng = random.Random(4048)
    pages = [f"{rng.getrandbits(64):016x}" for _ in range(151)]
    assert dedup_dhash(pages) == []  # old default incorrectly dropped 8
    assert dedup_dhash(pages, max_survivors=1) == []
    duplicate = f"{int(pages[0], 16) ^ 7:016x}"
    assert dedup_dhash(pages + [duplicate, None]) == [151]
    assert dedup_dhash([pages[0], duplicate, None]) == [1]


def test_retry_time_survives_restart_and_retries_beyond_three(db, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(model_policy.time, "time", lambda: now)
    for _ in range(4):
        db.update_error("10", "summarize", "暂时不可用")
    assert db.get_lecture("10")["retry_after"] == now + 2400
    assert db.get_unprocessed_lectures("1") == []
    assert db.get_unprocessed_lectures("1", force=True)[0]["sub_id"] == "10"
    reopened = Database(db.db_path)
    try:
        assert reopened.get_lecture("10")["retry_after"] == now + 2400
        monkeypatch.setattr(model_policy.time, "time", lambda: now + 2400)
        assert reopened.get_unprocessed_lectures("1")[0]["sub_id"] == "10"
        assert reopened.get_deferred_sub_ids("1") == set()
        reopened.clear_error("10")
        assert reopened.get_lecture("10")["retry_after"] is None
    finally:
        reopened.conn.close()


def test_retry_backoff_caps_and_stage_change_resets_delay(db, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(model_policy.time, "time", lambda: now)
    for _ in range(100):
        db.update_error("10", "summarize", "故障")
    assert db.get_lecture("10")["retry_after"] == now + 86400
    db.update_error("10", "audio", "新阶段")
    row = db.get_lecture("10")
    assert (row["error_count"], row["retry_after"]) == (1, now + 300)


def test_legacy_exhausted_rows_are_released_once(db):
    with db.conn:
        db.conn.execute("DELETE FROM meta WHERE key='processing_backoff_v1'")
        db.conn.execute("UPDATE lectures SET error_count=78,error_stage='summarize',retry_after=2000000000")
    migrated = Database(db.db_path)
    try:
        assert migrated.get_unprocessed_lectures("1")[0]["sub_id"] == "10"
        migrated.update_error("10", "summarize", "再次故障")
        retry_after = migrated.get_lecture("10")["retry_after"]
        reopened = Database(db.db_path)
        try:
            assert reopened.get_lecture("10")["retry_after"] == retry_after
        finally:
            reopened.conn.close()
    finally:
        migrated.conn.close()


@pytest.mark.parametrize("paused", [False, True])
def test_manual_recovery_bypasses_cooldown_and_pause_then_auto_retries_when_due(db, monkeypatch, paused):
    monkeypatch.setattr(config, "COURSE_IDS", ["1"])
    monkeypatch.setattr(config, "LECTURE_ORDER", "api")
    monkeypatch.setattr(config, "AUTO_CHECK_ENABLED", False)
    monkeypatch.setattr(config, "AUTO_CHECK_PAUSES", {"1": "pause"} if paused else {})
    client = Mock()
    client.get_course_detail.return_value = {"title": "课程", "teacher": "老师", "lectures": [
        {"sub_id": "10", "sub_title": "第一课", "date": "2026-09-01", "has_playback": True}]}
    for _ in range(4):
        db.update_error("10", "summarize", "暂时故障")
    assert main._enumerate_lectures(client, db, Mock(), session_check=lambda *_: None) == []
    work = main._enumerate_lectures(client, db, Mock(), force_video_recheck=True, session_check=lambda *_: None)
    assert [row[2]["sub_id"] for row in work] == ["10"]
    due = db.get_lecture("10")["retry_after"]
    monkeypatch.setattr(model_policy.time, "time", lambda: due)
    work = main._enumerate_lectures(client, db, Mock(), session_check=lambda *_: None)
    assert [row[2]["sub_id"] for row in work] == ["10"]


def test_manual_run_bypasses_paused_course_fast_exit(db, monkeypatch):
    from datetime import date
    db.upsert_all_courses_for_term("2026-春", [{"course_id": "1"}])
    db.write_meta("auto_check_pause_scans", '{"1":"pause"}')
    db.update_error("10", "summarize", "临时故障")
    monkeypatch.setattr(config, "COURSE_IDS", ["1"])
    monkeypatch.setattr(config, "AUTO_CHECK_PAUSES", {"1": "pause"})
    monkeypatch.setattr(config, "AUTO_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "VIDEO_RECHECK_NOW", True)
    monkeypatch.setattr(main, "Database", lambda: db)
    monkeypatch.setattr(main, "Reporter", Mock)
    monkeypatch.setattr(main, "course_today", lambda: date(2026, 10, 10))
    login = Mock()
    monkeypatch.setattr(main, "login_with_retry", login)
    monkeypatch.setattr(main, "ICourseClient", lambda *_: Mock())
    enumerate_work = Mock(return_value=[])
    monkeypatch.setattr(main, "_enumerate_lectures", enumerate_work)
    main.run()
    login.assert_called_once()
    assert enumerate_work.call_args.kwargs["force_video_recheck"] is True


@pytest.mark.parametrize("existing", [False, True])
def test_backoff_survives_progress_merge_into_new_and_existing_databases(db, tmp_path, existing):
    remote = tmp_path / "remote.db"
    if existing:
        other = Database(str(remote))
        other.upsert_course("1", "课程", "老师")
        other.insert_lecture("10", "1", "第一课", "2026-09-01")
        other.conn.close()
    db.update_error("10", "summarize", "临时失败")
    retry_after = db.get_lecture("10")["retry_after"]
    merge(db.db_path, str(remote))
    reopened = Database(str(remote))
    try:
        assert reopened.get_lecture("10")["retry_after"] == retry_after
        assert not reopened.get_unprocessed_lectures("1")
    finally:
        reopened.conn.close()


def test_full_course_rerun_batches_and_continues_after_failures(db, monkeypatch):
    lecture_ids = [str(100 + i) for i in range(45)]
    for sid in lecture_ids:
        db.insert_lecture(sid, "1", sid, "2026-09-01")
        db.update_transcript(sid, "课程材料")
        db.update_summary(sid, "历史正文", "previous/model")
    monkeypatch.setenv("RESUMMARIZE_SUB_IDS", ",".join(lecture_ids + [lecture_ids[0]]))
    calls, saved, events = [], [], []
    def generate(*_):
        sid = lecture_ids[len(calls)]
        calls.append(sid)
        if sid in {lecture_ids[1], lecture_ids[21]}:
            raise RuntimeError("临时故障耗尽本次重试")
        return "# 新标题\n新正文", "test/saved-model"
    monkeypatch.setattr(resummarize, "Database", lambda: db)
    monkeypatch.setattr(resummarize, "Summarizer", lambda: SimpleNamespace(summarize=generate))
    monkeypatch.setattr(resummarize, "note_saved", lambda _db, sid: saved.append(sid))
    monkeypatch.setattr(resummarize, "emit", lambda kind, **values: events.append((kind, values)))
    assert resummarize.run() == 1
    assert calls == lecture_ids
    assert len(saved) == 43
    assert [e[1]["size"] for e in events if e[0] == "batch"] == [20, 20, 5]
    assert [e[1]["index"] for e in events if e[0] == "lecture"] == list(range(1, 46))
    assert db.get_lecture(lecture_ids[1])["summary"] == "历史正文"
    assert db.get_lecture(lecture_ids[-1])["summary"] == "新正文"
    assert db.conn.execute("SELECT COUNT(*) FROM summary_versions WHERE sub_id=?", (lecture_ids[-1],)).fetchone()[0] == 2


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_shared_policy_retries_transient_http_failures_with_backoff(status, monkeypatch):
    delays, calls = [], []
    monkeypatch.setattr(model_policy.time, "sleep", delays.append)
    def call(options):
        calls.append(options)
        if len(calls) < 3:
            error = RuntimeError("temporarily unavailable")
            error.status_code = status
            error.response = SimpleNamespace(headers={"retry-after": "3"})
            raise error
        return "complete"
    value, options = model_policy.complete_with_recovery(provider(timeout_seconds=900), "saved-model", call)
    assert value == "complete" and len(calls) == 3
    assert delays == [3, 4]
    assert options["timeout"] == 900


@pytest.mark.parametrize("error", [ValueError("invalid"), model_policy.IncompleteSummaryError("content_filter")])
def test_permanent_errors_are_not_retried(error):
    call = Mock(side_effect=error)
    with pytest.raises(type(error)):
        model_policy.complete_with_recovery(provider(), "saved-model", call)
    assert call.call_count == 1


def test_sdk_retries_current_model_before_saved_fallback(monkeypatch):
    calls, delays = [], []
    monkeypatch.setattr(model_policy.time, "sleep", delays.append)
    p = provider(max_retries=2, retry_delay_seconds=1)
    p["models"] = ["saved-model", "fallback"]
    def create(**options):
        calls.append(options)
        if options["model"] == "saved-model":
            error = RuntimeError("temporary 503")
            error.status_code = 503
            raise error
        reply = response()
        reply.model = options["model"]
        return reply
    s = Summarizer.__new__(Summarizer)
    s.providers = [p]
    s._clients = {"test": SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))}
    assert s.summarize("课程", "材料")[1] == "test/fallback"
    assert [c["model"] for c in calls] == ["saved-model"] * 3 + ["fallback"]
    assert delays == [1, 2]


@pytest.mark.parametrize("parameter", ["max_tokens", "max_completion_tokens"])
def test_length_expands_for_all_providers_up_to_configured_ceiling(parameter):
    budgets = []
    def call(options):
        budgets.append(options[parameter])
        if options[parameter] < 250:
            raise model_policy.IncompleteSummaryError("length")
        return "complete"
    p = provider(output_tokens=64, max_output_tokens=250, token_parameter=parameter)
    assert model_policy.complete_with_recovery(p, "saved-model", call)[0] == "complete"
    assert budgets == [64, 128, 250]
    call = Mock(side_effect=model_policy.IncompleteSummaryError("length"))
    with pytest.raises(model_policy.IncompleteSummaryError):
        model_policy.complete_with_recovery(p, "saved-model", call)
    assert call.call_count == 3


def test_stdlib_talk_uses_same_retry_timeout_and_truncation_policy(monkeypatch):
    calls, delays = [], []
    monkeypatch.setattr(model_policy.time, "sleep", delays.append)
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self):
            return json.dumps({"model": "saved-model", "choices": [{"finish_reason": "length" if len(calls) == 2 else "stop", "message": {"content": "# 标题\n完整正文"}}]}).encode()
    def urlopen(request, **kwargs):
        calls.append((json.loads(request.data), kwargs))
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "limited", {"retry-after": "5"}, io.BytesIO(b"limited"))
        return Reply()
    monkeypatch.setattr(talks.urllib.request, "urlopen", urlopen)
    p = provider(timeout_seconds=900, output_tokens=64, max_output_tokens=128)
    assert talks.complete_talk([p], "system", "user") == ("# 标题\n完整正文", "test/saved-model")
    assert [c[0]["max_tokens"] for c in calls] == [64, 64, 128]
    assert [c[1]["timeout"] for c in calls] == [900, 900, 900]
    assert delays == [5]


def test_saved_request_settings_survive_config_normalization():
    p = provider(timeout_seconds=900, title_timeout_seconds=120, output_tokens=4096, max_output_tokens=8192)
    document = validate_model_config({"providers": [p]})
    resolved = runtime_providers(document)[0]
    assert resolved["generation"]["timeout_seconds"] == 900
    assert model_policy.request_options(resolved, "saved-model", title=True)["timeout"] == 120
    assert model_policy.request_options(resolved, "saved-model")["max_tokens"] == 4096


def test_cloud_runtime_keeps_configured_request_settings(monkeypatch):
    document = validate_model_config({"providers": [provider(timeout_seconds=1200, max_retries=4)]})
    monkeypatch.setattr(config, "MODEL_PROVIDERS", document["providers"])
    monkeypatch.setenv("LLM_TEST_API_KEY", "synthetic-key")
    monkeypatch.delenv("LLM_TEST_BASE_URL", raising=False)
    resolved = config.resolve_model_providers()[0]
    assert resolved["generation"]["max_retries"] == 4
    assert model_policy.request_options(resolved, "saved-model")["timeout"] == 1200


def test_network_retry_exhaustion_in_talk_falls_back_to_next_saved_model(monkeypatch):
    calls = []
    monkeypatch.setattr(model_policy.time, "sleep", lambda *_: None)
    p = provider(max_retries=1)
    p["models"] = ["saved-model", "fallback"]
    def call(_base, model, *_args, **_kwargs):
        calls.append(model)
        if model == "saved-model":
            raise ConnectionResetError("temporary reset")
        return "# 标题\n完整正文"
    monkeypatch.setattr(talks, "chat_complete", call)
    assert talks.complete_talk([p], "system", "user")[1] == "test/fallback"
    assert calls == ["saved-model", "saved-model", "fallback"]


@pytest.mark.parametrize("settings", [
    {"timeout_seconds": True}, {"max_retries": -1}, {"output_tokens": 0},
    {"output_tokens": 100, "max_output_tokens": 99}, {"token_parameter": "unknown"},
    {"retry_delay_seconds": 10, "retry_max_delay_seconds": 5}, {"token_parameter": []},
])
def test_invalid_generation_settings_rejected_before_dispatch(settings):
    with pytest.raises(ValueError):
        validate_model_config({"providers": [provider(**settings)]})
