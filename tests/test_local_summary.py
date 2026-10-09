import json
from types import SimpleNamespace

import pytest

from scripts import local_course, summarize_local_requests as worker


def configured_provider(name, host, enabled=True):
    return {"name": name, "api_key_env": "LLM_TEST_API_KEY", "default_base_url": host,
            "models": ["preferred", "fallback"], "enabled": enabled}


def test_default_selects_first_enabled_host_without_key_based_rerouting(monkeypatch):
    from local_web.state import ModelConfigStore
    document = {"version": 1, "providers": [
        configured_provider("disabled", "https://disabled.test/v1", False),
        configured_provider("default", "https://chosen.test/v1"),
        configured_provider("other", "https://other.test/v1")]}
    monkeypatch.setattr(ModelConfigStore, "load", lambda _: document)
    monkeypatch.delenv("LLM_TEST_API_KEY", raising=False)
    selected = local_course.select_summary_provider("default", None)
    assert selected["name"] == "default"
    assert selected["default_base_url"] == "https://chosen.test/v1"
    assert selected["models"] == ["preferred"]


def api_mock(finish="stop", model="actual-model"):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model=model, choices=[SimpleNamespace(
            finish_reason=finish, message=SimpleNamespace(content="# 笔记\n\n完整正文"))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    summarizer = SimpleNamespace(providers=[{"name": "chosen", "models": ["requested"],
        "base_url": "https://chosen.test/v1"}], _clients={"chosen": client})
    return summarizer, calls


@pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls", None])
def test_api_rejects_every_non_stop_result(finish):
    summarizer, _ = api_mock(finish)
    with pytest.raises(ValueError, match="未正常完成"):
        worker.generate_result(summarizer, {"messages": []})


def test_receipt_uses_actual_model_and_exact_verified_messages():
    summarizer, calls = api_mock()
    request = {"sub_id": "101945", "course_id": "4048", "prompt_sha256": "source-hash",
               "messages": [{"role": "user", "content": "已经验收的源材料"}]}
    result = worker.generate_result(summarizer, request)
    assert calls[0]["messages"] == request["messages"]
    assert result["model"] == "chosen/actual-model"
    assert result["requested_model"] == "requested"
    assert worker.complete_result(result, request)
    assert not worker.complete_result({**result, "prompt_sha256": "old-source"}, request)


@pytest.mark.parametrize("host", ["api.xiaomimimo.com", "token-plan-cn.xiaomimimo.com"])
def test_mimo_notes_have_explicit_output_budget_without_thinking(host):
    summarizer, calls = api_mock()
    summarizer.providers[0]["base_url"] = f"https://{host}/v1"
    request = {"sub_id": "101945", "course_id": "4048", "prompt_sha256": "hash", "messages": []}
    result = worker.generate_result(summarizer, request)
    assert calls[0]["max_completion_tokens"] == 32768
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert result["thinking"] == "disabled"
    assert result["finish_reason"] == "stop"


def test_mimo_length_retries_once_and_only_returns_the_complete_answer():
    summarizer, calls = api_mock()
    summarizer.providers[0]["base_url"] = "https://token-plan-cn.xiaomimimo.com/v1"
    create = summarizer._clients["chosen"].chat.completions.create
    def first_truncated(**kwargs):
        response = create(**kwargs)
        if len(calls) == 1:
            response.choices[0].finish_reason = "length"
            response.choices[0].message.content = "被截断的正文"
        return response
    summarizer._clients["chosen"].chat.completions.create = first_truncated
    request = {"sub_id": "101945", "course_id": "4048", "prompt_sha256": "hash", "messages": []}
    result = worker.generate_result(summarizer, request)
    assert [c["max_completion_tokens"] for c in calls] == [32768, 65536]
    assert result["text"] == "# 笔记\n\n完整正文"
    assert result["finish_reason"] == "stop"


def test_mimo_permanent_truncation_is_still_rejected_with_the_reason():
    summarizer, calls = api_mock(finish="length")
    summarizer.providers[0]["base_url"] = "https://token-plan-cn.xiaomimimo.com/v1"
    with pytest.raises(ValueError, match="finish_reason='length'"):
        worker.generate_result(summarizer, {"messages": []})
    assert len(calls) == 2


def test_other_hosts_do_not_receive_mimo_specific_parameters():
    summarizer, calls = api_mock()
    summarizer.providers[0]["base_url"] = "https://api.xiaomimimo.com.example.test/v1"
    worker.generate_result(summarizer, {
        "sub_id": "101945", "course_id": "4048", "prompt_sha256": "hash", "messages": []})
    assert "max_completion_tokens" not in calls[0]
    assert "extra_body" not in calls[0]


def test_terminal_api_overlaps_prepare_and_reuses_existing_complete_result(tmp_path, monkeypatch):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    creds = SimpleNamespace(stuid="test", uispsw="test-only")
    lecture = {"sub_id": "101945", "date": "2024-04-10", "sub_title": "第一讲", "has_playback": True}
    manifest = {"title": local_course.TITLE, "teacher": local_course.TEACHER, "lectures": [lecture]}
    summarizer, calls = api_mock()
    with local_course.running_job(), local_course.local_database(creds) as (db, checkpoint):
        db.upsert_course("4048", local_course.TITLE, local_course.TEACHER)
        db.insert_lecture("101945", "4048", "第一讲", "2024-04-10")
        db.update_transcript("101945", "已通过本机完整性验收的文字")
        local_course.save_meta(db, "101945", "audio", {"expected_seconds": 100, "decoded_seconds": 100})
        local_course.save_meta(db, "101945", "ocr", {"source_pages": 0})
        checkpoint()
        assert worker.process_requests(creds, manifest, [lecture], summarizer, False) == 0
        assert len(calls) == 1
        result = json.loads((tmp_path / "summary-results/101945.json").read_text())
        assert result["finish_reason"] == "stop"
        assert not db.get_lecture("101945")["summary"]  # owner imports the inbox
        assert worker.process_requests(creds, manifest, [lecture], summarizer, False) == 0
        assert len(calls) == 1
    worker.import_if_idle(creds, manifest)
    with local_course.read_snapshot(creds) as db:
        assert db.get_lecture("101945")["summary"]
        assert local_course.meta(db, "101945", "summary")["finish_reason"] == "stop"
    assert (tmp_path / "notes/101945.md").exists()


def test_unverified_material_is_never_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    creds = SimpleNamespace(stuid="test", uispsw="test-only")
    lecture = {"sub_id": "101945", "date": "2024-04-10", "sub_title": "第一讲", "has_playback": True}
    manifest = {"title": local_course.TITLE, "teacher": local_course.TEACHER, "lectures": [lecture]}
    summarizer, calls = api_mock()
    with local_course.local_database(creds) as (db, checkpoint):
        db.upsert_course("4048", local_course.TITLE, local_course.TEACHER)
        db.insert_lecture("101945", "4048", "第一讲", "2024-04-10")
        db.update_transcript("101945", "有文字但未验收")
        checkpoint()
    assert worker.process_requests(creds, manifest, [lecture], summarizer, True) == 1
    assert not calls


def test_one_filtered_lecture_does_not_abort_other_lectures(tmp_path, monkeypatch):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    creds = SimpleNamespace(stuid="test", uispsw="test-only")
    lectures = [{"sub_id": x, "date": "2024-04-10", "sub_title": x, "has_playback": True}
                for x in ("101945", "101947")]
    manifest = {"title": local_course.TITLE, "teacher": local_course.TEACHER, "lectures": lectures}
    summarizer, calls = api_mock()
    create = summarizer._clients["chosen"].chat.completions.create
    def filtered_first(**kwargs):
        response = create(**kwargs)
        if len(calls) == 1:
            response.choices[0].finish_reason = "content_filter"
        return response
    summarizer._clients["chosen"].chat.completions.create = filtered_first
    with local_course.local_database(creds) as (db, checkpoint):
        db.upsert_course("4048", local_course.TITLE, local_course.TEACHER)
        for lecture in lectures:
            sub_id = lecture["sub_id"]
            db.insert_lecture(sub_id, "4048", sub_id, lecture["date"])
            db.update_transcript(sub_id, "已经验收的完整转录")
            local_course.save_meta(db, sub_id, "audio", {"expected_seconds": 100, "decoded_seconds": 100})
            local_course.save_meta(db, sub_id, "ocr", {"source_pages": 0})
        checkpoint()
    assert worker.process_requests(creds, manifest, lectures, summarizer, False) == 1
    assert len(calls) == 2
    assert not (tmp_path / "summary-results/101945.json").exists()
    assert (tmp_path / "summary-results/101947.json").exists()
    assert json.loads((tmp_path / "summary-failures/101945.json").read_text())["finish_reason"] == "content_filter"
    with local_course.read_snapshot(creds) as db:
        assert not db.get_lecture("101945")["summary"]
        assert db.get_lecture("101947")["summary"]
    assert worker.process_requests(creds, manifest, lectures, summarizer, False) == 1
    assert len(calls) == 2  # remembered refusal is not called again after restart


def test_connection_failure_retries_but_content_filter_does_not(monkeypatch):
    from openai import APIConnectionError
    calls = []
    def generate(*_):
        calls.append(None)
        if len(calls) == 1:
            raise APIConnectionError(request=SimpleNamespace(url='https://chosen.test/v1'))
        return {'finish_reason': 'stop'}
    monkeypatch.setattr(worker, 'generate_result', generate)
    monkeypatch.setattr(worker.time, 'sleep', lambda *_: None)
    assert worker.generate_with_retry(None, {'sub_id': '101945'})['finish_reason'] == 'stop'
    assert len(calls) == 2
    def filtered(*_):
        raise worker.IncompleteSummaryError('content_filter')
    monkeypatch.setattr(worker, 'generate_result', filtered)
    with pytest.raises(worker.IncompleteSummaryError):
        worker.generate_with_retry(None, {'sub_id': '101945'})
