"""Validation and serialization for user-managed LLM provider settings.

The document deliberately contains no API keys.  It only names the Actions
Secret that holds each key, so the same JSON is safe to cache locally and to
record in database metadata for troubleshooting.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse


CONFIG_VERSION = 1
PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")
CUSTOM_KEY_RE = re.compile(r"^LLM_[A-Z0-9_]{1,80}_API_KEY$")
CUSTOM_URL_RE = re.compile(r"^LLM_[A-Z0-9_]{1,80}_BASE_URL$")
LEGACY_KEY_ENVS = {
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
}
LEGACY_URL_ENVS = {
    "DASHSCOPE_BASE_URL",
    "DEEPSEEK_BASE_URL",
    "GEMINI_BASE_URL",
}


def normalize_base_url(value: Any, provider_name: str = "供应商") -> str:
    """Return a safe OpenAI-compatible base URL or raise ValueError."""
    if not isinstance(value, str):
        raise ValueError(f"{provider_name} 的 Base URL 必须是字符串")
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    try:
        invalid = (
            parsed.scheme != "https"
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in base_url
            or "#" in base_url
            or "\\" in base_url
            or any(character.isspace() for character in base_url)
            or parsed.port is not None and not 1 <= parsed.port <= 65535
        )
    except ValueError:
        invalid = True
    if invalid:
        raise ValueError(
            f"{provider_name} 的 Base URL 必须是无凭据和查询参数的 HTTPS URL"
        )
    return base_url


def validate_model_config(value: Any) -> dict[str, Any]:
    """Return a normalized versioned config document or raise ValueError."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MODEL_PROVIDERS_JSON 不是合法 JSON: {exc}") from exc
    if isinstance(value, list):
        value = {"version": CONFIG_VERSION, "providers": value}
    if not isinstance(value, dict):
        raise ValueError("模型配置必须是 JSON object")
    version = value.get("version", CONFIG_VERSION)
    valid_version = (
        type(version) is int and version == CONFIG_VERSION
    ) or (
        isinstance(version, str) and version == str(CONFIG_VERSION)
    )
    if not valid_version:
        raise ValueError(f"不支持的模型配置版本：{version}")
    raw_providers = value.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        raise ValueError("至少需要一个模型供应商")
    if len(raw_providers) > 20:
        raise ValueError("模型供应商不能超过 20 个")

    providers: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_providers, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 个供应商必须是 object")
        name = str(raw.get("name") or "").strip()
        if not PROVIDER_NAME_RE.fullmatch(name):
            raise ValueError(f"供应商名称不合法：{name!r}")
        if name in names:
            raise ValueError(f"供应商名称重复：{name}")
        names.add(name)

        api_key_env = str(raw.get("api_key_env") or "").strip().upper()
        if (
            api_key_env not in LEGACY_KEY_ENVS
            and not CUSTOM_KEY_RE.fullmatch(api_key_env)
        ):
            raise ValueError(
                f"{name} 的 API Key Secret 必须使用现有兼容名称，或符合 "
                "LLM_*_API_KEY"
            )
        base_url = normalize_base_url(
            raw.get("base_url") or raw.get("default_base_url") or "",
            name,
        )
        raw_models = raw.get("models")
        if not isinstance(raw_models, list):
            raise ValueError(f"{name} 的 models 必须是数组")
        models: list[str] = []
        for item in raw_models:
            if not isinstance(item, str):
                raise ValueError(f"{name} 的模型名称必须是字符串")
            model = item.strip()
            if model and model not in models:
                if len(model) > 200:
                    raise ValueError(f"{name} 的模型名称过长")
                models.append(model)
        if not models:
            raise ValueError(f"{name} 至少需要一个模型")
        if len(models) > 30:
            raise ValueError(f"{name} 的模型不能超过 30 个")

        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{name} 的 enabled 必须是布尔值")
        provider = {
            "name": name,
            "api_key_env": api_key_env,
            "default_base_url": base_url,
            "models": models,
            "enabled": enabled,
        }
        base_url_env = str(raw.get("base_url_env") or "").strip().upper()
        if base_url_env:
            if (
                base_url_env not in LEGACY_URL_ENVS
                and not CUSTOM_URL_RE.fullmatch(base_url_env)
            ):
                raise ValueError(
                    f"{name} 的 Base URL 环境变量必须符合 LLM_*_BASE_URL"
                )
            provider["base_url_env"] = base_url_env
        providers.append(provider)

    if not any(item["enabled"] for item in providers):
        raise ValueError("至少需要启用一个模型供应商")
    return {"version": CONFIG_VERSION, "providers": providers}


def config_json(value: Any) -> str:
    return json.dumps(
        validate_model_config(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def runtime_providers(value: Any) -> list[dict[str, Any]]:
    document = validate_model_config(value)
    return [
        deepcopy({key: val for key, val in provider.items() if key != "enabled"})
        for provider in document["providers"]
        if provider["enabled"]
    ]


def select_runtime_model(
    value: Any, provider_name: str, model_name: str
) -> list[dict[str, Any]]:
    """Return one enabled provider with exactly one approved model.

    The values originate from a workflow-dispatch input, so they must be
    checked against the saved model configuration instead of being treated as
    arbitrary provider/model strings.  Keeping the return shape identical to
    :func:`runtime_providers` lets the normal ``Summarizer`` execute without
    a separate, less-tested API-key path.
    """
    provider_name = str(provider_name or "").strip()
    model_name = str(model_name or "").strip()
    if not provider_name or not model_name:
        raise ValueError("重新生成必须指定供应商和模型")

    document = validate_model_config(value)
    provider = next(
        (item for item in document["providers"] if item["name"] == provider_name),
        None,
    )
    if provider is None or not provider["enabled"]:
        raise ValueError("所选模型供应商不存在或未启用")
    if model_name not in provider["models"]:
        raise ValueError("所选模型不在该供应商的已保存配置中")

    selected = deepcopy({key: val for key, val in provider.items() if key != "enabled"})
    selected["models"] = [model_name]
    return [selected]
