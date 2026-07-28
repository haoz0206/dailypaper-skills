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

import machine_config
import user_config


class MachineConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        user_config.clear_config_cache()

    def test_round_trip_uses_one_cross_harness_machine_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "machine.json"
            vault = root / "vault"
            zotero_db = root / "Zotero" / "zotero.sqlite"
            with patch.dict(
                os.environ,
                {"DAILYPAPER_MACHINE_CONFIG": str(config_path)},
                clear=False,
            ):
                written = machine_config.write_machine_config(
                    machine_config.build_machine_config(
                        vault_path=vault,
                        zotero_database=zotero_db,
                    )
                )
                loaded = machine_config.load_machine_config(required=True)

            self.assertEqual(written, loaded)
            self.assertEqual(loaded["vault_path"], str(vault.resolve()))
            self.assertEqual(
                loaded["zotero"]["database_path"],
                str(zotero_db.resolve()),
            )

    def test_user_config_uses_machine_vault_and_auto_loads_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            machine_path = root / "machine.json"
            vault = root / "vault"
            shared_path = vault / ".dailypaper" / "config.json"
            shared_path.parent.mkdir(parents=True)
            shared_path.write_text(
                json.dumps({"daily_papers": {"top_n": 11}}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_MACHINE_CONFIG": str(machine_path),
                },
                clear=False,
            ):
                with patch.dict(
                    os.environ,
                    {
                        "DAILYPAPER_VAULT": "",
                        "DAILYPAPER_CONFIG": "",
                    },
                    clear=False,
                ):
                    machine_config.write_machine_config(
                        {
                            "version": 1,
                            "vault_path": str(vault),
                        }
                    )
                    os.environ.pop("DAILYPAPER_VAULT", None)
                    os.environ.pop("DAILYPAPER_CONFIG", None)
                    user_config.clear_config_cache()
                    self.assertEqual(
                        user_config.obsidian_vault_path(),
                        vault.resolve(),
                    )
                    self.assertEqual(
                        user_config.shared_config_path(),
                        shared_path.resolve(),
                    )
                    self.assertEqual(
                        user_config.daily_papers_config()["top_n"],
                        11,
                    )

    def test_environment_vault_overrides_machine_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            machine_path = root / "machine.json"
            machine_vault = root / "machine-vault"
            environment_vault = root / "environment-vault"
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_MACHINE_CONFIG": str(machine_path),
                    "DAILYPAPER_VAULT": str(environment_vault),
                },
                clear=False,
            ):
                machine_config.write_machine_config(
                    {
                        "version": 1,
                        "vault_path": str(machine_vault),
                    }
                )
                user_config.clear_config_cache()
                self.assertEqual(
                    user_config.obsidian_vault_path(),
                    environment_vault.resolve(),
                )

    def test_rejects_relative_or_unknown_machine_settings(self) -> None:
        with self.assertRaises(machine_config.MachineConfigError):
            machine_config.normalize_machine_config(
                {"version": 1, "vault_path": "relative/vault"}
            )
        with self.assertRaises(machine_config.MachineConfigError):
            machine_config.normalize_machine_config(
                {
                    "version": 1,
                    "vault_path": "/tmp/vault",
                    "token": "secret",
                }
            )

    def test_explicit_set_can_repair_a_malformed_machine_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "machine.json"
            vault = root / "vault"
            config_path.write_text("{not-json", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"DAILYPAPER_MACHINE_CONFIG": str(config_path)},
                clear=False,
            ):
                repaired = machine_config.write_machine_config(
                    machine_config.build_machine_config(vault_path=vault)
                )

            self.assertEqual(repaired["vault_path"], str(vault.resolve()))
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                repaired,
            )


if __name__ == "__main__":
    unittest.main()
