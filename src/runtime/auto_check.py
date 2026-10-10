"""Repository-wide pauses for scheduled course discovery.

Each pause has a unique token. A later pause of the same course therefore
requires a fresh final discovery, even when no daily run saw the resume.
"""
from __future__ import annotations

import json

PAUSE_VARIABLE = "COURSE_AUTO_CHECK_JSON"
PAUSE_SCANS_META = "auto_check_pause_scans"


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
