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

import run_context
import user_config


class RunContextTests(unittest.TestCase):
    def tearDown(self) -> None:
        user_config.clear_config_cache()

    def test_each_run_gets_isolated_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            runs = root / "runs"
            vault.mkdir()
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_VAULT": str(vault),
                    "DAILYPAPER_RUN_ROOT": str(runs),
                },
                clear=False,
            ):
                user_config.clear_config_cache()
                first = run_context.create_run(
                    target_date="2026-07-26",
                    timezone="Asia/Shanghai",
                )
                second = run_context.create_run(
                    target_date="2026-07-26",
                    timezone="Asia/Shanghai",
                )

            self.assertNotEqual(first.parent, second.parent)
            first_data = json.loads(first.read_text(encoding="utf-8"))
            second_data = json.loads(second.read_text(encoding="utf-8"))
            self.assertNotEqual(first_data["run_id"], second_data["run_id"])
            self.assertNotEqual(
                first_data["paths"]["enriched"],
                second_data["paths"]["enriched"],
            )
            self.assertEqual(first_data["paths"]["vault"], str(vault.resolve()))

    def test_invalid_timezone_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_VAULT": temp_dir,
                    "DAILYPAPER_RUN_ROOT": str(Path(temp_dir) / "runs"),
                },
                clear=False,
            ):
                user_config.clear_config_cache()
                with self.assertRaisesRegex(ValueError, "Unknown timezone"):
                    run_context.create_run(timezone="Mars/Olympus_Mons")

    def test_update_records_status_and_vault_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            runs = root / "runs"
            note = vault / "PaperNotes" / "paper.md"
            note.parent.mkdir(parents=True)
            note.write_text("# Paper\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_VAULT": str(vault),
                    "DAILYPAPER_RUN_ROOT": str(runs),
                },
                clear=False,
            ):
                user_config.clear_config_cache()
                manifest = run_context.create_run(
                    target_date="2026-07-26",
                    timezone="Asia/Shanghai",
                )
                updated = run_context.update_manifest(
                    manifest,
                    status="validated",
                    changed_paths=[note, Path("PaperNotes/paper.md")],
                )

            self.assertEqual(updated["status"], "validated")
            self.assertEqual(updated["changed_paths"], ["PaperNotes/paper.md"])

    def test_update_rejects_paths_outside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            vault.mkdir()
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_VAULT": str(vault),
                    "DAILYPAPER_RUN_ROOT": str(root / "runs"),
                },
                clear=False,
            ):
                user_config.clear_config_cache()
                manifest = run_context.create_run()
                with self.assertRaisesRegex(ValueError, "outside the Vault"):
                    run_context.update_manifest(
                        manifest,
                        changed_paths=[root / "elsewhere.md"],
                    )


if __name__ == "__main__":
    unittest.main()
