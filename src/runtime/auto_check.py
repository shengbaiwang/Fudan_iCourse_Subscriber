"""Scheduled discovery stops after an ended term's notes are complete.

Each pause has a unique token. A later pause of the same course therefore
requires a fresh final discovery, even when no daily run saw the resume.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from src.data.terms import term_end_date, term_sort_key

PAUSE_VARIABLE = "COURSE_AUTO_CHECK_JSON"
PAUSE_SCANS_META = "auto_check_pause_scans"
FINISHED_TERMS_META = "auto_check_finished_terms"


def course_today() -> date:
    """Use the school's UTC+8 date, including on UTC workflow runners."""
    return datetime.now(timezone(timedelta(hours=8))).date()


def load_auto_check_status(conn, pauses: dict[str, str] | None = None, *,
                           today: date | None = None, course_id: str | None = None) -> dict[str, dict]:
    """One status calculation for the scheduled runner and local console."""
    today = today or course_today()
    conn.create_function("term_sort_key", 1, term_sort_key, deterministic=True)
    metadata = dict(conn.execute("SELECT key, value FROM meta WHERE key IN (?, ?)",
                                 (PAUSE_SCANS_META, FINISHED_TERMS_META)).fetchall())
    scans = parse_auto_check_pauses(metadata.get(PAUSE_SCANS_META))
    finished = parse_auto_check_pauses(metadata.get(FINISHED_TERMS_META))
    rows = conn.execute("""
        SELECT c.course_id,
            (SELECT term FROM all_courses ac WHERE ac.course_id=c.course_id
             ORDER BY term_sort_key(term) DESC, term DESC LIMIT 1) AS term,
            COUNT(l.sub_id) AS total_count,
            SUM(TRIM(COALESCE(l.summary, ''), char(9)||char(10)||char(13)||' ') != '') AS summary_count,
            SUM(NULLIF(l.processed_at, '') IS NOT NULL
                AND TRIM(COALESCE(l.summary, ''), char(9)||char(10)||char(13)||' ') != '') AS ready_count,
            MAX(l.date) AS latest_date,
            SUM(l.sub_id IS NOT NULL
                AND (date(l.date) IS NULL OR date(l.date) != l.date)) AS invalid_date_count,
            SUM(l.sub_id IS NOT NULL AND l.processed_at IS NULL
                AND (COALESCE(l.error_count, 0) < 3
                     OR l.error_stage IN ('no_video', 'video', 'video_access'))) AS pending_count,
            SUM(l.processed_at IS NULL AND COALESCE(l.error_count, 0) >= 3
                AND COALESCE(l.error_stage, '') NOT IN ('no_video', 'video', 'video_access')) AS blocked_count
        FROM courses c LEFT JOIN lectures l ON l.course_id=c.course_id
    """ + (" WHERE c.course_id=?" if course_id is not None else "") + " GROUP BY c.course_id",
        (course_id,) if course_id is not None else ()).fetchall()
    result = {}
    for row in rows:
        item = dict(row)
        cid = str(item["course_id"])
        end = term_end_date(item.pop("term"))
        end_text = end.isoformat() if end else None
        ended = bool(end and today >= end)
        ready = item.pop("ready_count")
        latest = item.pop("latest_date")
        invalid_dates = item.pop("invalid_date_count")
        complete = bool(item["total_count"] and ready == item["total_count"])
        dates_passed = bool(latest and latest <= today.isoformat() and not invalid_dates)
        item.update(term_ended=ended, notes_complete=complete,
                    term_end_date=end_text,
                    auto_check_stopped=ended and complete and dates_passed
                    and finished.get(cid) == end_text)
        result[cid] = item
    for cid, token in (pauses or {}).items():
        result.setdefault(cid, {})["pause_scan_pending"] = scans.get(cid) != token
    return result


def parse_auto_check_pauses(raw: str | None) -> dict[str, str]:
    values = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(values, dict) or len(values) > 500:
        raise ValueError("自动检查设置必须是课程 ID 与暂停标记的对应表")
    for course_id, token in values.items():
        if (not course_id or len(course_id) > 100
                or any(char.isspace() for char in course_id) or "," in course_id
                or not isinstance(token, str) or not token or len(token) > 128):
            raise ValueError("自动检查设置中的课程 ID 或暂停标记无效")
    return values
