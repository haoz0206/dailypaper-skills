import copy
import json
import os
import stat
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
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)

    def test_user_config_uses_machine_vault_and_auto_loads_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            machine_path = root / "machine.json"
            vault = root / "vault"
            shared_path = vault / ".dailypaper" / "config.json"
            shared_path.parent.mkdir(parents=True)
            effective = copy.deepcopy(user_config.DEFAULT_CONFIG)
            effective["daily_papers"]["top_n"] = 11
            shared_path.write_text(
                json.dumps(
                    user_config.config_schema.materialize_shared_config(
                        effective,
                        user_config.DEFAULT_CONFIG,
                    )
                ),
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

    def test_rejects_boolean_version_and_duplicate_json_keys(self) -> None:
        with self.assertRaises(machine_config.MachineConfigError):
            machine_config.normalize_machine_config(
                {"version": True, "vault_path": "/tmp/vault"}
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "machine.json"
            path.write_text(
                '{"version":1,"vault_path":"/tmp/a","vault_path":"/tmp/b"}',
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DAILYPAPER_MACHINE_CONFIG": str(path)},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    machine_config.MachineConfigError,
                    "duplicate JSON key",
                ):
                    machine_config.load_machine_config(required=True)

    def test_rejects_non_file_oversized_and_too_deep_machine_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "machine.json"
            with patch.dict(
                os.environ,
                {"DAILYPAPER_MACHINE_CONFIG": str(config_path)},
                clear=False,
            ):
                config_path.mkdir()
                with self.assertRaisesRegex(
                    machine_config.MachineConfigError,
                    "regular file",
                ):
                    machine_config.load_machine_config(required=True)
                config_path.rmdir()

                config_path.write_bytes(
                    b" " * (machine_config.MAX_MACHINE_CONFIG_BYTES + 1)
                )
                with self.assertRaisesRegex(
                    machine_config.MachineConfigError,
                    "safety limit",
                ):
                    machine_config.load_machine_config(required=True)

                config_path.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
                with self.assertRaisesRegex(
                    machine_config.MachineConfigError,
                    "bounded UTF-8 JSON|JSON object",
                ):
                    machine_config.load_machine_config(required=True)

    def test_rejects_symlink_and_relative_machine_config_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.json"
            target.write_text(
                '{"version":1,"vault_path":"/tmp/vault"}',
                encoding="utf-8",
            )
            link = root / "machine.json"
            link.symlink_to(target)
            with patch.dict(
                os.environ,
                {"DAILYPAPER_MACHINE_CONFIG": str(link)},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    machine_config.MachineConfigError,
                    "symbolic link",
                ):
                    machine_config.load_machine_config(required=True)
                with self.assertRaisesRegex(
                    machine_config.MachineConfigError,
                    "symbolic link",
                ):
                    machine_config.write_machine_config(
                        {"version": 1, "vault_path": "/tmp/vault"}
                    )

        with patch.dict(
            os.environ,
            {"DAILYPAPER_MACHINE_CONFIG": "relative.json"},
            clear=False,
        ):
            with self.assertRaisesRegex(
                machine_config.MachineConfigError,
                "absolute path",
            ):
                machine_config.machine_config_path()

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
