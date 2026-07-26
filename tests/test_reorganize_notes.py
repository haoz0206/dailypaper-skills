import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "skills" / "_shared"
MODULE_PATH = (
    REPO_ROOT
    / "skills"
    / "paper-reader"
    / "assets"
    / "reorganize_notes.py"
)
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import user_config


def load_reorganize_module():
    spec = importlib.util.spec_from_file_location(
        "reorganize_notes_under_test",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ReorganizeNotesTests(unittest.TestCase):
    def tearDown(self) -> None:
        user_config.clear_config_cache()

    def test_concept_directory_is_excluded_using_configured_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            with patch.dict(
                os.environ,
                {"DAILYPAPER_VAULT": str(vault)},
                clear=False,
            ):
                user_config.clear_config_cache()
                module = load_reorganize_module()
                notes_root = vault / "PaperNotes"
                concept_note = notes_root / "_concepts" / "topic" / "Concept.md"
                paper_note = notes_root / "Robotics" / "Paper.md"
                concept_note.parent.mkdir(parents=True)
                paper_note.parent.mkdir(parents=True)
                concept_note.write_text("# concept\n", encoding="utf-8")
                paper_note.write_text("# paper\n", encoding="utf-8")
                module.PAPER_NOTES_ROOT = notes_root

                found = module.get_all_notes()

            self.assertEqual(found, [paper_note])


if __name__ == "__main__":
    unittest.main()
