"""Offline regressions for saved order and the OpenRouter model whitelist."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.ai.model_policy import check_model
from src.ai.summarizer import Summarizer
from src.runtime.model_config import validate_model_config
from local_web.talks import complete_talk, TalkLLMError
from scripts.summarize_local_requests import generate_result

ROOT = Path(__file__).resolve().parents[1]


def provider(name, models, host=None):
    return {"name": name, "models": models, "base_url": host or f"https://{name.lower()}.test/v1",
            "api_key": "synthetic-key", "api_key_env": f"LLM_{name.upper()}_API_KEY"}


def fake_summarizer(providers, responses):
    calls = []
    summarizer = Summarizer.__new__(Summarizer)
    summarizer.providers = providers
    def create(name, **kwargs):
        calls.append((name, kwargs))
        result = responses.get((name, kwargs["model"]), RuntimeError("synthetic failure"))
        if isinstance(result, BaseException):
            raise result
        actual, reason, text = result
        return SimpleNamespace(model=actual, choices=[SimpleNamespace(
            finish_reason=reason, message=SimpleNamespace(content=text))], usage=None)
    summarizer._clients = {p["name"]: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda name=p["name"], **kwargs: create(name, **kwargs)))) for p in providers}
    return summarizer, calls


@pytest.mark.parametrize("task", ["summary", "title", "terminal"])
def test_every_generation_starts_at_first_saved_model_and_falls_back_in_order(task):
    chain = [provider("Mimo", ["mimo-pro", "mimo-fast"]), provider("Kimi", ["kimi-k3"]),
             provider("DeepSeek", ["deepseek-flash"])]
    s, calls = fake_summarizer(chain, {
        ("Mimo", "mimo-pro"): ("mimo-pro", "content_filter", ""),
        ("Kimi", "kimi-k3"): ("kimi-k3", "stop", "正确标题\n完整正文"),
    })
    if task == "title":
        assert s.generate_title("verified material")[1] == "Kimi/kimi-k3"
    elif task == "terminal":
        receipt = generate_result(s, {"messages": [], "course_id": "1", "sub_id": "10", "prompt_sha256": "hash"})
        assert receipt["provider"] == "Kimi"
        assert receipt["requested_model"] == "kimi-k3"
    else:
        assert s.summarize("课程", "verified material")[1] == "Kimi/kimi-k3"
    assert [(name, args["model"]) for name, args in calls] == [
        ("Mimo", "mimo-pro"), ("Mimo", "mimo-fast"), ("Kimi", "kimi-k3")]


def test_legacy_workflow_selection_cannot_reorder_chain_or_limit_injected_keys():
    chain = [provider("Mimo", ["mimo-pro"]), provider("Kimi", ["kimi-k3"])]
    document = {"version": 1, "providers": [{k: v for k, v in p.items() if k != "api_key"} for p in chain]}
    env = {**os.environ, "MODEL_PROVIDERS_JSON": json.dumps(document),
           "SUMMARY_PROVIDER": "OpenRouter", "SUMMARY_MODEL": "not-added/model",
           "SECRETS_CONTEXT": json.dumps({"LLM_MIMO_API_KEY": "mimo-test-key", "LLM_KIMI_API_KEY": "kimi-test-key",
                                           "UNRELATED": "private-unrelated"})}
    result = subprocess.run([sys.executable, "scripts/provider_env.py", "--", sys.executable, "-c",
        "import os,json; from src.runtime import config; "
        "print(json.dumps({'order':[p['name'] for p in config.resolve_model_providers()], "
        "'keys':[bool(os.environ.get(p['api_key_env'])) for p in config.MODEL_PROVIDERS], "
        "'context':bool(os.environ.get('SECRETS_CONTEXT')),'unrelated':bool(os.environ.get('UNRELATED'))}))"],
        cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"order": ["Mimo", "Kimi"], "keys": [True, True], "context": False, "unrelated": False}
    assert "test-key" not in result.stdout


@pytest.mark.parametrize("model", ["openrouter/auto", "openrouter/auto:online", "OPENROUTER/AUTO", "openrouter/free", "openrouter/bodybuilder", "@preset/test", "~test", "vendor/*"])
def test_openrouter_routers_and_presets_are_rejected_before_any_call(model):
    p = provider("OpenRouter", [model], "https://openrouter.ai/api/v1")
    with pytest.raises(ValueError, match="具体模型"):
        validate_model_config({"providers": [p]})
    s, calls = fake_summarizer([p], {})
    with pytest.raises(RuntimeError):
        s.summarize("课程", "material")
    assert not calls


def test_unadded_openrouter_model_is_rejected_before_request():
    p = provider("OpenRouter", ["saved/model"], "https://openrouter.ai/api/v1")
    with pytest.raises(ValueError, match="已保存"):
        check_model(p, "not-added/model")


@pytest.mark.parametrize("task", ["summary", "title"])
def test_openrouter_requests_only_saved_id_and_rejects_another_models_response(task):
    chain = [provider("OpenRouter", ["saved/model"], "https://openrouter.ai/api/v1"), provider("Kimi", ["kimi-k3"])]
    s, calls = fake_summarizer(chain, {
        ("OpenRouter", "saved/model"): ("not-added/model", "stop", "不允许的结果"),
        ("Kimi", "kimi-k3"): ("kimi-k3", "stop", "正确结果"),
    })
    method = s.generate_title if task == "title" else lambda text: s.summarize("课程", text)
    assert method("material") == ("正确结果", "Kimi/kimi-k3")
    assert calls[0][1]["model"] == "saved/model"
    assert calls[0][1]["extra_body"] == {"models": ["saved/model"]}
    assert [args["model"] for _, args in calls] == ["saved/model", "kimi-k3"]


def test_talk_uses_order_for_notes_and_for_titles_without_locking_to_previous_winner():
    chain = [provider("Mimo", ["mimo-pro"]), provider("Kimi", ["kimi-k3"]), provider("DeepSeek", ["deepseek-flash"])]
    calls = []
    def chat(base, model, key, system, user, **kwargs):
        calls.append((model, kwargs["title"]))
        if model == "mimo-pro" and not kwargs["title"]:
            raise TalkLLMError("synthetic quota exhausted")
        return "# 标题\n\n正文" if not kwargs["title"] else "回到首选模型的标题"
    from local_web.talks import generate_talk_title
    with patch("local_web.talks.chat_complete", side_effect=chat):
        assert complete_talk(chain, "system", "material")[1] == "Kimi/kimi-k3"
        assert generate_talk_title(chain, "transcript") == "回到首选模型的标题"
    assert calls == [("mimo-pro", False), ("kimi-k3", False), ("mimo-pro", True)]


def test_talk_openrouter_wire_request_has_exact_whitelist_and_rejects_unadded_response():
    from local_web.talks import chat_complete
    p = provider("OpenRouter", ["saved/model"], "https://openrouter.ai/api/v1")
    sent = []
    from unittest.mock import MagicMock
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({"model": "not-added/model", "choices": [
        {"finish_reason": "stop", "message": {"content": "unexpected body"}}]}).encode()
    def open_request(request, **kwargs):
        sent.append(json.loads(request.data))
        return response
    with patch("local_web.talks.urllib.request.urlopen", side_effect=open_request):
        with pytest.raises(ValueError, match="请求之外"):
            chat_complete(p["base_url"], "saved/model", "synthetic-key", "system", "material", provider=p)
    assert sent[0]["model"] == "saved/model"
    assert sent[0]["models"] == ["saved/model"]
