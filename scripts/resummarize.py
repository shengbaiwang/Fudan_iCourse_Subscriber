#!/usr/bin/env python3
"""Regenerate one or more existing lecture summaries with a chosen LLM.

This deliberately does not log in to iCourse or instantiate ASR/OCR.  A
rerun consumes the transcript and accepted PPT OCR already in the encrypted
database, then replaces only ``summary`` and ``summary_model``.
"""

from __future__ import annotations

import os
import sys

from src.ai import bucketer
from src.ai.summarizer import Summarizer
from src.data.database import Database


def _lecture_ids() -> list[str]:
    values = [item.strip() for item in os.environ.get("RESUMMARIZE_SUB_IDS", "").split(",")]
    result = list(dict.fromkeys(item for item in values if item))
    if not result:
        raise ValueError("RESUMMARIZE_SUB_IDS 必须包含至少一个课次 ID")
    if len(result) > 20:
        raise ValueError("一次最多重新生成 20 个课次")
    return result


def run() -> int:
    lecture_ids = _lecture_ids()
    db = Database()
    summarizer = Summarizer()
    completed = 0

    for sub_id in lecture_ids:
        lecture = db.get_lecture(sub_id)
        if lecture is None:
            print(f"::error::课次不存在：{sub_id}", file=sys.stderr)
            continue
        transcript = str(lecture.get("transcript") or "").strip()
        if not transcript:
            print(
                f"::error::{sub_id} 没有可用转录，无法只重写摘要；请先完成原始处理。",
                file=sys.stderr,
            )
            continue

        pages = db.get_done_ppt_pages(sub_id)
        prompt_text, mode = bucketer.assemble(transcript, None, pages)
        course = str(lecture.get("course_title") or lecture.get("course_id") or sub_id)
        print(
            f"[Re-summarize] {sub_id}: {course} — mode={mode}, "
            f"prompt={len(prompt_text)} chars",
            flush=True,
        )
        try:
            summary, model_used = summarizer.summarize(course, prompt_text)
            db.update_summary(sub_id, summary, model_used)
            db.mark_processed(sub_id)
            db.clear_error(sub_id)
            completed += 1
            print(f"[Re-summarize] Done {sub_id} with {model_used}", flush=True)
        except Exception as exc:
            db.update_error(sub_id, "summarize", str(exc))
            print(f"::error::{sub_id} 重新生成失败：{exc}", file=sys.stderr)

    if completed != len(lecture_ids):
        print(
            f"::error::仅完成 {completed}/{len(lecture_ids)} 个课次，详见上方日志。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
