import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app_paths


class FrontendDirectoryTest(unittest.TestCase):
    def test_default_is_next_to_the_project_folder_whatever_the_current_directory(self):
        expected = Path(app_paths.__file__).resolve().parent.parent / "3d-scanner-front-end"
        old_cwd = os.getcwd()
        try:
            for cwd in (tempfile.gettempdir(), "/"):
                os.chdir(cwd)
                self.assertEqual(app_paths.frontend_directory({}), expected)
        finally:
            os.chdir(old_cwd)
        self.assertTrue(expected.is_absolute())

    def test_environment_variable_overrides_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(app_paths.frontend_directory({"FRONTEND_DIR": tmp}), Path(tmp).resolve())

    def test_blank_variable_is_ignored(self):
        self.assertEqual(app_paths.frontend_directory({"FRONTEND_DIR": "   "}), app_paths.DEFAULT_FRONTEND_DIR)

    def test_main_prod_no_longer_uses_a_relative_front_end_path(self):
        source = (Path(app_paths.__file__).parent / "main_prod.py").read_text(encoding="utf-8")
        self.assertNotIn('directory="../3d-scanner-front-end"', source)
        self.assertIn("frontend_directory()", source)


if __name__ == "__main__":
    unittest.main()
