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
        data = types.ModuleType("src.data")
        database = types.ModuleType("src.data.database")
        database.Database = type("Database", (), {})
        ai.bucketer = bucketer

        modules = {
            "src": src,
            "src.ai": ai,
            "src.ai.bucketer": bucketer,
            "src.ai.summarizer": summarizer,
            "src.data": data,
            "src.data.database": database,
        }
        with patch.dict(sys.modules, modules), patch.object(sys, "path", [str(script.parent)]):
            runpy.run_path(str(script), run_name="resummarize_import_check")
            self.assertEqual(sys.path[0], str(root))


if __name__ == "__main__":
    unittest.main()
