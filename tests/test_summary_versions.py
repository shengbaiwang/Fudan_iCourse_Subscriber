from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.merge_db import merge
from src.data.database import Database
from src.data.schema import SCHEMA_SQL


class SummaryVersionStorageTest(unittest.TestCase):
    def test_update_summary_appends_every_rerun_version(self):
        """A rerun — even with the same model — adds a row, never overwrites."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icourse.db"
            database = Database(str(path))
            try:
                database.upsert_course("course", "课程", "教师")
                self.assertTrue(database.insert_lecture("lecture", "course", "第一讲", "2026-08-04"))
                database.update_summary("lecture", "模型 A 的第一次输出", "provider/model-a")
                database.update_summary("lecture", "模型 B 的输出", "provider/model-b")
                database.update_summary("lecture", "模型 A 的新版输出", "provider/model-a")

                lecture = database.get_lecture("lecture")
                self.assertEqual(lecture["summary"], "模型 A 的新版输出")
                self.assertEqual(lecture["summary_model"], "provider/model-a")
                versions = database.conn.execute(
                    "SELECT model, summary FROM summary_versions "
                    "WHERE sub_id = ? ORDER BY generated_at",
                    ("lecture",),
                ).fetchall()
                self.assertEqual(
                    [(row["model"], row["summary"]) for row in versions],
                    [
                        ("provider/model-a", "模型 A 的第一次输出"),
                        ("provider/model-b", "模型 B 的输出"),
                        ("provider/model-a", "模型 A 的新版输出"),
                    ],
                )
            finally:
                database.conn.close()

    def test_opening_a_legacy_database_backfills_the_active_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            with sqlite3.connect(path) as legacy:
                legacy.executescript(
                    """
                    CREATE TABLE courses (course_id TEXT PRIMARY KEY, title TEXT, teacher TEXT);
                    CREATE TABLE lectures (
                        sub_id TEXT PRIMARY KEY, course_id TEXT NOT NULL,
                        sub_title TEXT, date TEXT, transcript TEXT, summary TEXT,
                        processed_at TEXT, emailed_at TEXT
                    );
                    INSERT INTO courses VALUES ('course', '课程', '教师');
                    INSERT INTO lectures VALUES
                        ('lecture', 'course', '第一讲', '', '', '历史摘要', '2026-08-01T10:00:00', NULL);
                    """
                )
            database = Database(str(path))
            try:
                row = database.conn.execute(
                    "SELECT model, summary FROM summary_versions WHERE sub_id = 'lecture'"
                ).fetchone()
                self.assertEqual((row["model"], row["summary"]), ("unknown", "历史摘要"))
            finally:
                database.conn.close()


class SummaryVersionMigrationTest(unittest.TestCase):
    def test_legacy_per_model_table_is_rebuilt_preserving_rows(self):
        """An old PRIMARY KEY (sub_id, model) table is migrated in place."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icourse.db"
            with sqlite3.connect(path) as legacy:
                legacy.executescript(
                    """
                    CREATE TABLE courses (course_id TEXT PRIMARY KEY, title TEXT, teacher TEXT);
                    CREATE TABLE lectures (
                        sub_id TEXT PRIMARY KEY, course_id TEXT NOT NULL,
                        sub_title TEXT, date TEXT, transcript TEXT, summary TEXT,
                        processed_at TEXT, emailed_at TEXT, error_msg TEXT,
                        error_count INTEGER DEFAULT 0, error_stage TEXT, summary_model TEXT
                    );
                    CREATE TABLE summary_versions (
                        sub_id TEXT NOT NULL,
                        model TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        generated_at TEXT NOT NULL,
                        PRIMARY KEY (sub_id, model)
                    );
                    INSERT INTO courses VALUES ('course', '课程', '教师');
                    INSERT INTO lectures VALUES
                        ('lecture', 'course', '第一讲', '', '', '现行摘要', '2026-08-01T10:00:00',
                         NULL, NULL, 0, NULL, 'provider/model-a');
                    INSERT INTO summary_versions VALUES
                        ('lecture', 'provider/model-a', '旧版输出', '2026-08-01T09:00:00');
                    """
                )
            database = Database(str(path))
            try:
                # The legacy row survives the rebuild; the guarded backfill
                # adds nothing because the lecture already has a version.
                rows = database.conn.execute(
                    "SELECT model, summary FROM summary_versions ORDER BY generated_at"
                ).fetchall()
                self.assertEqual(
                    [(row["model"], row["summary"]) for row in rows],
                    [("provider/model-a", "旧版输出")],
                )
                # A same-model rerun now appends instead of overwriting.
                database.update_summary("lecture", "新版输出", "provider/model-a")
                rows = database.conn.execute(
                    "SELECT model, summary FROM summary_versions ORDER BY generated_at"
                ).fetchall()
                self.assertEqual(
                    [(row["model"], row["summary"]) for row in rows],
                    [("provider/model-a", "旧版输出"), ("provider/model-a", "新版输出")],
                )
            finally:
                database.conn.close()


class AiTitleStorageTest(unittest.TestCase):
    def test_update_ai_title_writes_and_never_wipes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icourse.db"
            database = Database(str(path))
            try:
                database.upsert_course("course", "课程", "教师")
                database.insert_lecture("lecture", "course", "第一讲", "2026-08-04")
                database.update_ai_title("lecture", "梯度下降")
                self.assertEqual(
                    database.get_lecture("lecture")["ai_title"], "梯度下降"
                )
                # A failed generation returns "" and must keep the old title.
                database.update_ai_title("lecture", "  ")
                self.assertEqual(
                    database.get_lecture("lecture")["ai_title"], "梯度下降"
                )
                database.update_ai_title("lecture", "随机梯度下降")
                self.assertEqual(
                    database.get_lecture("lecture")["ai_title"], "随机梯度下降"
                )
            finally:
                database.conn.close()


class SummaryVersionMergeTest(unittest.TestCase):
    def _make_pair(self, directory):
        local_path = Path(directory) / "local.db"
        remote_path = Path(directory) / "remote.db"
        for path in (local_path, remote_path):
            with sqlite3.connect(path) as db:
                db.executescript(SCHEMA_SQL)
                db.execute("INSERT INTO courses VALUES ('course', '课程', '教师')")
                db.execute(
                    "INSERT INTO lectures (sub_id, course_id, summary, summary_model) "
                    "VALUES ('lecture', 'course', '摘要', 'model')"
                )
        return local_path, remote_path

    def test_merge_keeps_versions_written_by_different_models(self):
        with tempfile.TemporaryDirectory() as directory:
            local_path, remote_path = self._make_pair(directory)
            with sqlite3.connect(local_path) as db:
                db.execute(
                    "INSERT INTO summary_versions VALUES ('lecture', 'provider/model-a', 'A', '2026-08-04T10:00:00')"
                )
            with sqlite3.connect(remote_path) as db:
                db.execute(
                    "INSERT INTO summary_versions VALUES ('lecture', 'provider/model-b', 'B', '2026-08-04T09:00:00')"
                )

            merge(str(local_path), str(remote_path))

            with sqlite3.connect(remote_path) as db:
                rows = db.execute(
                    "SELECT model, summary FROM summary_versions WHERE sub_id = 'lecture' ORDER BY model"
                ).fetchall()
            self.assertEqual(rows, [("provider/model-a", "A"), ("provider/model-b", "B")])

    def test_merge_keeps_same_model_versions_from_both_sides(self):
        """Concurrent same-model reruns union instead of overwriting."""
        with tempfile.TemporaryDirectory() as directory:
            local_path, remote_path = self._make_pair(directory)
            with sqlite3.connect(local_path) as db:
                db.execute(
                    "INSERT INTO summary_versions VALUES ('lecture', 'provider/model-a', '本地重跑', '2026-08-03T10:00:00')"
                )
            with sqlite3.connect(remote_path) as db:
                db.execute(
                    "INSERT INTO summary_versions VALUES ('lecture', 'provider/model-a', '远端重跑', '2026-08-04T10:00:00')"
                )

            merge(str(local_path), str(remote_path))

            with sqlite3.connect(remote_path) as db:
                rows = db.execute(
                    "SELECT summary FROM summary_versions WHERE sub_id = 'lecture' ORDER BY generated_at"
                ).fetchall()
            self.assertEqual([row[0] for row in rows], ["本地重跑", "远端重跑"])


if __name__ == "__main__":
    unittest.main()
