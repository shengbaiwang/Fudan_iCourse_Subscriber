"""Single source of truth for the SQLite schema.

This module is imported by every Python component that creates or migrates
the database (Database, sharder, merge_db) so the column list lives in
exactly one place.

frontend/js/schema.js is a **manual mirror** of these constants.  When you
change SCHEMA_SQL, LECTURES_MIGRATION_COLUMNS, or PPT_PAGES_MIGRATION_COLUMNS
here, update that file too — there is no automated sync.  Both run in
different processes (Python on the CI runner, JS in the browser) and have
to agree on what tables and columns exist.
"""

from __future__ import annotations


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS courses (
    course_id TEXT PRIMARY KEY,
    title TEXT,
    teacher TEXT
);
CREATE TABLE IF NOT EXISTS lectures (
    sub_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    sub_title TEXT, date TEXT,
    transcript TEXT, summary TEXT,
    processed_at TEXT, emailed_at TEXT,
    error_msg TEXT, error_count INTEGER DEFAULT 0,
    error_stage TEXT, summary_model TEXT,
    ai_title TEXT,
    FOREIGN KEY (course_id) REFERENCES courses(course_id)
);
-- A lecture's current summary remains on ``lectures`` for compatibility
-- with email/export.  This table keeps EVERY rerun as its own row — the
-- primary key includes ``generated_at``, so re-running with the same model
-- appends a new version instead of overwriting the previous one.
CREATE TABLE IF NOT EXISTS summary_versions (
    sub_id TEXT NOT NULL,
    model TEXT NOT NULL,
    summary TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (sub_id, model, generated_at),
    FOREIGN KEY (sub_id) REFERENCES lectures(sub_id)
);
CREATE INDEX IF NOT EXISTS idx_summary_versions_sub_generated
    ON summary_versions(sub_id, generated_at DESC);
CREATE TABLE IF NOT EXISTS ppt_pages (
    sub_id TEXT NOT NULL,
    page_num INTEGER NOT NULL,
    created_sec INTEGER NOT NULL,
    pptimgurl TEXT,
    text TEXT,
    ocr_status TEXT NOT NULL DEFAULT 'pending',
    ocr_at TEXT,
    dhash TEXT,
    PRIMARY KEY (sub_id, page_num),
    FOREIGN KEY (sub_id) REFERENCES lectures(sub_id)
);
CREATE INDEX IF NOT EXISTS idx_ppt_pages_sub_status
    ON ppt_pages(sub_id, ocr_status);
-- ``all_courses`` is the catalog of every course offered by the school in
-- a given term, regardless of whether the user has subscribed to it.  Used
-- by the frontend's subscription editor to render a searchable picker;
-- separate from ``courses`` (which only holds subscribed courses with
-- locally-cached lectures).
CREATE TABLE IF NOT EXISTS all_courses (
    course_id TEXT NOT NULL,
    term TEXT NOT NULL,
    title TEXT,
    teacher TEXT,
    dept TEXT,
    last_seen_at TEXT,
    PRIMARY KEY (course_id, term)
);
CREATE INDEX IF NOT EXISTS idx_all_courses_term
    ON all_courses(term);
-- ``meta`` holds key-value configuration that the frontend needs without
-- loading the full course-data shards (e.g. currently-subscribed course IDs).
-- Populated by the CI runner from secrets / runtime state.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added to ``lectures`` after the v1 schema shipped.  Existing DBs
# get them via ALTER TABLE in Database._init_tables / merge_db._ensure_schema.
LECTURES_MIGRATION_COLUMNS: list[tuple[str, str]] = [
    ("error_msg", "TEXT"),
    ("error_count", "INTEGER DEFAULT 0"),
    ("error_stage", "TEXT"),
    ("summary_model", "TEXT"),
    # AI-generated note title (src/ai/title.py + Summarizer.generate_title).
    # NULL on older databases — display falls back to local derivation.
    ("ai_title", "TEXT"),
]

# Columns added to ``ppt_pages`` after its initial shape shipped.
PPT_PAGES_MIGRATION_COLUMNS: list[tuple[str, str]] = [
    ("dhash", "TEXT"),
]


# ── summary_versions: per-rerun primary key migration ──────────────────────
# The table originally shipped with PRIMARY KEY (sub_id, model), which made a
# same-model rerun silently overwrite that model's previous version.  The
# current key is (sub_id, model, generated_at) so every rerun is preserved.
# All three components that open or merge databases (Database, merge_db,
# sharder.reassemble) share the two helpers below — do not fork the logic.

def _summary_versions_pk(conn, schema: str) -> list[str]:
    """Primary-key column names of ``summary_versions`` in ``schema`` ([] if absent)."""
    rows = conn.execute(f"PRAGMA {schema}.table_info(summary_versions)").fetchall()
    return [r[1] for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])]


def migrate_summary_versions_table(conn, schema: str = "main") -> None:
    """Rebuild ``summary_versions`` with the per-rerun primary key, in place.

    Idempotent: no-op when the table does not exist yet (SCHEMA_SQL creates
    the new shape on fresh databases) or is already keyed by
    (sub_id, model, generated_at).  Existing rows are preserved verbatim.
    """
    if _summary_versions_pk(conn, schema) in ([], ["sub_id", "model", "generated_at"]):
        return
    conn.execute(
        f"""CREATE TABLE {schema}.summary_versions_new (
                sub_id TEXT NOT NULL,
                model TEXT NOT NULL,
                summary TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                PRIMARY KEY (sub_id, model, generated_at),
                FOREIGN KEY (sub_id) REFERENCES lectures(sub_id)
            )"""
    )
    conn.execute(
        f"INSERT OR IGNORE INTO {schema}.summary_versions_new "
        f"SELECT * FROM {schema}.summary_versions"
    )
    conn.execute(f"DROP TABLE {schema}.summary_versions")
    conn.execute(
        f"ALTER TABLE {schema}.summary_versions_new RENAME TO summary_versions"
    )
    conn.execute(
        f"""CREATE INDEX IF NOT EXISTS {schema}.idx_summary_versions_sub_generated
            ON summary_versions(sub_id, generated_at DESC)"""
    )


def backfill_summary_versions(conn, schema: str = "main") -> None:
    """Seed a first version from the active summary for versionless lectures.

    Only lectures with NO version rows at all are seeded (legacy databases
    from before the feature).  The NOT EXISTS guard is essential under
    per-rerun keys: without it every call would fabricate an extra copy of
    the active summary stamped with the old ``processed_at``.
    """
    conn.execute(
        f"""INSERT OR IGNORE INTO {schema}.summary_versions
               (sub_id, model, summary, generated_at)
           SELECT lec.sub_id,
                  COALESCE(NULLIF(lec.summary_model, ''), 'unknown'),
                  lec.summary,
                  COALESCE(NULLIF(lec.processed_at, ''), datetime('now'))
           FROM {schema}.lectures lec
           WHERE TRIM(COALESCE(lec.summary, '')) != ''
             AND NOT EXISTS (
                   SELECT 1 FROM {schema}.summary_versions sv
                   WHERE sv.sub_id = lec.sub_id
             )"""
    )
