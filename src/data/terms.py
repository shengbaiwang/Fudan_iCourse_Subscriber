"""Semester display names, independent of API codes and stored catalog keys."""

import re

_TERM = re.compile(
    r"([0-9]{4})\s*[-–—]\s*([0-9]{4})\s*(?:学年\s*)?"
    r"(?:[-–—]\s*)?(1|2|第[一二12]学期|暑期(?:学期)?)"
)
_LABELS = {
    "1": "第一学期", "第1学期": "第一学期", "第一学期": "第一学期",
    "2": "第二学期", "第2学期": "第二学期", "第二学期": "第二学期",
    "暑期": "暑期", "暑期学期": "暑期",
}
_SORT_TERM = re.compile(r"([0-9]{4})–([0-9]{4}) (第一学期|第二学期|暑期)")
_RANK = {"第一学期": 1, "第二学期": 2, "暑期": 3}


def normalize_term(value: str | None) -> str:
    """Format recognized names for any academic year; preserve unknown labels.

    Keep these rules in sync with local_web/static/terms.js. Catalog storage
    retains its raw keys so replacement crawls and shard merges still match.
    """
    name = str(value or "").strip()
    match = _TERM.fullmatch(name)
    if not match or int(match[2]) != int(match[1]) + 1:
        return name
    return f"{match[1]}–{match[2]} {_LABELS[match[3]]}"


def term_sort_key(value: str | None) -> str:
    """Chronological ordering within a year: first, second, then summer."""
    name = normalize_term(value)
    match = _SORT_TERM.fullmatch(name)
    if not match:
        return name
    rank = _RANK[match[3]]
    return f"{match[1]}-{match[2]}-{rank}"
