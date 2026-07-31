import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SHARED_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "daily-papers"
    / "scripts"
    / "shared"
)
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import user_config


class UserConfigTests(unittest.TestCase):
    def _shared_document(self, **updates: object) -> dict:
        effective = copy.deepcopy(user_config.DEFAULT_CONFIG)
        for section, value in updates.items():
            assert isinstance(value, dict)
            effective[section].update(value)
        return user_config.config_schema.materialize_shared_config(
            effective,
            user_config.DEFAULT_CONFIG,
        )

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
                json.dumps(self._shared_document()),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_CONFIG": str(config_path),
                    "DAILYPAPER_WORKSPACE": str(workspace),
                    "DAILYPAPER_MACHINE_CONFIG": str(
                        workspace / "missing-machine-config.json"
                    ),
                },
                clear=False,
            ):
                user_config.clear_config_cache()
                self.assertEqual(
                    user_config.obsidian_vault_path(),
                    workspace.resolve(),
                )

    def test_push_without_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            document = self._shared_document()
            document["automation"]["git_push"] = True
            config_path.write_text(
                json.dumps(document),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DAILYPAPER_CONFIG": str(config_path)},
                clear=False,
            ):
                user_config.clear_config_cache()
                with self.assertRaisesRegex(
                    user_config.config_schema.ConfigurationError,
                    "must be enabled or disabled together",
                ):
                    user_config.git_push_enabled()

    def test_commit_without_push_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            document = self._shared_document()
            document["automation"]["git_commit"] = True
            config_path.write_text(
                json.dumps(document),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DAILYPAPER_CONFIG": str(config_path)},
                clear=False,
            ):
                user_config.clear_config_cache()
                with self.assertRaisesRegex(
                    user_config.config_schema.ConfigurationError,
                    "must be enabled or disabled together",
                ):
                    user_config.git_commit_enabled()

    def test_external_configuration_must_not_be_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "config.json"
            link.symlink_to(target)
            with patch.dict(
                os.environ,
                {"DAILYPAPER_CONFIG": str(link)},
                clear=False,
            ):
                user_config.clear_config_cache()
                with self.assertRaisesRegex(
                    user_config.config_schema.ConfigurationError,
                    "readable regular file",
                ):
                    user_config.load_user_config()

            self.assertEqual(target.read_text(encoding="utf-8"), "{}")

    def test_tracked_config_contains_no_personal_absolute_path(self) -> None:
        config = json.loads(
            (SHARED_DIR / "defaults.json").read_text(encoding="utf-8")
        )
        paths = config["paths"]
        self.assertEqual(paths["obsidian_vault"], ".")
        self.assertNotIn("/Users/", json.dumps(paths))

    def test_legacy_vault_configuration_is_not_silently_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_CONFIG": str(config_path),
                    "DAILYPAPER_MACHINE_CONFIG": str(
                        Path(temp_dir) / "missing-machine-config.json"
                    ),
                },
                clear=False,
            ):
                user_config.clear_config_cache()
                with self.assertRaisesRegex(
                    user_config.config_schema.ConfigurationMigrationRequired,
                    "configure-dailypaper",
                ):
                    user_config.load_user_config()

    def test_versioned_vault_config_is_stable_when_bundled_defaults_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            document = self._shared_document()
            document["daily_papers"]["top_n"] = 17
            config_path.write_text(json.dumps(document), encoding="utf-8")
            changed_defaults = copy.deepcopy(user_config.DEFAULT_CONFIG)
            changed_defaults["daily_papers"]["top_n"] = 99
            real_load = user_config.config_schema.load_json_object

            def load_with_updated_package(path: Path, **kwargs: object) -> dict:
                if Path(path).name == "defaults.json":
                    return copy.deepcopy(changed_defaults)
                return real_load(path, **kwargs)

            with (
                patch.dict(
                    os.environ,
                    {
                        "DAILYPAPER_CONFIG": str(config_path),
                        "DAILYPAPER_MACHINE_CONFIG": str(
                            root / "missing-machine.json"
                        ),
                    },
                    clear=True,
                ),
                patch.object(
                    user_config.config_schema,
                    "load_json_object",
                    side_effect=load_with_updated_package,
                ),
            ):
                user_config.clear_config_cache()
                self.assertEqual(
                    user_config.daily_papers_config()["top_n"],
                    17,
                )

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

    def test_default_repository_matches_coordinated_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_MACHINE_CONFIG": str(
                        Path(temp_dir) / "missing-machine.json"
                    )
                },
                clear=True,
            ):
                user_config.clear_config_cache()
                repository = user_config.repository_config()
        self.assertEqual(
            repository["url"],
            "git@github.com:haoz0206/dailypaper-vault.git",
        )
        self.assertEqual(repository["remote"], "origin")
        self.assertEqual(repository["branch"], "main")
        self.assertTrue(repository["coordination_enabled"])


if __name__ == "__main__":
    unittest.main()
