from types import SimpleNamespace
import json

import pytest

from scripts import local_course


@pytest.mark.parametrize("expected,actual", [(0, 100), (100, 0), (float("nan"), 100), (100, float("inf")), (7200, 6400), (7200, 7300)])
def test_incomplete_or_unknown_audio_is_rejected(expected, actual):
    with pytest.raises(ValueError):
        local_course.verify_duration(expected, actual)


def test_small_decode_drift_is_accepted():
    local_course.verify_duration(7745.86, 7745.812)


def test_course_identity_and_recording_ids():
    detail = {"title": local_course.TITLE, "teacher": local_course.TEACHER,
              "lectures": [{"sub_id": 2, "date": "2024-02-28", "sub_title": "同一天"},
                           {"sub_id": 1, "date": "2024-02-28", "sub_title": "同一天"}]}
    assert len(local_course.lecture_list(detail)) == 2
    detail["teacher"] = "另一位教师"
    with pytest.raises(ValueError):
        local_course.lecture_list(detail)


def test_encrypted_checkpoint_survives_interruption(tmp_path, monkeypatch):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    creds = SimpleNamespace(stuid="test", uispsw="test-only-password")
    with pytest.raises(RuntimeError):
        with local_course.local_database(creds) as (db, checkpoint):
            db.upsert_course("4048", local_course.TITLE, local_course.TEACHER)
            db.insert_lecture("test-sub", "4048", "测试", "2024-01-01")
            db.update_transcript("test-sub", "完整已验证转录")
            checkpoint()
            db.update_transcript("test-sub", "未通过检查的临时结果")
            raise RuntimeError("模拟进程中断")
    assert "完整已验证转录".encode() not in (tmp_path / "icourse.db.enc").read_bytes()
    with local_course.local_database(creds) as (db, checkpoint):
        assert db.get_lecture("test-sub")["transcript"] == "完整已验证转录"


def test_concurrent_run_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    creds = SimpleNamespace(stuid="test", uispsw="test-only-password")
    with local_course.local_database(creds):
        with pytest.raises(RuntimeError, match="已有本机任务"):
            with local_course.local_database(creds):
                pass


def test_job_lock_covers_login_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    with local_course.running_job():
        assert json.loads((tmp_path / "active-job.json").read_text())["stage"] == "prepare"
        with pytest.raises(RuntimeError, match="已有本机任务"):
            with local_course.running_job():
                pass
    assert not (tmp_path / "active-job.json").exists()


def test_status_can_read_checkpoint_during_preparation(tmp_path, monkeypatch):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    creds = SimpleNamespace(stuid="test", uispsw="test-only-password")
    with local_course.local_database(creds) as (db, checkpoint):
        db.upsert_course("4048", local_course.TITLE, local_course.TEACHER)
        db.insert_lecture("test-sub", "4048", "测试", "2024-01-01")
        db.update_transcript("test-sub", "已保存结果")
        checkpoint()
        before = (tmp_path / "icourse.db.enc").read_bytes()
        db.update_transcript("test-sub", "尚未通过检查")
        with local_course.read_snapshot(creds) as snapshot:
            assert snapshot.get_lecture("test-sub")["transcript"] == "已保存结果"
        assert (tmp_path / "icourse.db.enc").read_bytes() == before


def test_error_output_redacts_session_material():
    error = RuntimeError("https://example.test/media?t=secret\nCookie: private-session")
    text = local_course.safe_error(error)
    assert "secret" not in text
    assert "private-session" not in text


def test_summary_requires_verified_material(tmp_path, monkeypatch):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    creds = SimpleNamespace(stuid="test", uispsw="test-only-password")
    with local_course.local_database(creds) as (db, checkpoint):
        db.upsert_course("4048", local_course.TITLE, local_course.TEACHER)
        db.insert_lecture("test-sub", "4048", "测试", "2024-01-01")
        db.update_transcript("test-sub", "有文字但未经完整性检查")
        with pytest.raises(ValueError, match="先完成并验证"):
            local_course.summarize(db, checkpoint, {"sub_id": "test-sub"}, None)


def test_profile_keeps_another_course_isolated(tmp_path, monkeypatch):
    for name in ("COURSE_ID", "TITLE", "TEACHER", "TERM", "DEPT", "RUN_DIR"):
        monkeypatch.setattr(local_course, name, getattr(local_course, name))
    profile = tmp_path / "course.json"
    profile.write_text(json.dumps({"course_id": "123", "title": "另一门课", "teacher": "另一位教师",
                                   "term": "2025-2026-1", "dept": "历史学系"}))
    local_course.configure_course(profile)
    assert local_course.COURSE_ID == "123"
    assert local_course.RUN_DIR.name == "local-course-123"
    with pytest.raises(ValueError):
        local_course.lecture_list({"title": "当代中国经济与社会专题研究", "teacher": "林超超", "lectures": []})


def test_profile_rejects_path_traversal(tmp_path):
    profile = tmp_path / "course.json"
    profile.write_text(json.dumps({"course_id": "../../other", "title": "课", "teacher": "师",
                                   "term": "2025-2026-1", "dept": "系"}))
    with pytest.raises(ValueError, match="不能包含目录路径"):
        local_course.configure_course(profile)


@pytest.mark.parametrize("change", [{"sub_id": "wrong"}, {"prompt_sha256": "stale"}, {"finish_reason": "length"}])
def test_connector_result_requires_matching_source_and_complete_output(tmp_path, monkeypatch, change):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    creds = SimpleNamespace(stuid="test", uispsw="test-only-password")
    with local_course.local_database(creds) as (db, checkpoint):
        db.upsert_course("4048", local_course.TITLE, local_course.TEACHER)
        db.insert_lecture("test-sub", "4048", "测试", "2024-01-01")
        db.update_transcript("test-sub", "已经验证的课程内容")
        local_course.save_meta(db, "test-sub", "audio", {"expected_seconds": 100, "decoded_seconds": 100})
        local_course.save_meta(db, "test-sub", "ocr", {"source_pages": 0})
        request = local_course.summary_request(db, {"sub_id": "test-sub"})
        result = {"sub_id": "test-sub", "prompt_sha256": request["prompt_sha256"],
                  "finish_reason": "stop", "text": "# 测试标题\n\n课程笔记正文", "model": "test-model", **change}
        path = tmp_path / "result.json"
        path.write_text(json.dumps(result))
        with pytest.raises(ValueError, match="指纹或完成状态不符"):
            local_course.import_summary(db, checkpoint, {"sub_id": "test-sub"}, path)
        assert not db.get_lecture("test-sub").get("summary")
