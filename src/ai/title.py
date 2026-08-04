"""AI note-title helpers: source-material assembly and output cleanup.

Kept dependency-free on purpose (no openai / PIL imports) so both the
summarizer and the unit tests can use it.  The title is derived from
reliable sources only — the PPT OCR text (real course slides) first, with a
transcript excerpt as supporting context.
"""

from __future__ import annotations

import re

# PPT is the most reliable signal (actual course content) and is small, so it
# gets a generous budget; the noisier transcript only contributes an excerpt.
_PPT_LIMIT = 3000
_TRANSCRIPT_LIMIT = 1500

_TITLE_PREFIX_PATTERN = re.compile(r"^(标题|题目|课题|课题名)[:：]\s*")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def clean_title(raw: str | None, *, max_length: int = 30) -> str:
    """Normalize raw LLM output into a bare note title ("" when unusable).

    Takes the first line, drops a leading "标题：" style prefix and
    surrounding quotes/brackets/markdown marks, collapses whitespace and
    truncates to ``max_length`` characters.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    line = _TITLE_PREFIX_PATTERN.sub("", line)
    line = line.strip("「」『』“”‘’\"'《》<>*#、。：:，, ")
    line = _WHITESPACE_PATTERN.sub(" ", line).strip()
    if not line:
        return ""
    return line[:max_length]


def split_generated_title(summary: str | None) -> tuple[str, str]:
    """Split the model's leading ``# <title>`` line off its summary output.

    The summarization prompt asks for the note title as the first line, so
    one LLM call yields both title and body — no extra request needed.
    Returns ``(title, body)``; when the output does not lead with exactly one
    level-1 heading (or it cleans to nothing), returns ``("", summary)`` so
    callers keep the original text and fall back to a dedicated title call.
    """
    original = str(summary or "")
    first_line, separator, rest = original.strip().partition("\n")
    if not first_line.startswith("# ") or first_line.startswith("##"):
        return ("", original)
    title = clean_title(first_line[2:])
    if not title:
        return ("", original)
    body = rest.strip() if separator else ""
    return (title, body or original.strip())


def build_title_material(
    course_title: str | None,
    transcript: str | None,
    ppt_pages: list[dict] | None,
    *,
    ppt_limit: int = _PPT_LIMIT,
    transcript_limit: int = _TRANSCRIPT_LIMIT,
) -> str:
    """Assemble the reliable-source excerpt for the title-generation call.

    ``ppt_pages`` are the OCR-done pages (``{text, ...}``); their joined text
    leads the material.  The transcript contributes only its opening excerpt
    — enough for topic context without drowning the slides.  Returns "" when
    there is nothing usable (callers then skip the LLM call).
    """
    parts: list[str] = []
    course = str(course_title or "").strip()
    if course:
        parts.append(f"课程：{course}")
    ppt_text = "\n".join(
        str(page.get("text") or "").strip()
        for page in (ppt_pages or [])
        if str(page.get("text") or "").strip()
    )
    if ppt_text:
        parts.append("【PPT 课件文字】\n" + ppt_text[:ppt_limit])
    transcript_text = str(transcript or "").strip()
    if transcript_text:
        parts.append("【录音转录（节选）】\n" + transcript_text[:transcript_limit])
    # Only the course-name line is no basis for a title — require real content.
    return "\n\n".join(parts) if len(parts) > 1 else ""
