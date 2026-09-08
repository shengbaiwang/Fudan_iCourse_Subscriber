from __future__ import annotations

import runpy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class ResummarizeEntrypointTest(unittest.TestCase):
    def test_direct_script_entrypoint_adds_repository_root_to_import_path(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "resummarize.py"

        # Stand in for the imported application modules: this test verifies
        # import-path setup only, without pulling optional AI dependencies.
        src = types.ModuleType("src")
        ai = types.ModuleType("src.ai")
        bucketer = types.ModuleType("src.ai.bucketer")
        summarizer = types.ModuleType("src.ai.summarizer")
        summarizer.Summarizer = type("Summarizer", (), {})
        title = types.ModuleType("src.ai.title")
        title.build_title_material = lambda *args, **kwargs: ""
        title.split_generated_title = lambda value: ("", value)
        data = types.ModuleType("src.data")
        database = types.ModuleType("src.data.database")
        database.Database = type("Database", (), {})
        ai.bucketer = bucketer
        ai.title = title

        modules = {
            "src": src,
            "src.ai": ai,
            "src.ai.bucketer": bucketer,
            "src.ai.summarizer": summarizer,
            "src.ai.title": title,
            "src.data": data,
            "src.data.database": database,
        }
        with patch.dict(sys.modules, modules), patch.object(sys, "path", [str(script.parent)]):
            runpy.run_path(str(script), run_name="resummarize_import_check")
            self.assertEqual(sys.path[0], str(root))

    def test_direct_script_entrypoint_adds_repository_root_for_generate_titles(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "generate_titles.py"

        src = types.ModuleType("src")
        ai = types.ModuleType("src.ai")
        summarizer = types.ModuleType("src.ai.summarizer")
        summarizer.Summarizer = type("Summarizer", (), {})
        title = types.ModuleType("src.ai.title")
        title.build_title_material = lambda *args, **kwargs: ""
        data = types.ModuleType("src.data")
        database = types.ModuleType("src.data.database")
        database.Database = type("Database", (), {})
        ai.summarizer = summarizer
        ai.title = title

        modules = {
            "src": src,
            "src.ai": ai,
            "src.ai.summarizer": summarizer,
            "src.ai.title": title,
            "src.data": data,
            "src.data.database": database,
        }
        with patch.dict(sys.modules, modules), patch.object(sys, "path", [str(script.parent)]):
            runpy.run_path(str(script), run_name="generate_titles_import_check")
            self.assertEqual(sys.path[0], str(root))


if __name__ == "__main__":
    unittest.main()
