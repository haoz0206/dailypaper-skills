from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT
    / "skills"
    / "daily-papers"
    / "scripts"
    / "configure"
    / "onboard.py"
)
SPEC = importlib.util.spec_from_file_location("onboard_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
onboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(onboard)


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.machine_config = self.root / "machine" / "config.json"
        self.environment = patch.dict(
            os.environ,
            {"DAILYPAPER_MACHINE_CONFIG": str(self.machine_config)},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_existing_vault_bootstraps_before_machine_config_is_written(self) -> None:
        vault = self.root / "vault"
        vault.mkdir()

        def bootstrap(target: Path) -> dict:
            self.assertEqual(target, vault)
            self.assertFalse(self.machine_config.exists())
            return {"status": "already-bootstrapped", "vault": str(target)}

        with patch.object(onboard, "bootstrap_vault", side_effect=bootstrap):
            result = onboard.onboard_machine(vault)

        self.assertFalse(result["cloned"])
        self.assertEqual(result["config"]["vault_path"], str(vault))
        self.assertTrue(self.machine_config.is_file())

    def test_bootstrap_failure_never_persists_machine_config(self) -> None:
        vault = self.root / "vault"
        vault.mkdir()

        with patch.object(
            onboard,
            "bootstrap_vault",
            side_effect=onboard.CoordinationError(
                "bootstrap-failed",
                "injected failure",
            ),
        ):
            with self.assertRaises(onboard.CoordinationError):
                onboard.onboard_machine(vault)

        self.assertFalse(self.machine_config.exists())

    def test_missing_vault_clones_to_unique_sibling_before_bootstrap(self) -> None:
        vault = self.root / "vault"
        commands: list[list[str]] = []

        def clone(*arguments, **_kwargs):
            command = ["git", *arguments]
            commands.append(command)
            destination = Path(command[-1])
            self.assertNotEqual(destination, vault)
            self.assertEqual(destination.parent, vault.parent)
            destination.mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")

        def bootstrap(target: Path) -> dict:
            self.assertEqual(target, vault)
            self.assertTrue(target.is_dir())
            return {"status": "bootstrapped", "vault": str(target)}

        with (
            patch.object(onboard, "run_git_program", side_effect=clone),
            patch.object(onboard, "bootstrap_vault", side_effect=bootstrap),
        ):
            result = onboard.onboard_machine(vault)

        self.assertTrue(result["cloned"])
        self.assertEqual(commands[0][0:4], ["git", "clone", "--branch", "main"])
        self.assertIn(onboard.FIXED_VAULT_URL, commands[0])
        self.assertEqual(list(self.root.glob(".vault.clone-*")), [])

    def test_failed_clone_cleans_only_its_unique_temporary_directory(self) -> None:
        vault = self.root / "vault"
        user_file = self.root / "keep.txt"
        user_file.write_text("keep", encoding="utf-8")

        def fail_clone(*arguments, **_kwargs):
            command = ["git", *arguments]
            destination = Path(command[-1])
            destination.mkdir()
            (destination / "partial").write_text("partial", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, "", "network failed")

        with patch.object(onboard, "run_git_program", side_effect=fail_clone):
            with self.assertRaisesRegex(onboard.OnboardingError, "network failed"):
                onboard.onboard_machine(vault)

        self.assertFalse(vault.exists())
        self.assertEqual(list(self.root.glob(".vault.clone-*")), [])
        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep")
        self.assertFalse(self.machine_config.exists())

    def test_symlink_target_is_rejected_without_touching_destination(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "vault"
        link.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(onboard.OnboardingError, "symlink"):
            onboard.onboard_machine(link)

        self.assertTrue(link.is_symlink())
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
