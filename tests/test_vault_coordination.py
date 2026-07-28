import json
import os
import subprocess
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

import run_context
import user_config
import vault_coordination


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


class VaultCoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.vault = self.root / "vault"
        self.runs = self.root / "runs"
        self.config_path = self.root / "config.json"

        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(self.remote)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(self.seed)],
            check=True,
            capture_output=True,
            text=True,
        )
        (self.seed / "README.md").write_text("# Vault\n", encoding="utf-8")
        git(self.seed, "add", "README.md")
        git(
            self.seed,
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        )
        git(self.seed, "remote", "add", "origin", str(self.remote))
        git(self.seed, "push", "-u", "origin", "main")
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.vault)],
            check=True,
            capture_output=True,
            text=True,
        )
        self._write_config(self.vault)

        self.environment = patch.dict(
            os.environ,
            {
                "DAILYPAPER_VAULT": str(self.vault),
                "DAILYPAPER_CONFIG": str(self.config_path),
                "DAILYPAPER_RUN_ROOT": str(self.runs),
            },
            clear=False,
        )
        self.environment.start()
        self.fixed_remote = patch.object(
            vault_coordination,
            "FIXED_VAULT_URL",
            str(self.remote),
        )
        self.fixed_remote.start()
        user_config.clear_config_cache()

    def tearDown(self) -> None:
        self.fixed_remote.stop()
        self.environment.stop()
        user_config.clear_config_cache()
        self.temporary.cleanup()

    def _write_config(self, vault: Path, *, top_n: int = 30) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "paths": {
                        "obsidian_vault": str(vault),
                    },
                    "daily_papers": {
                        "top_n": top_n,
                    },
                    "repository": {
                        "url": str(self.remote),
                        "remote": "origin",
                        "branch": "main",
                        "task_state_file": (
                            ".dailypaper/tasks/daily-papers.json"
                        ),
                        "pull_before_run": True,
                        "require_clean": True,
                        "coordination_enabled": True,
                        "lease_hours": 24,
                        "same_day_policy": "skip",
                    },
                }
            ),
            encoding="utf-8",
        )

    def _manifest(self, vault: Path | None = None) -> Path:
        selected_vault = vault or self.vault
        with patch.dict(
            os.environ,
            {
                "DAILYPAPER_VAULT": str(selected_vault),
                "DAILYPAPER_RUN_ROOT": str(
                    self.root / f"runs-{selected_vault.name}"
                ),
            },
            clear=False,
        ):
            user_config.clear_config_cache()
            return run_context.create_run(
                target_date="2026-07-26",
                timezone="Asia/Shanghai",
            )

    def test_acquire_pushes_machine_readable_task_state(self) -> None:
        manifest = self._manifest()
        result = vault_coordination.acquire(
            manifest,
            harness="codex",
            owner="test-host",
        )

        self.assertEqual(result["status"], "acquired")
        state_path = (
            self.vault / ".dailypaper" / "tasks" / "daily-papers.json"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["run_id"], result["run_id"])
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["harness"], "codex")
        self.assertEqual(git(self.vault, "rev-parse", "HEAD"), result["lock_commit"])

        manifest_data = run_context.load_manifest(manifest)
        self.assertEqual(
            manifest_data["coordination"]["lock_commit"],
            result["lock_commit"],
        )

    def test_active_remote_run_blocks_another_clone(self) -> None:
        first_manifest = self._manifest()
        first = vault_coordination.acquire(
            first_manifest,
            harness="codex",
            owner="first-host",
        )
        other_vault = self.root / "other-vault"
        subprocess.run(
            ["git", "clone", str(self.remote), str(other_vault)],
            check=True,
            capture_output=True,
            text=True,
        )
        self._write_config(other_vault)

        with patch.dict(
            os.environ,
            {
                "DAILYPAPER_VAULT": str(other_vault),
                "DAILYPAPER_CONFIG": str(self.config_path),
            },
            clear=False,
        ):
            user_config.clear_config_cache()
            second_manifest = self._manifest(other_vault)
            with self.assertRaises(vault_coordination.CoordinationError) as caught:
                vault_coordination.acquire(
                    second_manifest,
                    harness="claude-code",
                    owner="second-host",
                )

        self.assertEqual(first["status"], "acquired")
        self.assertEqual(caught.exception.status, "locked")

    def test_complete_publishes_outputs_and_marks_success(self) -> None:
        manifest = self._manifest()
        vault_coordination.acquire(
            manifest,
            harness="codex",
            owner="test-host",
        )
        daily_output = (
            self.vault / "DailyPapers" / "2026-07-26-论文推荐.md"
        )
        daily_output.parent.mkdir(parents=True)
        daily_output.write_text("# 今日锐评\n", encoding="utf-8")
        run_context.update_manifest(
            manifest,
            status="validated",
            changed_paths=[daily_output],
        )

        result = vault_coordination.complete(manifest)

        self.assertEqual(result["status"], "success")
        self.assertEqual(git(self.vault, "status", "--porcelain"), "")
        state = json.loads(
            (
                self.vault
                / ".dailypaper"
                / "tasks"
                / "daily-papers.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["changed_paths"], [
            "DailyPapers/2026-07-26-论文推荐.md"
        ])

        reader = self.root / "reader"
        subprocess.run(
            ["git", "clone", str(self.remote), str(reader)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertTrue(
            (reader / "DailyPapers" / "2026-07-26-论文推荐.md").exists()
        )

    def test_completed_date_is_idempotent(self) -> None:
        first_manifest = self._manifest()
        vault_coordination.acquire(
            first_manifest,
            harness="codex",
            owner="test-host",
        )
        daily_output = (
            self.vault / "DailyPapers" / "2026-07-26-论文推荐.md"
        )
        daily_output.parent.mkdir(parents=True)
        daily_output.write_text("# 今日锐评\n", encoding="utf-8")
        run_context.update_manifest(
            first_manifest,
            status="validated",
            changed_paths=[daily_output],
        )
        vault_coordination.complete(first_manifest)

        second_manifest = self._manifest()
        result = vault_coordination.acquire(
            second_manifest,
            harness="claude-code",
            owner="test-host",
        )
        self.assertEqual(result["status"], "already-completed")

    def test_configuration_change_blocks_publication(self) -> None:
        manifest = self._manifest()
        vault_coordination.acquire(
            manifest,
            harness="codex",
            owner="test-host",
        )
        daily_output = (
            self.vault / "DailyPapers" / "2026-07-26-论文推荐.md"
        )
        daily_output.parent.mkdir(parents=True)
        daily_output.write_text("# 今日锐评\n", encoding="utf-8")
        run_context.update_manifest(
            manifest,
            status="validated",
            changed_paths=[daily_output],
        )
        self._write_config(self.vault, top_n=29)
        user_config.clear_config_cache()

        with self.assertRaises(vault_coordination.CoordinationError) as caught:
            vault_coordination.complete(manifest)

        self.assertEqual(caught.exception.status, "config-conflict")

    def test_lost_acquisition_race_does_not_move_local_head(self) -> None:
        manifest = self._manifest()
        initial_head = git(self.vault, "rev-parse", "HEAD")
        with patch.object(
            vault_coordination,
            "_push_lock_commit",
            return_value=(False, "candidate", "non-fast-forward"),
        ):
            with self.assertRaises(vault_coordination.CoordinationError) as caught:
                vault_coordination.acquire(
                    manifest,
                    harness="claude-code",
                    owner="losing-host",
                )

        self.assertEqual(caught.exception.status, "lock-raced")
        self.assertEqual(git(self.vault, "rev-parse", "HEAD"), initial_head)
        self.assertFalse(
            (
                self.vault
                / ".dailypaper"
                / "tasks"
                / "daily-papers.json"
            ).exists()
        )

    def test_repository_url_cannot_be_overridden(self) -> None:
        manifest = self._manifest()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["repository"]["url"] = str(self.root / "other.git")
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        user_config.clear_config_cache()

        with self.assertRaises(vault_coordination.CoordinationError) as caught:
            vault_coordination.acquire(
                manifest,
                harness="claude-code",
                owner="test-host",
            )

        self.assertEqual(caught.exception.status, "invalid-config")

    def test_bootstrap_initializes_an_empty_remote(self) -> None:
        empty_remote = self.root / "empty.git"
        empty_vault = self.root / "empty-vault"
        subprocess.run(
            [
                "git",
                "init",
                "--bare",
                "--initial-branch=main",
                str(empty_remote),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "clone", str(empty_remote), str(empty_vault)],
            check=True,
            capture_output=True,
            text=True,
        )

        with patch.object(
            vault_coordination,
            "FIXED_VAULT_URL",
            str(empty_remote),
        ):
            result = vault_coordination.bootstrap_vault(empty_vault)
            repeated = vault_coordination.bootstrap_vault(empty_vault)

        self.assertEqual(result["status"], "bootstrapped")
        self.assertEqual(repeated["status"], "already-bootstrapped")
        self.assertEqual(git(empty_vault, "status", "--porcelain"), "")
        self.assertIn(
            ".dailypaper/runs/",
            (empty_vault / ".gitignore").read_text(encoding="utf-8"),
        )
        config = json.loads(
            (
                empty_vault / ".dailypaper" / "config.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(config["paths"]["obsidian_vault"], ".")
        self.assertEqual(config["repository"]["branch"], "main")

        run_state = (
            empty_vault
            / ".dailypaper"
            / "runs"
            / "local-only"
            / "manifest.json"
        )
        run_state.parent.mkdir(parents=True)
        run_state.write_text("{}\n", encoding="utf-8")
        self.assertEqual(git(empty_vault, "status", "--porcelain"), "")

    def test_acquire_reloads_vault_config_after_pull(self) -> None:
        vault_config = self.vault / ".dailypaper" / "config.json"
        vault_config.parent.mkdir(parents=True)
        initial_config = json.loads(
            self.config_path.read_text(encoding="utf-8")
        )
        initial_config["paths"]["obsidian_vault"] = "."
        vault_config.write_text(
            json.dumps(initial_config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        git(self.vault, "add", ".dailypaper/config.json")
        git(
            self.vault,
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "add vault config",
        )
        git(self.vault, "push", "origin", "main")

        with patch.dict(
            os.environ,
            {
                "DAILYPAPER_CONFIG": str(vault_config),
            },
            clear=False,
        ):
            user_config.clear_config_cache()
            manifest = self._manifest()

            updater = self.root / "config-updater"
            subprocess.run(
                ["git", "clone", str(self.remote), str(updater)],
                check=True,
                capture_output=True,
                text=True,
            )
            updated_config = json.loads(
                (updater / ".dailypaper" / "config.json").read_text(
                    encoding="utf-8"
                )
            )
            updated_config["daily_papers"]["top_n"] = 29
            (updater / ".dailypaper" / "config.json").write_text(
                json.dumps(
                    updated_config,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            git(updater, "add", ".dailypaper/config.json")
            git(
                updater,
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "update vault config",
            )
            git(updater, "push", "origin", "main")

            result = vault_coordination.acquire(
                manifest,
                harness="claude-code",
                owner="test-host",
            )
            state = json.loads(
                (
                    self.vault
                    / ".dailypaper"
                    / "tasks"
                    / "daily-papers.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "acquired")
        self.assertEqual(user_config.daily_papers_config()["top_n"], 29)
        self.assertEqual(
            state["config_sha256"],
            vault_coordination._config_fingerprint(),
        )


if __name__ == "__main__":
    unittest.main()
