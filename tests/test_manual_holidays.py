from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from local_web.database import DatabaseManager
from local_web.server import LectureHolidayRequest, create_app
from local_web.state import LectureHolidayStore, RepositorySettings, RuntimeState, SettingsStore
from local_web.talks import TalkStore
from src.data.schema import SCHEMA_SQL


class EmptyKeychain:
    available = False

    def load(self, _settings):
        return None


def test_manual_labels_survive_restart_cancel_and_repository_switch(tmp_path):
    runtime = RuntimeState(store=SettingsStore(tmp_path), credential_store=EmptyKeychain())
    runtime.save_lecture_holiday("first", True)
    runtime.save_lecture_holiday("second", True)
    restored = RuntimeState(store=SettingsStore(tmp_path), credential_store=EmptyKeychain())
    assert restored.lecture_holidays == {"first", "second"}
    restored.save_lecture_holiday("first", False)
    assert restored.lecture_holiday_store.load(restored.settings) == {"second"}
    original = restored.settings
    other = RepositorySettings("other", "library", "data")
    restored.configure(other, token="fixture", stuid="fixture", uispsw="fixture")
    assert restored.lecture_holidays == set()
    restored.save_lecture_holiday("first", True)
    restored.configure(original, token="fixture", stuid="fixture", uispsw="fixture")
    assert restored.lecture_holidays == {"second"}
    assert restored.lecture_holiday_store.load(other) == {"first"}


def test_failed_save_keeps_the_original_status(tmp_path):
    runtime = RuntimeState(store=SettingsStore(tmp_path), credential_store=EmptyKeychain())
    runtime.save_lecture_holiday("first", True)
    with patch.object(runtime.lecture_holiday_store, "save", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            runtime.save_lecture_holiday("first", False)
    assert runtime.lecture_holidays == {"first"}
    assert runtime.lecture_holiday_store.load(runtime.settings) == {"first"}


def test_malformed_saved_labels_do_not_mark_other_lectures(tmp_path):
    store = LectureHolidayStore(tmp_path)
    settings = RepositorySettings()
    store.path.write_text('{"' + store._scope(settings) + '": [true, null, "", " 12 "]}')
    assert store.load(settings) == {"12"}
    store.path.write_text('{"' + store._scope(settings) + '": "12"}')
    assert store.load(settings) == set()


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
def test_api_rejects_ambiguous_boolean_values(value):
    with pytest.raises(ValidationError):
        LectureHolidayRequest(sub_id="12", holiday=value)


def test_manual_api_preserves_errors_and_content_and_rejects_unknown_ids(tmp_path):
    manager = DatabaseManager(cache_dir=tmp_path / "cache")
    runtime = RuntimeState(store=SettingsStore(tmp_path), credential_store=EmptyKeychain())
    try:
        with sqlite3.connect(manager.db_path) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute("INSERT INTO courses VALUES ('course', '课程', '教师')")
            conn.execute("INSERT INTO lectures(sub_id,course_id,sub_title,error_stage,error_msg,error_count,retry_after) "
                         "VALUES ('failed','course','2026-03-31','video_access','录播无权限',2,2000000000)")
            conn.execute("INSERT INTO lectures(sub_id,course_id,sub_title,transcript,summary) "
                         "VALUES ('ready','course','2026-04-14','转录原文','笔记原文')")
        before = manager.db_path.read_bytes()
        app = create_app(state=runtime, database=manager, talk_store=TalkStore(tmp_path / "talks"))
        get = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/local/lecture-holidays" and "GET" in route.methods)
        put = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/local/lecture-holidays" and "PUT" in route.methods)
        assert asyncio.run(get()) == {"sub_ids": []}
        assert asyncio.run(put(LectureHolidayRequest(sub_id="failed", holiday=True))) == {"sub_ids": ["failed"]}
        assert manager.db_path.read_bytes() == before
        assert manager.lecture("ready")["summary"] == "笔记原文"
        assert manager.lecture("failed")["error_stage"] == "video_access"
        assert asyncio.run(put(LectureHolidayRequest(sub_id="failed", holiday=False))) == {"sub_ids": []}
        assert manager.db_path.read_bytes() == before
        with pytest.raises(HTTPException) as error:
            asyncio.run(put(LectureHolidayRequest(sub_id="unknown", holiday=True)))
        assert error.value.status_code == 404
        assert runtime.lecture_holidays == set()
    finally:
        manager.close()
