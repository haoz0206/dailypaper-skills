import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_ROOT = REPO_ROOT / "skills" / "daily-papers"
SHARED_DIR = SUITE_ROOT / "scripts" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import user_config


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HistoryAndLinksTests(unittest.TestCase):
    def tearDown(self) -> None:
        user_config.clear_config_cache()

    def test_history_uses_configured_daily_papers_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "paths": {
                            "obsidian_vault": str(root),
                            "daily_papers_folder": "ResearchDigest",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DAILYPAPER_CONFIG": str(config_path)},
                clear=False,
            ):
                user_config.clear_config_cache()
                module = load_module(
                    "update_history_under_test",
                    REPO_ROOT
                    / "skills"
                    / "daily-papers"
                    / "scripts"
                    / "review"
                    / "update_history.py",
                )
                module.update_history(
                    [{"id": "2607.00001", "title": "Example"}],
                    "2026-07-26",
                )

            history_path = root / "ResearchDigest" / ".history.json"
            self.assertTrue(history_path.exists())
            history = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual(history[0]["id"], "2607.00001")

    def test_backfill_scan_honors_notes_dir_and_concepts_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            notes = root / "CustomNotes"
            concepts = notes / "_concepts"
            paper = notes / "Robotics" / "Paper.md"
            concept = concepts / "Topic" / "Concept.md"
            paper.parent.mkdir(parents=True)
            concept.parent.mkdir(parents=True)
            paper.write_text("# paper\n", encoding="utf-8")
            concept.write_text("# concept\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {"DAILYPAPER_VAULT": str(root)},
                clear=False,
            ):
                user_config.clear_config_cache()
                module = load_module(
                    "backfill_links_under_test",
                    REPO_ROOT
                    / "skills"
                    / "daily-papers"
                    / "scripts"
                    / "notes"
                    / "backfill_links.py",
                )
                index = module.scan_notes(
                    notes_dir=notes,
                    concepts_path=concepts,
                )

            self.assertEqual(set(index), {"paper"})


if __name__ == "__main__":
    unittest.main()
