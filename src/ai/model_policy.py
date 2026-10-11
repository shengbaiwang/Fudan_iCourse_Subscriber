"""Request rules shared by cloud, terminal and local talk generation."""
from urllib.parse import urlparse


class IncompleteSummaryError(ValueError):
    def __init__(self, reason):
        self.finish_reason = reason
        super().__init__(f"摘要未正常完成（finish_reason={reason!r}），禁止导入截断结果")


def is_openrouter(provider):
    return (urlparse(provider.get("base_url", provider.get("default_base_url", ""))).hostname == "openrouter.ai"
            or provider.get("name", "").lower() == "openrouter")


def check_model(provider, model):
    if model not in provider["models"]:
        raise ValueError("模型不在该供应商的已保存配置中")
    if is_openrouter(provider) and (
        model.lower().split(":", 1)[0] in {"openrouter/auto", "openrouter/free", "openrouter/bodybuilder", "openrouter/auto-exacto"}
        or model.startswith(("@", "~")) or "*" in model
    ):
        raise ValueError("OpenRouter 必须使用已添加的具体模型 ID，不能自动选模或使用预设")


def request_options(provider, model, *, title=False, budget=32768):
    check_model(provider, model)
    host = urlparse(provider.get("base_url", provider.get("default_base_url", ""))).hostname
    options = {"timeout": 60 if title else 180}
    if host in {"api.xiaomimimo.com", "token-plan-cn.xiaomimimo.com"}:
        options.update(timeout=60 if title else 600,
                       max_completion_tokens=256 if title else budget,
                       extra_body={"thinking": {"type": "disabled"}})
    if is_openrouter(provider):
        # Only this saved model may answer; fallback stays in our saved order.
        options["extra_body"] = {"models": [model]}
    return options


def check_response(provider, model, actual_model, content, reason):
    if reason != "stop":
        raise IncompleteSummaryError(reason)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("模型返回空内容")
    if is_openrouter(provider) and actual_model != model:
        raise ValueError(f"OpenRouter 返回了请求之外的模型：{actual_model!r}；拒绝使用该结果")
