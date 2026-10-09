"""Stable, explicit course and lecture ordering shared by both runtimes."""
from __future__ import annotations

LECTURE_ORDERS = {"oldest", "newest", "api"}


def ordered_lectures(lectures: list[dict], order: str) -> list[dict]:
    if order not in LECTURE_ORDERS:
        raise ValueError("课次顺序必须为 oldest、newest 或 api")
    if order == "api":
        return list(lectures)
    # Preserve the platform's order for lectures on the same date.
    return sorted(lectures, key=lambda row: str(row.get("date") or ""),
                  reverse=order == "newest")


def ordered_ids(values: list[str]) -> list[str]:
    result = list(dict.fromkeys(str(value).strip() for value in values))
    if any(not value or len(value) > 100 or "," in value or
           any(char.isspace() for char in value) for value in result):
        raise ValueError("课程或课次 ID 格式不正确")
    return result
