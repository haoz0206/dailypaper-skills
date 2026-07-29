from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "skills" / "daily-papers" / "scripts" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import active_run_guard  # noqa: E402
import runtime_context  # noqa: E402
import user_config  # noqa: E402
import vault_coordination  # noqa: E402
from tests.task_state_fixtures import make_task_state  # noqa: E402


class RuntimeContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.shared_config = self.vault / ".dailypaper" / "config.json"
        self.shared_config.parent.mkdir()
        self.shared_config.write_text("{}\n", encoding="utf-8")
        self.machine_config = self.root / "machine.json"
        self.machine_config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "vault_path": str(self.vault),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.environment = patch.dict(
            os.environ,
            {"DAILYPAPER_MACHINE_CONFIG": str(self.machine_config)},
            clear=True,
        )
        self.environment.start()
        user_config.clear_config_cache()

    def tearDown(self) -> None:
        user_config.clear_config_cache()
        self.environment.stop()
        self.temporary.cleanup()

    def test_resolves_safe_paths_and_matching_coordinator_fingerprint(self) -> None:
        context = runtime_context.resolve_runtime_context()

        self.assertEqual(context["version"], 1)
        self.assertEqual(context["status"], "ready")
        self.assertEqual(context["paths"]["vault"], str(self.vault.resolve()))
        self.assertEqual(
            context["paths"]["paper_notes"],
            str((self.vault / "论文笔记").resolve()),
        )
        self.assertEqual(context["runtime"]["timezone"], "Asia/Shanghai")
        self.assertFalse(context["zotero"]["enabled"])
        self.assertFalse(context["guard"]["checked"])
        self.assertEqual(
            context["configuration_fingerprint"],
            vault_coordination.configuration_fingerprint(),
        )

    def test_guard_blocks_running_run_and_reports_exact_owner(self) -> None:
        state_path = self.vault / ".dailypaper" / "tasks" / "daily-papers.json"
        state_path.parent.mkdir()
        state_path.write_text(
            json.dumps(
                make_task_state(
                    "running",
                    run_id="2026-07-29-active",
                    owner="server",
                )
            ),
            encoding="utf-8",
        )

        with self.assertRaises(active_run_guard.ActiveRunError) as raised:
            runtime_context.resolve_runtime_context(
                guard_active_run=True,
                guard_remote=False,
            )

        self.assertEqual(raised.exception.state["run_id"], "2026-07-29-active")
        self.assertIn("codex/server", str(raised.exception))

    def test_guard_accepts_absent_or_terminal_task_state(self) -> None:
        absent = runtime_context.resolve_runtime_context(
            guard_active_run=True,
            guard_remote=False,
        )
        self.assertEqual(absent["guard"]["task_state"], "absent")

        state_path = self.vault / ".dailypaper" / "tasks" / "daily-papers.json"
        state_path.parent.mkdir()
        state_path.write_text(
            json.dumps(
                make_task_state(
                    "published",
                    run_id="2026-07-28-done",
                )
            ),
            encoding="utf-8",
        )
        terminal = runtime_context.resolve_runtime_context(
            guard_active_run=True,
            guard_remote=False,
        )
        self.assertEqual(terminal["guard"]["task_state"], "published")

    def test_rejects_unsafe_or_inert_runtime_configuration(self) -> None:
        cases = (
            (
                {"paths": {"paper_notes_folder": "../outside"}},
                "normalized safe relative POSIX path",
            ),
            (
                {"automation": {"git_commit": False, "git_push": True}},
                "must be enabled or disabled together",
            ),
            (
                {"automation": {"git_commit": True, "git_push": False}},
                "must be enabled or disabled together",
            ),
            (
                {"repository": {"branch": "other"}},
                "repository.branch must remain",
            ),
            (
                {"daily_papers": {"unknown": True}},
                "unsupported daily_papers fields",
            ),
        )
        for payload, message in cases:
            with self.subTest(payload=payload):
                self.shared_config.write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
                user_config.clear_config_cache()
                with self.assertRaisesRegex(
                    runtime_context.RuntimeContextError,
                    message,
                ):
                    runtime_context.resolve_runtime_context()

    def test_rejects_duplicate_json_keys_in_shared_configuration(self) -> None:
        self.shared_config.write_text(
            '{"paths":{},"paths":{"obsidian_vault":"."}}',
            encoding="utf-8",
        )
        user_config.clear_config_cache()

        with self.assertRaisesRegex(
            runtime_context.RuntimeContextError,
            "duplicate JSON key",
        ):
            runtime_context.resolve_runtime_context()

    def test_zotero_requires_explicit_machine_configuration(self) -> None:
        database = self.root / "zotero.sqlite"
        storage = self.root / "storage"
        self.machine_config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "vault_path": str(self.vault),
                    "zotero": {
                        "database_path": str(database),
                        "storage_path": str(storage),
                    },
                }
            ),
            encoding="utf-8",
        )
        user_config.clear_config_cache()

        context = runtime_context.resolve_runtime_context()

        self.assertTrue(context["zotero"]["enabled"])
        self.assertEqual(context["zotero"]["database_path"], str(database))
        self.assertEqual(context["zotero"]["storage_path"], str(storage))

    def test_absolute_environment_vault_is_one_consistent_temporary_override(self) -> None:
        alternate = self.root / "alternate-vault"
        alternate.mkdir()
        config = alternate / ".dailypaper" / "config.json"
        config.parent.mkdir()
        config.write_text("{}\n", encoding="utf-8")
        self.machine_config.unlink()
        with patch.dict(
            os.environ,
            {"DAILYPAPER_VAULT": str(alternate)},
            clear=False,
        ):
            user_config.clear_config_cache()
            context = runtime_context.resolve_runtime_context()

        self.assertEqual(context["paths"]["vault"], str(alternate.resolve()))
        self.assertEqual(
            context["paths"]["paper_notes"],
            str((alternate / "论文笔记").resolve()),
        )

    def test_vault_path_can_be_resolved_before_shared_config_bootstrap(self) -> None:
        self.shared_config.unlink()

        self.assertEqual(runtime_context.resolve_vault_path(), self.vault.resolve())

    def test_explicit_shared_config_must_not_depend_on_current_directory(self) -> None:
        with patch.dict(
            os.environ,
            {"DAILYPAPER_CONFIG": "relative-config.json"},
            clear=False,
        ):
            with self.assertRaisesRegex(
                runtime_context.RuntimeContextError,
                "DAILYPAPER_CONFIG must be an absolute path",
            ):
                runtime_context.resolve_runtime_context()


if __name__ == "__main__":
    unittest.main()
