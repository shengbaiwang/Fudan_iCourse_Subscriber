"""Scheduled pauses still drain unfinished notes without rediscovering old courses."""
import json
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

import main
from scripts.merge_db import merge
from src.data.database import Database
from src.runtime import config
from src.runtime.auto_check import FINISHED_TERMS_META, PAUSE_SCANS_META, parse_auto_check_pauses


@pytest.fixture
def course(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "course.db"))
    db.upsert_course("1", "已结课课程", "老师")
    db.insert_lecture("10", "1", "第一课", "2025-01-01")
    client = Mock()
    client.check_alive.return_value = True
    client.get_course_detail.return_value = {"title": "已结课课程", "teacher": "老师", "lectures": [
        {"sub_id": "10", "sub_title": "第一课", "date": "2025-01-01", "has_playback": True},
    ]}
    monkeypatch.setattr(config, "COURSE_IDS", ["1"])
    monkeypatch.setattr(config, "LECTURE_ORDER", "api")
    monkeypatch.setattr(config, "AUTO_CHECK_PAUSES", {"1": "first-pause"})
    monkeypatch.setattr(config, "AUTO_CHECK_ENABLED", False)
    yield db, client, Mock()
    db.conn.close()


def complete(db, sid):
    db.update_summary(sid, "已有笔记", "test/model")
    db.mark_processed(sid)


def test_complete_paused_course_gets_one_final_scan_then_no_queries(course):
    db, client, reporter = course
    complete(db, "10")
    assert main._enumerate_lectures(client, db, reporter) == []
    assert json.loads(db.read_meta(PAUSE_SCANS_META)) == {"1": "first-pause"}
    client.reset_mock()
    assert main._enumerate_lectures(client, db, reporter) == []
    client.check_alive.assert_not_called()
    client.get_course_detail.assert_not_called()
    assert db.get_lecture("10")["summary"] == "已有笔记"


def test_pause_discovers_missing_lectures_and_drains_saved_backlog(course):
    db, client, reporter = course
    client.get_course_detail.return_value["lectures"].append(
        {"sub_id": "11", "sub_title": "第二课", "date": "2025-01-02", "has_playback": True})
    assert [row[2]["sub_id"] for row in main._enumerate_lectures(client, db, reporter)] == ["10", "11"]
    complete(db, "10")
    client.reset_mock()
    assert [row[2]["sub_id"] for row in main._enumerate_lectures(client, db, reporter)] == ["11"]
    client.get_course_detail.assert_not_called()
    complete(db, "11")
    assert main._enumerate_lectures(client, db, reporter) == []


def test_failed_final_scan_is_retried(course):
    db, client, reporter = course
    client.get_course_detail.side_effect = RuntimeError("synthetic outage")
    assert main._enumerate_lectures(client, db, reporter) == []
    assert db.read_meta(PAUSE_SCANS_META) is None
    client.get_course_detail.side_effect = None
    assert len(main._enumerate_lectures(client, db, reporter)) == 1


def test_pause_probes_official_text_before_recording_is_published(course):
    db, client, reporter = course
    complete(db, "10")
    delayed = {"sub_id": "11", "sub_title": "第二课", "date": "2025-01-02", "has_playback": False}
    client.get_course_detail.return_value["lectures"].append(delayed)
    assert [row[2]["sub_id"] for row in main._enumerate_lectures(client, db, reporter)] == ["11"]
    assert db.read_meta(PAUSE_SCANS_META) is None
    delayed["has_playback"] = True
    assert [row[2]["sub_id"] for row in main._enumerate_lectures(client, db, reporter)] == ["11"]
    assert json.loads(db.read_meta(PAUSE_SCANS_META)) == {"1": "first-pause"}


def test_same_title_does_not_hide_a_distinct_official_text_source(course):
    db, client, reporter = course
    lectures = client.get_course_detail.return_value['lectures']
    lectures.append(dict(lectures[0], sub_id='11', has_playback=False))
    lectures.append(dict(lectures[0]))
    assert [row[2]['sub_id'] for row in main._enumerate_lectures(client, db, reporter)] == ['10', '11']


def test_resume_and_repause_require_fresh_discovery(course, monkeypatch):
    db, client, reporter = course
    complete(db, "10")
    main._enumerate_lectures(client, db, reporter)
    client.reset_mock()
    monkeypatch.setattr(config, "AUTO_CHECK_PAUSES", {"1": "second-pause"})
    main._enumerate_lectures(client, db, reporter)
    client.get_course_detail.assert_called_once_with("1")
    client.reset_mock()
    # Single Run has no pause configuration and always discovers its selection.
    monkeypatch.setattr(config, "AUTO_CHECK_PAUSES", {})
    main._enumerate_lectures(client, db, reporter)
    client.get_course_detail.assert_called_once_with("1")


@pytest.mark.parametrize("paused", [True, False])
def test_exhausted_lectures_do_not_reenter_as_new_playback(course, monkeypatch, paused):
    db, client, reporter = course
    monkeypatch.setattr(config, "AUTO_CHECK_PAUSES", {"1": "first-pause"} if paused else {})
    for _ in range(3):
        db.update_error("10", "audio", "synthetic missing recording")
    assert main._enumerate_lectures(client, db, reporter) == []
    assert main._enumerate_lectures(client, db, reporter) == []
    assert db.get_lecture("10")["error_count"] == 3


def test_pause_scan_survives_progress_merge(tmp_path, monkeypatch):
    monkeypatch.setenv("COURSE_AUTO_CHECK_JSON", '{"1":"first-pause"}')
    local, remote = tmp_path / "local.db", tmp_path / "remote.db"
    db = Database(str(local))
    db.write_meta(PAUSE_SCANS_META, '{"1":"first-pause"}')
    db.conn.close()
    db = Database(str(remote))
    db.conn.close()
    merge(str(local), str(remote))
    db = Database(str(remote))
    try:
        assert json.loads(db.read_meta(PAUSE_SCANS_META)) == {"1": "first-pause"}
    finally:
        db.conn.close()


def test_manual_checkpoint_does_not_rewind_daily_pause_scan(tmp_path, monkeypatch):
    monkeypatch.delenv("COURSE_AUTO_CHECK_JSON", raising=False)
    local, remote = tmp_path / "local.db", tmp_path / "remote.db"
    for path, token in ((local, "old-pause"), (remote, "new-pause")):
        db = Database(str(path))
        db.write_meta(PAUSE_SCANS_META, json.dumps({"1": token}))
        db.conn.close()
    merge(str(local), str(remote))
    db = Database(str(remote))
    assert json.loads(db.read_meta(PAUSE_SCANS_META)) == {"1": "new-pause"}
    db.conn.close()


def test_all_paused_completed_courses_skip_platform_login(course, monkeypatch):
    db, client, reporter = course
    db.upsert_all_courses_for_term("2025-春", [{"course_id": "1", "title": "课程"}])
    complete(db, "10")
    main._enumerate_lectures(client, db, reporter)
    login = Mock(side_effect=AssertionError("complete paused courses should not log in"))
    monkeypatch.setattr(main, "Database", lambda: db)
    monkeypatch.setattr(main, "Reporter", lambda: reporter)
    monkeypatch.setattr(main, "login_with_retry", login)
    monkeypatch.setattr(main, "course_today", lambda: date(2026, 10, 10))
    main.run()
    login.assert_not_called()


def test_pause_settings_survive_shards_and_manual_publish(tmp_path, monkeypatch):
    from src.data.sharder import shard_database, reassemble_database
    source, restored, output = tmp_path / "source.db", tmp_path / "restored.db", tmp_path / "shards"
    db = Database(str(source))
    db.upsert_course("1", "课程", "老师")
    db.write_meta(PAUSE_SCANS_META, '{"1":"first-pause"}')
    db.write_meta(FINISHED_TERMS_META, '{"1":"2025-08-01"}')
    db.conn.close()
    monkeypatch.setenv("COURSE_AUTO_CHECK_JSON", '{"1":"first-pause"}')
    index = shard_database(str(source), str(output), "synthetic-password")
    reassemble_database(index, str(output / "shards"), str(restored), "synthetic-password")
    db = Database(str(restored))
    assert json.loads(db.read_meta("auto_check_pauses")) == {"1": "first-pause"}
    assert json.loads(db.read_meta(PAUSE_SCANS_META)) == {"1": "first-pause"}
    assert json.loads(db.read_meta(FINISHED_TERMS_META)) == {"1": "2025-08-01"}
    db.conn.close()
    # A manual workflow omits the setting and must retain the shared snapshot.
    monkeypatch.delenv("COURSE_AUTO_CHECK_JSON")
    shard_database(str(restored), str(output), "synthetic-password")
    db = Database(str(restored))
    assert json.loads(db.read_meta("auto_check_pauses")) == {"1": "first-pause"}
    db.conn.close()
    # An explicit empty scheduled setting clears an earlier pause.
    monkeypatch.setenv("COURSE_AUTO_CHECK_JSON", "")
    shard_database(str(restored), str(output), "synthetic-password")
    db = Database(str(restored))
    assert json.loads(db.read_meta("auto_check_pauses")) == {}
    db.conn.close()


@pytest.mark.parametrize("raw", ['[]', '{"1":true}', '{"bad,id":"token"}'])
def test_pause_config_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        parse_auto_check_pauses(raw)


@pytest.fixture
def ended_course(course, monkeypatch):
    db, client, reporter = course
    db.upsert_all_courses_for_term("2025-20262", [{"course_id": "1", "title": "课程"}])
    monkeypatch.setattr(config, "AUTO_CHECK_PAUSES", {})
    monkeypatch.setattr(config, "AUTO_CHECK_ENABLED", True)
    monkeypatch.setattr(main, "course_today", lambda: date(2026, 10, 12))
    monkeypatch.setattr("src.runtime.auto_check.course_today", lambda: date(2026, 10, 12))
    return course


def test_ended_complete_course_stops_after_final_discovery(ended_course):
    db, client, reporter = ended_course
    complete(db, "10")
    assert not db.auto_check_status()["1"]["auto_check_stopped"]
    assert main._enumerate_lectures(client, db, reporter) == []
    assert db.auto_check_status()["1"]["auto_check_stopped"]
    client.reset_mock()
    assert main._enumerate_lectures(client, db, reporter) == []
    client.check_alive.assert_not_called()
    client.get_course_detail.assert_not_called()
    assert db.get_lecture("10")["summary"] == "已有笔记"


def test_ended_course_keeps_discovering_until_new_notes_are_complete(ended_course):
    db, client, reporter = ended_course
    complete(db, "10")
    client.get_course_detail.return_value["lectures"].append(
        {"sub_id": "11", "sub_title": "最后一课", "date": "2026-06-30", "has_playback": True})
    assert [row[2]["sub_id"] for row in main._enumerate_lectures(client, db, reporter)] == ["11"]
    assert not db.auto_check_status()["1"]["auto_check_stopped"]
    main._enumerate_lectures(client, db, reporter)
    assert client.get_course_detail.call_count == 2
    complete(db, "11")
    client.reset_mock()
    assert db.auto_check_status()["1"]["auto_check_stopped"]
    main._enumerate_lectures(client, db, reporter)
    client.get_course_detail.assert_not_called()


@pytest.mark.parametrize("stage", ["no_video", "video_access", "summary"])
def test_missing_notes_prevent_stop_even_if_deferred_or_exhausted(ended_course, stage):
    db, client, reporter = ended_course
    for _ in range(3):
        db.update_error("10", stage, "synthetic failure")
    main._enumerate_lectures(client, db, reporter)
    assert not db.auto_check_status()["1"]["auto_check_stopped"]
    main._enumerate_lectures(client, db, reporter)
    assert client.get_course_detail.call_count == 2


def test_failed_or_empty_final_discovery_does_not_stop(ended_course):
    db, client, reporter = ended_course
    complete(db, "10")
    client.get_course_detail.side_effect = RuntimeError("synthetic outage")
    main._enumerate_lectures(client, db, reporter)
    assert db.read_meta(FINISHED_TERMS_META) is None
    client.get_course_detail.side_effect = None
    client.get_course_detail.return_value["lectures"] = []
    main._enumerate_lectures(client, db, reporter)
    assert not db.auto_check_status()["1"]["auto_check_stopped"]


@pytest.mark.parametrize("summary,processed", [(None, True), ("  \n", True), ("已有笔记", False)])
def test_only_finished_nonempty_notes_count_as_complete(ended_course, summary, processed):
    db, client, reporter = ended_course
    if summary is not None:
        db.update_summary("10", summary, "test/model")
    if processed:
        db.mark_processed("10")
    main._enumerate_lectures(client, db, reporter)
    assert not db.auto_check_status()["1"]["auto_check_stopped"]


@pytest.mark.parametrize("term", ["2026-20271", "2027-春", "25", "未知学期"])
def test_current_future_or_unknown_terms_keep_checking(ended_course, term):
    db, client, reporter = ended_course
    db.conn.execute("DELETE FROM all_courses")
    db.upsert_all_courses_for_term(term, [{"course_id": "1"}])
    complete(db, "10")
    main._enumerate_lectures(client, db, reporter)
    main._enumerate_lectures(client, db, reporter)
    assert not db.auto_check_status()["1"]["auto_check_stopped"]
    assert client.get_course_detail.call_count == 2


@pytest.mark.parametrize("lecture_date", ["2026-12-01", "", None, "unknown"])
def test_future_or_unknown_lecture_dates_prevent_stop(ended_course, lecture_date):
    db, client, reporter = ended_course
    db.conn.execute("UPDATE lectures SET date=? WHERE sub_id='10'", (lecture_date,))
    client.get_course_detail.return_value["lectures"][0]["date"] = lecture_date
    complete(db, "10")
    main._enumerate_lectures(client, db, reporter)
    assert not db.auto_check_status()["1"]["auto_check_stopped"]


def test_official_notes_can_finish_without_a_playable_recording(ended_course):
    db, client, reporter = ended_course
    complete(db, "10")
    client.get_course_detail.return_value["lectures"][0]["has_playback"] = False
    main._enumerate_lectures(client, db, reporter)
    assert db.auto_check_status()["1"]["auto_check_stopped"]


def test_single_run_and_video_recheck_still_discover_stopped_courses(ended_course, monkeypatch):
    db, client, reporter = ended_course
    complete(db, "10")
    main._enumerate_lectures(client, db, reporter)
    client.reset_mock()
    main._enumerate_lectures(client, db, reporter, force_video_recheck=True)
    client.get_course_detail.assert_called_once_with("1")
    monkeypatch.setattr(config, "AUTO_CHECK_ENABLED", False)
    client.reset_mock()
    main._enumerate_lectures(client, db, reporter)
    client.get_course_detail.assert_called_once_with("1")


def test_stopped_courses_skip_login_even_on_catalog_refresh_day(ended_course, monkeypatch):
    db, client, reporter = ended_course
    complete(db, "10")
    main._enumerate_lectures(client, db, reporter)
    login = Mock(side_effect=AssertionError("finished courses should not log in"))
    monkeypatch.setattr(main, "Database", lambda: db)
    monkeypatch.setattr(main, "Reporter", lambda: reporter)
    monkeypatch.setattr(main, "login_with_retry", login)
    monkeypatch.setattr(main, "course_today", lambda: date(2026, 10, 25))
    main.run()
    login.assert_not_called()


def test_new_term_reactivates_a_reused_course_id(ended_course):
    db, client, reporter = ended_course
    complete(db, "10")
    main._enumerate_lectures(client, db, reporter)
    db.upsert_all_courses_for_term("2026-20271", [{"course_id": "1"}])
    assert not db.auto_check_status()["1"]["auto_check_stopped"]
    client.reset_mock()
    main._enumerate_lectures(client, db, reporter)
    client.get_course_detail.assert_called_once_with("1")


def test_calendar_boundary_requires_a_fresh_scan(ended_course, monkeypatch):
    db, client, reporter = ended_course
    complete(db, "10")
    monkeypatch.setattr(main, "course_today", lambda: date(2026, 7, 31))
    main._enumerate_lectures(client, db, reporter)
    assert db.read_meta(FINISHED_TERMS_META) is None
    monkeypatch.setattr(main, "course_today", lambda: date(2026, 8, 1))
    main._enumerate_lectures(client, db, reporter)
    assert client.get_course_detail.call_count == 2
    main._enumerate_lectures(client, db, reporter)
    assert client.get_course_detail.call_count == 2


def test_clock_uses_school_date_on_a_utc_runner(monkeypatch):
    from src.runtime import auto_check

    clock = Mock()
    clock.now.side_effect = lambda tz: datetime(2026, 7, 31, 16, 1, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(auto_check, "datetime", clock)
    assert auto_check.course_today() == date(2026, 8, 1)


def test_empty_course_does_not_count_as_complete(ended_course):
    db, client, reporter = ended_course
    db.conn.execute("DELETE FROM lectures")
    client.get_course_detail.return_value["lectures"] = []
    main._enumerate_lectures(client, db, reporter)
    assert not db.auto_check_status()["1"]["auto_check_stopped"]
    main._enumerate_lectures(client, db, reporter)
    assert client.get_course_detail.call_count == 2


def test_final_term_scan_survives_merge_and_matches_local_console(ended_course, tmp_path):
    from local_web.database import DatabaseManager

    db, client, reporter = ended_course
    complete(db, "10")
    main._enumerate_lectures(client, db, reporter)
    manager = DatabaseManager(tmp_path / "cache")
    try:
        target = Database(str(manager.db_path))
        target.conn.close()
        merge(db.db_path, str(manager.db_path))
        assert manager.subscription_check_status({}) == db.auto_check_status()
        assert manager.subscription_check_status({})["1"]["auto_check_stopped"]
    finally:
        manager.close()
