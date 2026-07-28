import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT
    / "skills"
    / "daily-papers"
    / "scripts"
    / "configure"
    / "config_manager.py"
)
SPEC = importlib.util.spec_from_file_location("config_manager", MODULE_PATH)
assert SPEC and SPEC.loader
config_manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config_manager)


class ConfigManagerTests(unittest.TestCase):
    def _vault(self, root: Path) -> tuple[Path, Path]:
        vault = root / "vault"
        config_path = vault / ".dailypaper" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "paths": {"obsidian_vault": "."},
                    "repository": {
                        "url": "git@github.com:haoz0206/dailypaper-vault.git",
                        "remote": "origin",
                        "branch": "main",
                    },
                }
            ),
            encoding="utf-8",
        )
        return vault, config_path

    def test_plan_normalizes_keywords_and_pins_daily_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps(
                    {
                        "daily_papers": {
                            "keywords": [
                                " Robot Learning ",
                                "robot learning",
                                "VLA",
                            ],
                            "top_n": 15,
                        }
                    }
                ),
                encoding="utf-8",
            )

            plan = config_manager.build_plan(config_path, patch_path)

            self.assertEqual(
                plan["proposed"]["daily_papers"]["keywords"],
                ["robot learning", "vla"],
            )
            self.assertEqual(plan["proposed"]["daily_papers"]["top_n"], 15)
            self.assertEqual(
                plan["proposed"]["daily_papers"]["arxiv_categories"],
                ["cs.RO", "cs.CV", "cs.AI", "cs.LG"],
            )
            self.assertEqual(
                plan["proposed"]["repository"]["branch"],
                "main",
            )

    def test_apply_writes_valid_config_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps(
                    {
                        "daily_papers": {
                            "arxiv_categories": ["cs.RO", "cs.CV"],
                            "min_score": 3,
                        },
                        "automation": {"auto_refresh_indexes": False},
                    }
                ),
                encoding="utf-8",
            )

            result = config_manager.apply_plan(_vault, config_path, patch_path)
            written = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "applied")
            self.assertEqual(
                written["daily_papers"]["arxiv_categories"],
                ["cs.RO", "cs.CV"],
            )
            self.assertEqual(written["daily_papers"]["min_score"], 3)
            self.assertFalse(written["automation"]["auto_refresh_indexes"])
            self.assertEqual(
                config_manager.validate_config(config_path)["daily_papers"][
                    "min_score"
                ],
                3,
            )
            self.assertFalse(list(config_path.parent.glob(".config.*.tmp")))

    def test_rejects_unsupported_inert_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps(
                    {
                        "daily_papers": {
                            "arxiv_date_mode": "calendar-day",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "Unsupported daily_papers patch fields",
            ):
                config_manager.build_plan(config_path, patch_path)

    def test_rejects_positive_negative_keyword_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps(
                    {
                        "daily_papers": {
                            "keywords": ["robot learning"],
                            "negative_keywords": ["robot learning"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "conflict",
            ):
                config_manager.build_plan(config_path, patch_path)

    def test_active_run_blocks_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            state_path = vault / ".dailypaper" / "tasks" / "daily-papers.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "task": "daily-papers",
                        "status": "running",
                        "run_id": "run-123",
                        "harness": "codex",
                        "owner": "server",
                        "lease_until": "2026-08-01T00:00:00+08:00",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(config_manager.ActiveRunError):
                config_manager.guard_active_run(vault)

    def test_cancelled_run_allows_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            state_path = vault / ".dailypaper" / "tasks" / "daily-papers.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "task": "daily-papers",
                        "status": "cancelled",
                        "run_id": "run-123",
                        "cancelled_at": "2026-08-01T00:00:00+08:00",
                    }
                ),
                encoding="utf-8",
            )

            result = config_manager.guard_active_run(vault)

            self.assertEqual(result["status"], "safe")
            self.assertEqual(result["task_state"], "cancelled")

    def test_active_run_blocks_apply_without_changing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault, config_path = self._vault(root)
            original = config_path.read_bytes()
            state_path = vault / ".dailypaper" / "tasks" / "daily-papers.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "task": "daily-papers",
                        "status": "running",
                        "run_id": "run-apply-race",
                        "harness": "claude-code",
                        "owner": "server",
                        "lease_until": "2026-08-01T00:00:00+08:00",
                    }
                ),
                encoding="utf-8",
            )
            patch_path = root / "patch.json"
            patch_path.write_text(
                json.dumps({"daily_papers": {"top_n": 12}}),
                encoding="utf-8",
            )

            with self.assertRaises(config_manager.ActiveRunError):
                config_manager.apply_plan(vault, config_path, patch_path)

            self.assertEqual(config_path.read_bytes(), original)
            self.assertFalse(list(config_path.parent.glob(".config.*.tmp")))

    def test_shared_vault_path_cannot_be_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["paths"]["obsidian_vault"] = "/workspace/private-vault"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "must remain",
            ):
                config_manager.validate_config(config_path)

    def test_rejects_unknown_or_secret_shared_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["credentials"] = {"github_token": "secret"}
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "Unsupported shared configuration sections",
            ):
                config_manager.validate_config(config_path)

    def test_rejects_per_machine_zotero_paths_in_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["paths"]["zotero_db"] = "/srv/zotero.sqlite"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "Unsupported shared paths fields",
            ):
                config_manager.validate_config(config_path)

    def test_rejects_unsafe_relative_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _vault, config_path = self._vault(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["paths"]["paper_notes_folder"] = "../elsewhere"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with self.assertRaisesRegex(
                config_manager.ConfigError,
                "safe relative path",
            ):
                config_manager.validate_config(config_path)


if __name__ == "__main__":
    unittest.main()
