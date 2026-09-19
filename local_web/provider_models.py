"""Fetch an OpenAI-compatible model directory without persisting credentials."""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

import certifi

from src.runtime.model_config import normalize_base_url


class ModelDirectoryError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward the supplied Authorization header to another endpoint.
        return None


def fetch_provider_models(base_url: str, api_key: str) -> dict:
    endpoint = normalize_base_url(base_url) + "/models"
    api_key = api_key.strip()
    if not api_key:
        raise ModelDirectoryError("请重新输入 API Key；GitHub 无法读回已保存的密钥。")
    request = urllib.request.Request(endpoint, headers={
        "Authorization": f"Bearer {api_key}", "Accept": "application/json",
    })
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
        NoRedirect(),
    )
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ModelDirectoryError("模型目录过大，请手动添加模型 ID。")
        data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        hints = {401: "API Key 无效", 403: "API Key 无权限", 404: "该地址不提供模型目录，可手动添加模型 ID", 429: "请求过于频繁，请稍后重试"}
        raise ModelDirectoryError(f"获取模型失败（HTTP {code}）：{hints.get(code, '请核对 API 地址，或稍后重试')}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ModelDirectoryError("无法连接模型目录，请检查网络与 API 地址后重试。") from None
    except (ValueError, UnicodeError):
        raise ModelDirectoryError("模型目录不是有效 JSON，可手动添加模型 ID。") from None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ModelDirectoryError("模型目录格式不兼容：需要 data 数组。可手动添加模型 ID。")
    ids = []
    seen = set()
    for row in data["data"]:
        model = row.get("id") if isinstance(row, dict) else None
        if isinstance(model, str) and model.strip() and len(model.strip()) <= 200:
            model = model.strip()
            if model not in seen:
                seen.add(model)
                ids.append(model)
    return {"models": ids}
