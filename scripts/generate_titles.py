#!/usr/bin/env python3
"""Backfill AI note titles for lectures that have a summary but no ai_title.

Titles only — summaries are NOT regenerated (no new versions, no big LLM
calls).  Each pending lecture costs one small title call built from its
saved PPT OCR text and transcript (the reliable sources).  Failures skip
the lecture, so a later run can retry it.

Env:
    GENERATE_TITLES_LIMIT  optional cap on how many lectures to process
                           (0 or unset = all pending).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# GitHub Actions invokes this file by path (``python scripts/generate_titles.py``),
# which otherwise puts only ``scripts/`` on ``sys.path``.  Add the repository
# root explicitly so the project packages remain importable.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ai.summarizer import Summarizer
from src.ai.title import build_title_material
from src.data.database import Database


def _pending_lecture_ids(db: Database, limit: int) -> list[str]:
    rows = db.conn.execute(
        """SELECT sub_id FROM lectures
           WHERE TRIM(COALESCE(summary, '')) != ''
             AND TRIM(COALESCE(ai_title, '')) = ''
           ORDER BY COALESCE(date, ''), sub_id"""
    ).fetchall()
    ids = [str(row["sub_id"]) for row in rows]
    return ids[:limit] if limit > 0 else ids


def _course_title(db: Database, lecture: dict) -> str:
    row = db.conn.execute(
        "SELECT title FROM courses WHERE course_id = ?",
        (lecture.get("course_id"),),
    ).fetchone()
    if row and row["title"]:
        return str(row["title"])
    return str(lecture.get("course_id") or "")


def run() -> int:
    try:
        limit = int(os.environ.get("GENERATE_TITLES_LIMIT", "0") or 0)
    except ValueError:
        limit = 0
    db = Database()
    summarizer = Summarizer()
    pending = _pending_lecture_ids(db, limit)
    if not pending:
        print("[Titles] 所有笔记已有标题，无需补齐。")
        return 0
    print(f"[Titles] 待补齐标题：{len(pending)} 节课", flush=True)

    completed = 0
    skipped = 0
    for index, sub_id in enumerate(pending, 1):
        lecture = db.get_lecture(sub_id)
        if lecture is None:
            skipped += 1
            continue
        pages = db.get_done_ppt_pages(sub_id)
        material = build_title_material(
            _course_title(db, lecture),
            str(lecture.get("transcript") or ""),
            pages,
        )
        if not material:
            skipped += 1
            print(
                f"[Titles] {index}/{len(pending)} {sub_id}: 无可用材料，跳过",
                flush=True,
            )
            continue
        try:
            title, model_used = summarizer.generate_title(material)
            if title:
                db.update_ai_title(sub_id, title)
                completed += 1
                print(
                    f"[Titles] {index}/{len(pending)} {sub_id}: "
                    f"{title} ({model_used})",
                    flush=True,
                )
            else:
                skipped += 1
                print(
                    f"[Titles] {index}/{len(pending)} {sub_id}: 生成失败，留下次重试",
                    flush=True,
                )
        except Exception as exc:
            skipped += 1
            print(
                f"[Titles] {index}/{len(pending)} {sub_id} 失败：{exc}",
                file=sys.stderr,
                flush=True,
            )
    print(
        f"[Titles] 完成：成功 {completed}，跳过/失败 {skipped}，共 {len(pending)}",
        flush=True,
    )
    return 0 if completed or not pending else 1


if __name__ == "__main__":
    raise SystemExit(run())
