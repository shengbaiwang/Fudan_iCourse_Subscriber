"""Request rules shared by cloud, terminal and local talk generation."""
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

GENERATION_DEFAULTS = {
    "timeout_seconds": 600,
    "title_timeout_seconds": 60,
    "max_retries": 2,
    "retry_delay_seconds": 2,
    "retry_max_delay_seconds": 60,
    "output_tokens": 32768,
    "max_output_tokens": 65536,
    "token_parameter": "auto",
}


def generation_policy(value=None):
    """Normalize saved per-provider request settings, including old configs."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("generation 必须是 object")
    unknown = set(value) - GENERATION_DEFAULTS.keys()
    if unknown:
        raise ValueError(f"未知生成设置：{', '.join(sorted(unknown))}")
    policy = {**GENERATION_DEFAULTS, **value}
    for key, minimum, maximum in (
        ("timeout_seconds", 1, 86400), ("title_timeout_seconds", 1, 86400),
        ("max_retries", 0, 10), ("retry_delay_seconds", 0, 3600),
        ("retry_max_delay_seconds", 0, 3600),
        ("output_tokens", 1, 2147483647), ("max_output_tokens", 1, 2147483647),
    ):
        number = policy[key]
        if isinstance(number, bool) or not isinstance(number, int) or not minimum <= number <= maximum:
            raise ValueError(f"{key} 必须为 {minimum}–{maximum} 的整数")
    if policy["max_output_tokens"] < policy["output_tokens"]:
        raise ValueError("扩容上限不能小于初始输出预算")
    if policy["retry_max_delay_seconds"] < policy["retry_delay_seconds"]:
        raise ValueError("退避上限不能小于初始重试间隔")
    if not isinstance(policy["token_parameter"], str) or policy["token_parameter"] not in {"auto", "max_tokens", "max_completion_tokens"}:
        raise ValueError("token_parameter 必须为 auto、max_tokens 或 max_completion_tokens")
    return policy


def _error_chain(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__


def transient_error(error):
    for cause in _error_chain(error):
        status = getattr(cause, "status_code", getattr(cause, "code", None))
        if isinstance(status, int):
            return status in {408, 409, 429} or status >= 500
        if isinstance(cause, (TimeoutError, ConnectionError)) or type(cause).__name__ in {
            "APIConnectionError", "APITimeoutError", "URLError", "RemoteDisconnected",
        }:
            return True
    return False


def _retry_delay(error, policy, attempt):
    delay = policy["retry_delay_seconds"] * 2 ** attempt
    for cause in _error_chain(error):
        headers = getattr(getattr(cause, "response", None), "headers", None)
        if headers is None:
            headers = getattr(cause, "headers", None)
        value = headers.get("retry-after", headers.get("Retry-After")) if headers is not None else None
        if value is not None:
            try:
                delay = max(delay, float(value))
            except (TypeError, ValueError):
                try:
                    delay = max(delay, parsedate_to_datetime(value).timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
    return max(0, min(delay, policy["retry_max_delay_seconds"]))


def complete_with_recovery(provider, model, call, *, title=False):
    """Retry this saved model before fallback; never accept truncated output.

    ``call(options)`` must validate the response before returning. SDK and
    stdlib clients share this recovery loop and saved provider settings.
    """
    policy = generation_policy(provider.get("generation"))
    budget = 256 if title else policy["output_tokens"]
    ceiling = 256 if title else policy["max_output_tokens"]
    while True:
        options = request_options(provider, model, title=title, budget=budget)
        for attempt in range(policy["max_retries"] + 1):
            try:
                return call(options), options
            except IncompleteSummaryError as exc:
                if exc.finish_reason != "length" or budget >= ceiling:
                    raise
                budget = min(budget * 2, ceiling)
                print(f"[LLM] {provider.get('name', '')}/{model}: 输出截断，扩容至 {budget} tokens 后重新生成", flush=True)
                break
            except Exception as exc:
                if not transient_error(exc) or attempt >= policy["max_retries"]:
                    raise
                delay = _retry_delay(exc, policy, attempt)
                print(f"[LLM] {provider.get('name', '')}/{model}: 临时故障，{delay:g} 秒后重试 ({attempt + 1}/{policy['max_retries']})", flush=True)
                time.sleep(delay)


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


def request_options(provider, model, *, title=False, budget=None):
    check_model(provider, model)
    host = urlparse(provider.get("base_url", provider.get("default_base_url", ""))).hostname
    policy = generation_policy(provider.get("generation"))
    options = {"timeout": policy["title_timeout_seconds"] if title else policy["timeout_seconds"]}
    parameter = policy["token_parameter"]
    if parameter == "auto":
        parameter = "max_completion_tokens" if host in {"api.xiaomimimo.com", "token-plan-cn.xiaomimimo.com"} else "max_tokens"
    options[parameter] = 256 if title else budget if budget is not None else policy["output_tokens"]
    if host in {"api.xiaomimimo.com", "token-plan-cn.xiaomimimo.com"}:
        options["extra_body"] = {"thinking": {"type": "disabled"}}
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
