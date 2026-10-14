#!/usr/bin/env python3
"""Regenerate one or more existing lecture summaries with a chosen LLM.

This deliberately does not log in to iCourse or instantiate ASR/OCR.  A
rerun consumes the transcript and accepted PPT OCR already in the encrypted
database, updates the active ``summary`` / ``summary_model``, and appends a
new row to ``summary_versions`` — previous versions (from any model,
including this one) are never overwritten.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# GitHub Actions invokes this file by path (``python scripts/resummarize.py``),
# which otherwise puts only ``scripts/`` on ``sys.path``.  Add the repository
# root explicitly so the project packages remain importable from that entry
# point as well as via ``python -m``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ai import bucketer
from src.ai.summarizer import Summarizer
from src.ai.title import build_title_material, split_generated_title
from src.data.database import Database
from src.runtime.progress import emit, note_saved

RERUN_BATCH_SIZE = 20


def batched_lecture_ids(lecture_ids):
    """Keep the whole ordered selection; persist notes within each batch."""
    total_batches = (len(lecture_ids) + RERUN_BATCH_SIZE - 1) // RERUN_BATCH_SIZE
    for start in range(0, len(lecture_ids), RERUN_BATCH_SIZE):
        batch = lecture_ids[start:start + RERUN_BATCH_SIZE]
        print(f"[Re-summarize] Batch {start // RERUN_BATCH_SIZE + 1}/{total_batches}: {len(batch)} lectures", flush=True)
        emit("batch", index=start // RERUN_BATCH_SIZE + 1, total=total_batches, size=len(batch))
        yield from enumerate(batch, start=start + 1)


def _lecture_ids() -> list[str]:
    values = [item.strip() for item in os.environ.get("RESUMMARIZE_SUB_IDS", "").split(",")]
    result = list(dict.fromkeys(item for item in values if item))
    if not result:
        raise ValueError("RESUMMARIZE_SUB_IDS 必须包含至少一个课次 ID")
    return result


def run() -> int:
    lecture_ids = _lecture_ids()
    db = Database()
    summarizer = Summarizer()
    completed = 0
    emit("queue", total=len(lecture_ids))

    for index, sub_id in batched_lecture_ids(lecture_ids):
        emit("lecture", sub_id=sub_id, index=index,
             total=len(lecture_ids))
        lecture = db.get_lecture(sub_id)
        if lecture is None:
            emit("failed", sub_id=sub_id)
            print(f"::error::课次不存在：{sub_id}", file=sys.stderr)
            continue
        transcript = str(lecture.get("transcript") or "").strip()
        if not transcript:
            emit("failed", sub_id=sub_id)
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
            # The prompt embeds the note title as the output's first line —
            # one call covers summary + title, saving a second API request.
            ai_title, body = split_generated_title(summary)
            db.update_summary(sub_id, body, model_used)
            db.mark_processed(sub_id)
            db.clear_error(sub_id)
            # Fall back to a dedicated tiny title call only when the summary
            # did not carry a usable title.  A title failure never fails the rerun.
            try:
                title = ai_title.strip()
                if not title:
                    title, _title_model = summarizer.generate_title(
                        build_title_material(course, transcript, pages)
                    )
                db.update_ai_title(sub_id, title)
            except Exception as title_exc:
                print(
                    f"::warning::{sub_id} 标题生成失败：{title_exc}",
                    file=sys.stderr,
                )
            completed += 1
            note_saved(db, sub_id)
            print(f"[Re-summarize] Done {sub_id} with {model_used}", flush=True)
        except Exception as exc:
            db.update_error(sub_id, "summarize", str(exc))
            emit("failed", sub_id=sub_id)
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
