"""LLM-based course lecture summarization via ModelScope API."""

import time

from openai import OpenAI

from src.ai.summary_prompt import SYSTEM_PROMPT, summary_user_content
from src.ai.title import clean_title
from src.runtime import config

class Summarizer:
    """Course lecture summarizer with multi-provider fallback.

    Iterates config.MODEL_PROVIDERS in declared order. Within each provider,
    tries each model in declared order. Returns the first successful result.
    Setting only DASHSCOPE_API_KEY still works because the default
    MODEL_PROVIDERS list ships a modelscope entry that reads it.
    """

    def __init__(self):
        self.providers = config.resolve_model_providers()
        if not self.providers:
            raise ValueError(
                "No model provider available. "
                "Set at least one provider's API key (e.g. DASHSCOPE_API_KEY)."
            )
        self._clients = {
            p["name"]: OpenAI(api_key=p["api_key"], base_url=p["base_url"])
            for p in self.providers
        }

    def _call_llm(self, client: OpenAI, model: str,
                  title: str, content: str) -> str:
        t0 = time.time()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": summary_user_content(title, content),
                },
            ],
            # temperature=0.3,
            timeout=180,
        )
        if not response.choices:
            raise ValueError("API returned empty choices — likely content filter or quota exceeded")
        result = response.choices[0].message.content
        if not isinstance(result, str) or not result.strip():
            raise ValueError("模型返回空内容")
        if response.choices[0].finish_reason in {"length", "content_filter"}:
            raise ValueError("模型输出不完整，尝试下一个模型")
        elapsed = time.time() - t0
        # Token usage helps explain run cost — every provider's billing is
        # token-based, and rate-limit decisions key off prompt size much
        # more than character count.  Some providers (OpenAI-compatible)
        # leave usage None on streaming or error paths, so fall back to a
        # plain "no usage" line so the summary still prints.
        usage = getattr(response, "usage", None)
        if usage is not None:
            print(
                f"[Summarizer] Done ({model}): "
                f"{len(content)} chars input → {len(result)} chars output"
                f" in {elapsed:.0f}s "
                f"(tokens: prompt={getattr(usage,'prompt_tokens','?')}, "
                f"completion={getattr(usage,'completion_tokens','?')})"
            )
        else:
            print(
                f"[Summarizer] Done ({model}): {len(content)} chars input"
                f" → {len(result)} chars output in {elapsed:.0f}s"
            )
        return result

    def summarize(self, title: str, content: str) -> tuple[str, str]:
        """Summarize lecture, trying providers in MODEL_PROVIDERS order.

        Returns (summary, model_used) where model_used is "{provider}/{model}".

        Raises:
            RuntimeError: if all providers/models fail.
        """
        if not content or not content.strip():
            return ("（内容为空）", "")

        errors = []
        for provider in self.providers:
            client = self._clients[provider["name"]]
            for model in provider["models"]:
                model_id = f"{provider['name']}/{model}"
                try:
                    result = self._call_llm(client, model, title, content)
                    return (result, model_id)
                except Exception as e:
                    print(f"[Summarizer] {model_id} failed: "
                          f"{type(e).__name__}: {e}")
                    errors.append(f"{model_id}: {e}")

        raise RuntimeError(
            "All LLM models failed:\n" + "\n".join(errors)
        )

    def generate_title(self, material: str) -> tuple[str, str]:
        """Generate a concise note title from reliable source material.

        Uses the same provider/model fallback chain as ``summarize`` but is
        deliberately forgiving: any failure returns ``("", "")`` so callers
        keep the previous title instead of failing the whole lecture.

        Returns ``(title, model_used)``; title is "" when nothing usable
        came back.  Never raises.
        """
        if not material or not material.strip():
            return ("", "")
        system = (
            "你是课程助教。根据用户提供的课程材料（PPT 课件文字与录音转录节选），"
            "为这节课生成一个简短准确的笔记标题。要求：不超过 20 字；概括本节课的"
            "核心内容而非课程名；PPT 文字是真实课件内容，比录音转录更可靠，以其为准；"
            "只输出标题本身，不要日期、课次号、引号或任何解释；不要给整个标题加书名号，"
            "但标题中提到的著作名必须保留其书名号。"
        )
        for provider in self.providers:
            client = self._clients[provider["name"]]
            for model in provider["models"]:
                model_id = f"{provider['name']}/{model}"
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": f"课程材料如下：\n\n{material}"},
                        ],
                        timeout=60,
                    )
                    if not response.choices:
                        raise ValueError("API returned empty choices")
                    title = clean_title(response.choices[0].message.content)
                    if title:
                        print(f"[Summarizer] Title by {model_id}: {title}")
                        return (title, model_id)
                except Exception as e:
                    print(
                        f"[Summarizer] title via {model_id} failed: "
                        f"{type(e).__name__}: {e}"
                    )
        print("[Summarizer] title generation skipped — all models failed")
        return ("", "")
