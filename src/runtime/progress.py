"""Per-note notifications; cloud checkpoints publish encrypted data only."""
from __future__ import annotations

import json
import os

PREFIX = "ICOURSE_EVENT "


def emit(kind: str, **values) -> None:
    if os.environ.get("LOCAL_RUN_EVENTS") == "1":
        print(PREFIX + json.dumps({"kind": kind, **values}, ensure_ascii=False), flush=True)


def note_saved(db, sub_id: str) -> None:
    emit("note", sub_id=sub_id)
    if os.environ.get("PUBLISH_NOTE_PROGRESS") == "1":
        try:
            from scripts.publish_progress import publish_database
            publish_database(db.db_path)
        except Exception as exc:
            # A checkpoint outage must not lose work or abort other lectures.
            # The workflow's final deployment remains the retry path.
            print(f"[Progress] 发布暂未成功（{type(exc).__name__}），最终部署将重试。", flush=True)
