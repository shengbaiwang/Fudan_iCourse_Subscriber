"""Canonical department names for both new catalogs and historical libraries."""

import re

_SPACE = re.compile(r"\s+", re.ASCII)
_CODE = re.compile(r"^(?:[0-9０-９]{3} )+(?=\S)")


def normalize_department(value: str | None) -> str:
    """Drop a separated three-digit catalog code; keep distinct names distinct.

    Match the browser's departments.js rules. Do not infer aliases from codes
    or similar names: a department rename is not necessarily the same unit.
    """
    name = str(value or "").replace("\u00a0", " ").replace("\u3000", " ")
    name = _SPACE.sub(" ", name).strip()
    return _CODE.sub("", name)


def normalize_catalog_departments(conn, schema: str = "main") -> None:
    """Idempotently repair old catalogs without changing IDs or timestamps."""
    if not conn.execute(f"PRAGMA {schema}.table_info(all_courses)").fetchall():
        return
    updates = []
    for (raw,) in conn.execute(f"SELECT DISTINCT dept FROM {schema}.all_courses"):
        if raw is not None and (name := normalize_department(raw)) != raw:
            updates.append((name, raw))
    conn.executemany(f"UPDATE {schema}.all_courses SET dept = ? WHERE dept = ?", updates)
