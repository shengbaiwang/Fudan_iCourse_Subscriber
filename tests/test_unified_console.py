"""Regression checks for the shared console and the two deployment entrypoints."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

from local_web.database import DatabaseManager
from scripts.build_frontend import ROOT, build
from src.data.schema import SCHEMA_SQL
from src.runtime.config import DEFAULT_MODEL_PROVIDERS
from src.runtime.model_config import validate_model_config
import json


class SharedConsoleTest(unittest.TestCase):
    def test_pages_packages_exact_shared_ui_and_relative_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'pages'
            build(output)
            source = ROOT / 'local_web/static'
            for name in ('index.html', 'styles.css', 'app.js', 'bootstrap.js'):
                self.assertEqual((output / name).read_bytes(), (source / name).read_bytes())
            self.assertIn('"pages"', (output / 'runtime-config.js').read_text())
            self.assertIn('"local"', (source / 'runtime-config.js').read_text())
            self.assertNotIn('src="/', (output / 'index.html').read_text())
            self.assertNotIn('href="/', (output / 'index.html').read_text())
            self.assertTrue((output / 'browser/transport.js').is_file())

    def test_browser_defaults_match_runtime_configuration(self):
        defaults = json.loads((ROOT / 'local_web/static/browser/default-providers.json').read_text())
        self.assertEqual(defaults, validate_model_config(DEFAULT_MODEL_PROVIDERS))

    def test_domain_search_returns_matching_snippets_and_paginates(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = DatabaseManager(Path(directory))
            try:
                with closing(sqlite3.connect(manager.db_path)) as db:
                    db.executescript(SCHEMA_SQL)
                    db.execute("INSERT INTO courses VALUES ('1', '课程', '教师')")
                    db.execute("INSERT INTO courses VALUES ('2', '另一课程', '教师')")
                    db.executemany("INSERT INTO lectures (sub_id, course_id, summary, transcript) VALUES (?, ?, ?, ?)", [
                        ('a', '1', '摘要内容', '专属转录关键词'),
                        ('b', '1', '另一摘要关键词', '无匹配内容'),
                        ('c', '2', '关键词', ''),
                    ])
                    db.executemany("INSERT INTO ppt_pages (sub_id, page_num, created_sec, text, ocr_status) VALUES (?, ?, 0, ?, ?)", [
                        ('a', 1, '专属 OCR 关键词', 'done'),
                        ('b', 1, '未完成 OCR 关键词', 'pending'),
                    ])
                    db.commit()
                rows = manager.search('关键词', domains=['transcript'])['results']
                self.assertEqual([row['sub_id'] for row in rows], ['a'])
                self.assertIn('专属转录', rows[0]['snippet'])
                rows = manager.search('关键词', domains=['ocr'])['results']
                self.assertEqual([row['sub_id'] for row in rows], ['a'])
                self.assertIn('专属 OCR', rows[0]['snippet'])
                self.assertEqual(manager.search('关键词', domains=[])['results'], [])
                self.assertEqual(manager.search('关键词', course_id='missing')['results'], [])
                first = manager.search('关键词', page_size=1, course_id='1')['results']
                second = manager.search('关键词', page_size=1, page=2, course_id='1')['results']
                self.assertNotEqual(first[0]['sub_id'], second[0]['sub_id'])
                self.assertEqual(manager.search('关键词', page_size=1, page=3, course_id='1')['results'], [])
            finally:
                manager.close()


if __name__ == '__main__':
    unittest.main()
