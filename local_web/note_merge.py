"""Copy only one job result, preserving unrelated remote/local notes."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from scripts.merge_db import merge
from src.data.database import Database


def merge_note(source: Path, target: Path, sub_id: str, scratch: Path) -> None:
    delta = scratch / "note-delta.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(delta) + suffix).unlink(missing_ok=True)
    initial = Database(str(delta))
    initial.conn.close()
    with closing(sqlite3.connect(str(delta))) as conn:
        conn.execute("ATTACH DATABASE ? AS source", (str(source),))
        with conn:
            for table, where, params in (
                ("courses", "course_id IN (SELECT course_id FROM source.lectures WHERE sub_id=?)", (sub_id,)),
                ("lectures", "sub_id=?", (sub_id,)),
                ("ppt_pages", "sub_id=?", (sub_id,)),
                ("summary_versions", "sub_id=?", (sub_id,)),
                ("meta", "key IN (?,?,?)", tuple(f"local:{sub_id}:{stage}" for stage in ("audio", "segments", "ocr"))),
            ):
                available = {row[1] for row in conn.execute(f"PRAGMA source.table_info({table})")}
                columns = [row[1] for row in conn.execute(f"PRAGMA main.table_info({table})") if row[1] in available]
                names = ",".join(columns)
                conn.execute(f"INSERT INTO main.{table} ({names}) SELECT {names} FROM source.{table} WHERE {where}", params)
    if not target.is_file():
        initial = Database(str(target))
        initial.conn.close()
    merge_local_notes(delta, target)


def merge_local_notes(source: Path, target: Path) -> None:
    merge(str(source), str(target))
    with closing(sqlite3.connect(str(target))) as conn:
        conn.execute("ATTACH DATABASE ? AS source", (str(source),))
        with conn:
            # Preserve local verification checkpoints for retries and only
            # advance incomplete OCR rows to a verified terminal state.
            conn.execute("""INSERT OR REPLACE INTO main.meta (key,value)
                SELECT key,value FROM source.meta WHERE key GLOB 'local:*'""")
            conn.execute("""UPDATE main.ppt_pages SET text=s.text, ocr_status=s.ocr_status,
                ocr_at=s.ocr_at, dhash=s.dhash FROM source.ppt_pages s
                WHERE main.ppt_pages.sub_id=s.sub_id
                  AND main.ppt_pages.page_num=s.page_num
                  AND main.ppt_pages.ocr_status IN ('pending','failed')
                  AND s.ocr_status IN ('done','invalid','dedup','dedup_dropped')""")
