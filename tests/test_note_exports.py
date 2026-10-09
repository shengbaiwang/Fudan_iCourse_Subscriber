"""Verify file export without mail credentials or outbound SMTP."""
from __future__ import annotations

import base64
import importlib.util
import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import export_course
from src.data.database import Database


HAS_RENDERER = all(importlib.util.find_spec(name) for name in ("markdown", "pygments", "requests", "PIL"))


class NoteExportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.db_path = self.directory / "icourse.db"
        db = Database(str(self.db_path))
        try:
            for cid in ("1", "2"):
                db.upsert_course(cid, "同名课程", "教师")
                for sid in (f"{cid}1", f"{cid}2"):
                    db.insert_lecture(sid, cid, f"第 {sid} 讲", "2026-10-09")
                    db.update_summary(sid, f"## 知识点\n\n**重点 {sid}**\n\n> 引用", "test/model")
                    db.mark_processed(sid)
        finally:
            db.conn.close()

    def export(self, *arguments):
        argv = ["export_course.py", "--db", str(self.db_path), "--output-dir", str(self.directory / "exports"), *arguments]
        # Even stale mail credentials must never cause an SMTP connection.
        with patch.object(sys, "argv", argv), patch.dict("os.environ", {
            "SMTP_EMAIL": "old@example.test", "SMTP_PASSWORD": "unused", "RECEIVER_EMAIL": "reader@example.test",
        }), patch("smtplib.SMTP_SSL", side_effect=AssertionError("Unexpected mail send")), redirect_stdout(io.StringIO()):
            export_course.main()
        return sorted((self.directory / "exports").iterdir())

    def test_markdown_exports_same_named_courses_as_separate_files(self):
        files = self.export("--course-id", "1,2,1", "--md")
        self.assertEqual(len(files), 2)
        self.assertIn("**重点 11**", files[0].read_text())
        self.assertIn("**重点 22**", files[1].read_text())

    def test_selected_lectures_only(self):
        files = self.export("--course-id", "1", "--sub-ids", "12", "--md")
        content = files[0].read_text()
        self.assertIn("重点 12", content)
        self.assertNotIn("重点 11", content)

    def test_no_matching_notes_reports_failure(self):
        with self.assertRaises(SystemExit) as error:
            self.export("--course-id", "1", "--sub-ids", "missing", "--md")
        self.assertEqual(error.exception.code, 1)

    @unittest.skipUnless(HAS_RENDERER, "HTML export requires the pipeline rendering dependencies")
    def test_html_export_retains_color_and_markdown(self):
        files = self.export("--course-id", "1")
        html = files[0].read_text()
        self.assertIn("<strong>重点 11</strong>", html)
        self.assertIn("<blockquote>", html)
        self.assertIn("strong { color:", html)
        self.assertNotIn("cid:", html)

    @unittest.skipUnless(HAS_RENDERER, "PDF export requires the pipeline rendering dependencies")
    def test_pdf_export_writes_the_selected_course_file(self):
        # Exercise the CLI/file contract without requiring system Pango fonts.
        class HTML:
            def __init__(self, *, string):
                self.html = string

            def write_pdf(self, destination):
                if "重点 12" not in self.html or "重点 11" in self.html:
                    raise AssertionError("PDF included the wrong notes")
                Path(destination).write_bytes(b"%PDF-test")

        with patch.dict(sys.modules, {"weasyprint": types.SimpleNamespace(HTML=HTML)}):
            files = self.export("--course-id", "1", "--sub-ids", "12", "--pdf")
        self.assertEqual(files[0].suffix, ".pdf")
        self.assertEqual(files[0].read_bytes(), b"%PDF-test")

    @unittest.skipUnless(HAS_RENDERER, "Formula export requires the pipeline rendering dependencies")
    def test_formula_images_are_embedded_in_downloaded_html(self):
        from src import note_rendering

        with patch.object(note_rendering, "_prefetch_latex_images"), patch.object(
            note_rendering, "_fetch_latex_image", return_value=(20, 15, b"formula-png"),
        ):
            html = note_rendering.markdown_to_html("公式 $P_n$ 和 $$x^2$$")
        source = "data:image/png;base64," + base64.b64encode(b"formula-png").decode("ascii")
        self.assertEqual(html.count(source), 2)
        self.assertIn('alt="P_n"', html)
        self.assertIn('alt="x^2"', html)
        self.assertNotIn("cid:", html)


@unittest.skipUnless(importlib.util.find_spec("sherpa_onnx") and importlib.util.find_spec("openai"), "Requires pipeline dependencies")
class PipelineWithoutMailTest(unittest.TestCase):
    def test_processed_notes_are_saved_without_email(self):
        import main

        with tempfile.TemporaryDirectory() as directory:
            db = Database(str(Path(directory) / "icourse.db"))
            try:
                db.upsert_course("1", "课程", "教师")
                db.insert_lecture("10", "1", "第一讲", "2026-10-09")

                def save_summary(*args, **kwargs):
                    db.update_summary("10", "**已保存的重点**", "test/model")
                    db.mark_processed("10")
                    return "**已保存的重点**"

                with patch.object(main.config, "COURSE_IDS", ["1"]), patch.object(main, "Database", return_value=db), \
                     patch.object(main, "Transcriber"), patch.object(main, "Summarizer"), \
                     patch.object(main, "login_with_retry"), patch.object(main, "ICourseClient"), \
                     patch.object(main, "Scheduler") as scheduler, patch.object(main, "LectureRunner") as runner, \
                     patch.object(main, "_crawl_semester_catalog"), patch.object(main, "_enumerate_lectures", return_value=[("1", "课程", {"sub_id": "10"})]), \
                     patch("smtplib.SMTP_SSL", side_effect=AssertionError("Unexpected mail send")), redirect_stdout(io.StringIO()):
                    runner.return_value.run.side_effect = save_summary
                    main.run()
                row = db.get_lecture("10")
                self.assertEqual(row["summary"], "**已保存的重点**")
                self.assertTrue(row["processed_at"])
                self.assertIsNone(row["emailed_at"])
                scheduler.return_value.shutdown.assert_called_once()
            finally:
                db.conn.close()


if __name__ == "__main__":
    unittest.main()
