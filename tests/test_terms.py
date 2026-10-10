"""The same naming contract covers old libraries and future catalog imports."""

import json
from pathlib import Path
import sqlite3
import subprocess

from local_web.database import DatabaseManager
from src.data.database import Database
from src.data.schema import SCHEMA_SQL
from src.data.terms import normalize_term, term_sort_key


CASES = [
    ("2026-20271", "2026–2027 第一学期"),
    ("2025-2026暑期", "2025–2026 暑期"),
    ("2025-20262", "2025–2026 第二学期"),
    ("2025-20261", "2025–2026 第一学期"),
    ("2024-20252", "2024–2025 第二学期"),
    ("2024-20251", "2024–2025 第一学期"),
    ("2023-2024-2", "2023–2024 第二学期"),
    ("2030-20311", "2030–2031 第一学期"),
    ("2030–2031 第二学期", "2030–2031 第二学期"),
    (" 2030 — 2031 第1学期 ", "2030–2031 第一学期"),
    ("2030-2031学年暑期学期", "2030–2031 暑期"),
    ("2030-2031-2", "2030–2031 第二学期"),
    (None, ""), ("", ""), (" \t　", ""),
    ("2026-秋", "2026-秋"), ("25", "25"),
    ("2030-20321", "2030-20321"),
    ("2030-20313", "2030-20313"),
    ("2030-20311其他", "2030-20311其他"),
]


def test_python_and_browser_share_names_sort_keys_and_idempotence():
    source = Path(__file__).resolve().parents[1] / "local_web/static/terms.js"
    output = subprocess.check_output([
        "node", "-e",
        "global.window = {}; require(process.argv[1]); "
        "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(v => "
        "[window.ICS.normalizeTerm(v), window.ICS.termSortKey(v), "
        "window.ICS.normalizeTerm(window.ICS.normalizeTerm(v))])));",
        str(source), json.dumps([raw for raw, _ in CASES]),
    ], text=True)
    assert json.loads(output) == [[expected, term_sort_key(raw), expected] for raw, expected in CASES]
    for raw, expected in CASES:
        assert normalize_term(raw) == expected
        assert normalize_term(expected) == expected


def test_legacy_terms_group_filter_and_sort_without_rewriting_catalog(tmp_path):
    manager = DatabaseManager(tmp_path)
    try:
        with sqlite3.connect(manager.db_path) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.executemany("INSERT INTO courses VALUES (?, '课程', '教师')", [(str(i),) for i in range(7)])
            conn.executemany("INSERT INTO all_courses VALUES (?, ?, '课程', '教师', '院系', 'original')", [
                (str(i), raw) for i, (raw, _) in enumerate(CASES[:7])
            ] + [("alias", "2025-2026-1"), ("0", "2026-2026暑期")])
        expected = [name for _, name in CASES[:7]]
        # Unknown names are preserved; known ones retain chronological order.
        assert manager.subscription_terms() == expected[:1] + ["2026-2026暑期"] + expected[1:]
        for term in ("2025–2026 第一学期", "2025-20261", "2025-2026-1"):
            result = manager.subscription_catalog(term=term, limit=1)
            assert result["total"] == 2
            assert result["courses"][0]["term"] == "2025–2026 第一学期"
            second = manager.subscription_catalog(term=term, limit=1, page=2)
            assert second["courses"][0]["course_id"] != result["courses"][0]["course_id"]
        assert manager.subscription_courses(["0"])[0]["term"] == expected[0]
        assert {r["course_id"]: r["term"] for r in manager.courses()}["0"] == expected[0]
        with sqlite3.connect(manager.db_path) as conn:
            assert conn.execute("SELECT term FROM all_courses WHERE course_id = 'alias'").fetchone()[0] == "2025-2026-1"
    finally:
        manager.close()


def test_future_imports_and_catalog_replacements_keep_format(tmp_path):
    manager = DatabaseManager(tmp_path)
    writer = Database(str(manager.db_path))
    try:
        writer.upsert_all_courses_for_term("2035-20361", [{"course_id": "new"}, {"course_id": "dropped"}])
        assert manager.subscription_terms() == ["2035–2036 第一学期"]
        writer.upsert_all_courses_for_term("2035-20361", [{"course_id": "new"}])
        writer.upsert_all_courses_for_term("2035-2036-2", [{"course_id": "new"}])
        writer.upsert_all_courses_for_term("2035-2036暑期", [{"course_id": "summer"}])
        assert manager.subscription_terms() == ["2035–2036 暑期", "2035–2036 第二学期", "2035–2036 第一学期"]
        assert manager.subscription_catalog()["total"] == 3
        assert manager.subscription_courses(["new"])[0]["term"] == "2035–2036 第二学期"
        assert manager.subscription_catalog(term="2035–2036 第一学期")["total"] == 1
    finally:
        writer.conn.close()
        manager.close()
