from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

import certifi


class ProviderTestError(RuntimeError):
    pass


def _redact(value: str, api_key: str) -> str:
    return value.replace(api_key, "***") if api_key else value


def test_provider(
    base_url: str,
    model: str,
    api_key: str,
    timeout: int = 30,
) -> dict[str, Any]:
    """Make a minimal OpenAI-compatible request with a user-supplied key."""
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 2,
            "temperature": 0,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Fudan-iCourse-Subscriber-local-web/0.2",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=ssl.create_default_context(cafile=certifi.where()),
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise ProviderTestError(
            f"模型 API {exc.code}: {_redact(detail, api_key)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderTestError(f"无法连接模型 API: {exc.reason}") from exc
    except (ValueError, TimeoutError) as exc:
        raise ProviderTestError(f"模型 API 响应无效: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderTestError("模型 API 响应不是 JSON object")
    choices = data.get("choices") or []
    if (
        not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], dict)
    ):
        raise ProviderTestError("模型 API 返回成功，但没有 choices")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise ProviderTestError("模型 API 返回的 message 格式无效")
    return {
        "ok": True,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "model": str(data.get("model") or model),
        "reply": str(message.get("content") or "")[:100],
    }
