import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SHARED_DIR = Path(__file__).resolve().parents[1] / "skills" / "_shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import user_config


class UserConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        user_config.clear_config_cache()

    def test_vault_environment_override_is_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {"DAILYPAPER_VAULT": temp_dir},
                clear=False,
            ):
                user_config.clear_config_cache()
                self.assertEqual(
                    user_config.obsidian_vault_path(),
                    Path(temp_dir).resolve(),
                )

    def test_relative_vault_is_anchored_to_explicit_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            config_path = workspace / "config.json"
            config_path.write_text(
                json.dumps({"paths": {"obsidian_vault": "."}}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_CONFIG": str(config_path),
                    "DAILYPAPER_WORKSPACE": str(workspace),
                },
                clear=False,
            ):
                user_config.clear_config_cache()
                self.assertEqual(
                    user_config.obsidian_vault_path(),
                    workspace.resolve(),
                )

    def test_push_is_disabled_without_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {"automation": {"git_commit": False, "git_push": True}}
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DAILYPAPER_CONFIG": str(config_path)},
                clear=False,
            ):
                user_config.clear_config_cache()
                self.assertFalse(user_config.git_push_enabled())

    def test_tracked_config_contains_no_personal_absolute_path(self) -> None:
        config = json.loads(
            (SHARED_DIR / "user-config.json").read_text(encoding="utf-8")
        )
        paths = config["paths"]
        self.assertEqual(paths["obsidian_vault"], ".")
        self.assertNotIn("/Users/", json.dumps(paths))

    def test_default_output_paths_match_shared_harness_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {"DAILYPAPER_VAULT": temp_dir},
                clear=False,
            ):
                user_config.clear_config_cache()
                vault = Path(temp_dir).resolve()
                self.assertEqual(
                    user_config.daily_papers_dir(),
                    vault / "DailyPapers",
                )
                self.assertEqual(
                    user_config.paper_notes_dir(),
                    vault / "论文笔记",
                )
                self.assertEqual(
                    user_config.concepts_dir(),
                    vault / "论文笔记" / "_概念",
                )
                self.assertEqual(
                    user_config.paper_inbox_dir(),
                    vault / "论文笔记" / "_待整理",
                )


if __name__ == "__main__":
    unittest.main()
