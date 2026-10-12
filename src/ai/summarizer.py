"""Course generation using only saved models, in their configured order."""
from openai import OpenAI

from src.ai.summary_prompt import SYSTEM_PROMPT, summary_user_content
from src.ai.title import clean_title
from src.ai.model_policy import IncompleteSummaryError, request_options, check_response
from src.runtime import config


class Summarizer:
    def __init__(self, providers=None):
        self.providers = config.resolve_model_providers() if providers is None else providers
        if not self.providers:
            raise ValueError("No model provider available. 请在模型与 API 中保存模型及 API Key。")
        self._clients = {
            p["name"]: OpenAI(api_key=p["api_key"], base_url=p["base_url"],
                              timeout=600, max_retries=0)
            for p in self.providers
        }

    def complete_messages(self, messages, *, title=False):
        """Try saved providers/models in order and return the actual receipt."""
        errors = []
        last_error = None
        failures = []
        for provider in self.providers:
            for model in provider["models"]:
                model_id = f"{provider['name']}/{model}"
                try:
                    for budget in (32768, 65536):
                        options = request_options(provider, model, title=title, budget=budget)
                        print(f"[Summarizer] Trying {model_id}", flush=True)
                        response = self._clients[provider["name"]].chat.completions.create(
                            model=model, messages=messages, **options)
                        choice = response.choices[0] if response.choices else None
                        reason = choice.finish_reason if choice else "empty_choices"
                        try:
                            check_response(provider, model, getattr(response, "model", None),
                                           choice.message.content if choice else None, reason)
                        except IncompleteSummaryError:
                            if reason == "length" and not title and budget == 32768 and "max_completion_tokens" in options:
                                continue
                            raise
                        if title and not clean_title(choice.message.content):
                            raise ValueError("模型没有返回可用标题")
                        return response, provider, model, options
                except Exception as exc:
                    last_error = exc
                    failures.append(exc)
                    detail = str(exc)
                    for configured in self.providers:
                        key = configured.get("api_key")
                        if key:
                            detail = detail.replace(key, "***")
                    print(f"[Summarizer] {model_id} failed: {type(exc).__name__}: {detail}", flush=True)
                    errors.append(f"{model_id}: {detail}")
        if isinstance(last_error, IncompleteSummaryError) and failures and all(isinstance(exc, IncompleteSummaryError) and exc.finish_reason == last_error.finish_reason
                            for exc in failures):
            last_error.attempt_errors = errors
            raise last_error
        failure = RuntimeError("All LLM models failed:\n" + "\n".join(errors))
        failure.failures = failures
        raise failure from last_error

    def summarize(self, title, content):
        if not content or not content.strip():
            return "（内容为空）", ""
        response, provider, model, _ = self.complete_messages([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": summary_user_content(title, content)},
        ])
        result = response.choices[0].message.content
        model_id = f"{provider['name']}/{getattr(response, 'model', None) or model}"
        usage = getattr(response, "usage", None)
        print(f"[Summarizer] Done ({model_id}): {len(content)} chars input → {len(result)} chars output "
              f"(tokens: prompt={getattr(usage, 'prompt_tokens', None)}, completion={getattr(usage, 'completion_tokens', None)})", flush=True)
        return result, model_id

    def generate_title(self, material):
        if not material or not material.strip():
            return "", ""
        system = (
            "你是课程助教。根据用户提供的课程材料（PPT 课件文字与录音转录节选），"
            "为这节课生成一个简短准确的笔记标题。要求：不超过 20 字；概括本节课的"
            "核心内容而非课程名；PPT 文字是真实课件内容，比录音转录更可靠，以其为准；"
            "只输出标题本身，不要日期、课次号、引号或任何解释；不要给整个标题加书名号，"
            "但标题中提到的著作名必须保留其书名号。"
        )
        try:
            response, provider, model, _ = self.complete_messages([
                {"role": "system", "content": system},
                {"role": "user", "content": f"课程材料如下：\n\n{material}"},
            ], title=True)
            title = clean_title(response.choices[0].message.content)
            if title:
                model_id = f"{provider['name']}/{getattr(response, 'model', None) or model}"
                print(f"[Summarizer] Title by {model_id}: {title}", flush=True)
                return title, model_id
        except Exception:
            pass
        print("[Summarizer] title generation skipped — all models failed", flush=True)
        return "", ""
