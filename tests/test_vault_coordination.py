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

import run_lifecycle
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
        self.manifest_counter = 0
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
        self.manifest_counter += 1
        run_id = f"2026-07-26-run-{self.manifest_counter}"
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
            manifest = (
                Path(os.environ["DAILYPAPER_RUN_ROOT"])
                / run_id
                / "manifest.json"
            )
            run_lifecycle.RunLifecycle.create(
                manifest,
                run_id=run_id,
                target_date="2026-07-26",
                timezone="Asia/Shanghai",
                vault=selected_vault,
                contract=run_lifecycle.DAILY_WORKFLOW_CONTRACT,
                configuration_fingerprint=(
                    vault_coordination.configuration_fingerprint()
                ),
            )
            return manifest

    def _v2_manifest(self) -> tuple[Path, run_lifecycle.RunLifecycle]:
        contract = run_lifecycle.DAILY_WORKFLOW_CONTRACT
        manifest = self.runs / "2026-07-26-v2" / "manifest.json"
        lifecycle = run_lifecycle.RunLifecycle.create(
            manifest,
            run_id="2026-07-26-v2",
            target_date="2026-07-26",
            timezone="Asia/Shanghai",
            vault=self.vault,
            contract=contract,
            configuration_fingerprint=(
                vault_coordination.configuration_fingerprint()
            ),
        )
        return manifest, lifecycle

    def _prepare_v2_publication(
        self,
    ) -> tuple[Path, run_lifecycle.RunLifecycle, Path]:
        manifest, lifecycle = self._v2_manifest()
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
        self._advance_to_publication(manifest, lifecycle, daily_output)
        return manifest, lifecycle, daily_output

    def _advance_to_publication(
        self,
        manifest: Path,
        lifecycle: run_lifecycle.RunLifecycle,
        daily_output: Path,
    ) -> None:
        lifecycle.advance("fetching")
        candidates = manifest.parent / "candidates.json"
        candidates.write_text("[]\n", encoding="utf-8")
        enriched = manifest.parent / "enriched.json"
        enriched.write_text("[]\n", encoding="utf-8")
        lifecycle.checkpoint(
            artifacts=[
                run_lifecycle.ArtifactCandidate("candidates", candidates),
                run_lifecycle.ArtifactCandidate("enriched", enriched),
            ]
        )
        lifecycle.advance("reviewing")
        history = daily_output.parent / ".history.json"
        history.write_text("[]\n", encoding="utf-8")
        lifecycle.checkpoint(
            artifacts=[
                run_lifecycle.ArtifactCandidate(
                    "recommendation",
                    daily_output,
                ),
                run_lifecycle.ArtifactCandidate("history", history),
            ],
            changed_paths=[daily_output, history],
        )
        lifecycle.advance("writing-notes")
        lifecycle.checkpoint(
            artifacts=[
                run_lifecycle.ArtifactCandidate("daily-note", daily_output),
                run_lifecycle.ArtifactCandidate("history", history),
            ],
            changed_paths=[daily_output, history],
            allow_artifact_updates=True,
        )
        lifecycle.advance("validated")
        lifecycle.checkpoint(
            artifacts=[
                run_lifecycle.ArtifactCandidate("daily-note", daily_output),
                run_lifecycle.ArtifactCandidate("history", history),
            ],
            changed_paths=[daily_output, history],
        )
        lifecycle.advance("publishing")

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

        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest_data["publication"]["acquisition_commit"],
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
        lifecycle = run_lifecycle.RunLifecycle.open(
            manifest,
            contract=run_lifecycle.DAILY_WORKFLOW_CONTRACT,
            configuration_fingerprint=(
                vault_coordination.configuration_fingerprint()
            ),
            expected_vault=self.vault,
            expected_run_id=manifest.parent.name,
        )
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
        self._advance_to_publication(manifest, lifecycle, daily_output)

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
        self.assertEqual(
            state["changed_paths"],
            [
                "DailyPapers/2026-07-26-论文推荐.md",
                "DailyPapers/.history.json",
            ],
        )

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
        lifecycle = run_lifecycle.RunLifecycle.open(
            first_manifest,
            contract=run_lifecycle.DAILY_WORKFLOW_CONTRACT,
            configuration_fingerprint=(
                vault_coordination.configuration_fingerprint()
            ),
            expected_vault=self.vault,
            expected_run_id=first_manifest.parent.name,
        )
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
        self._advance_to_publication(
            first_manifest,
            lifecycle,
            daily_output,
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
        lifecycle = run_lifecycle.RunLifecycle.open(
            manifest,
            contract=run_lifecycle.DAILY_WORKFLOW_CONTRACT,
            configuration_fingerprint=(
                vault_coordination.configuration_fingerprint()
            ),
            expected_vault=self.vault,
            expected_run_id=manifest.parent.name,
        )
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
        self._advance_to_publication(manifest, lifecycle, daily_output)
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

    def test_acquire_rejects_manifest_created_before_remote_config_pull(
        self,
    ) -> None:
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

            with self.assertRaises(
                vault_coordination.CoordinationError
            ) as caught:
                vault_coordination.acquire(
                    manifest,
                    harness="claude-code",
                    owner="test-host",
                )

        self.assertEqual(user_config.daily_papers_config()["top_n"], 29)
        self.assertEqual(caught.exception.status, "config-conflict")

    def test_v2_acquire_records_immutable_publication_metadata(self) -> None:
        manifest, lifecycle = self._v2_manifest()

        result = vault_coordination.acquire(
            manifest,
            harness="codex",
            owner="test-host",
        )

        publication = lifecycle.snapshot().as_dict()["publication"]
        self.assertEqual(publication["acquisition_commit"], result["lock_commit"])
        self.assertEqual(publication["remote"], "origin")
        self.assertEqual(publication["branch"], "main")

    def test_v2_complete_reuses_content_commit_after_failed_push(self) -> None:
        manifest, lifecycle, _ = self._prepare_v2_publication()
        acquisition_commit = lifecycle.snapshot().as_dict()["publication"][
            "acquisition_commit"
        ]
        original_git = vault_coordination._git
        failed_once = False

        def fail_first_content_push(
            vault: Path,
            *args: str,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            nonlocal failed_once
            if (
                not failed_once
                and args
                and args[0] == "push"
                and any(":refs/heads/main" in value for value in args)
            ):
                failed_once = True
                return subprocess.CompletedProcess(
                    ["git", *args],
                    1,
                    "",
                    "simulated lost network",
                )
            return original_git(vault, *args, check=check)

        with patch.object(
            vault_coordination,
            "_git",
            side_effect=fail_first_content_push,
        ):
            with self.assertRaises(vault_coordination.CoordinationError) as caught:
                vault_coordination.complete(manifest)

        self.assertEqual(caught.exception.status, "publish-failed")
        interrupted = lifecycle.snapshot().as_dict()
        content_commit = interrupted["publication"]["content_commit"]
        self.assertIsNotNone(content_commit)
        self.assertEqual(interrupted["condition"], "interrupted")
        self.assertEqual(
            git(self.remote, "rev-parse", "refs/heads/main"),
            acquisition_commit,
        )

        result = vault_coordination.complete(manifest)

        self.assertEqual(result["content_commit"], content_commit)
        terminal = lifecycle.snapshot()
        self.assertEqual(terminal.outcome, "published")
        self.assertEqual(
            git(self.remote, "rev-parse", "refs/heads/main"),
            content_commit,
        )

    def test_v2_complete_accepts_ambiguous_successful_push(self) -> None:
        manifest, lifecycle, _ = self._prepare_v2_publication()
        original_git = vault_coordination._git
        obscured_once = False

        def obscure_successful_push(
            vault: Path,
            *args: str,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            nonlocal obscured_once
            if (
                not obscured_once
                and args
                and args[0] == "push"
                and any(":refs/heads/main" in value for value in args)
            ):
                obscured_once = True
                original_git(vault, *args, check=True)
                return subprocess.CompletedProcess(
                    ["git", *args],
                    1,
                    "",
                    "simulated lost response",
                )
            return original_git(vault, *args, check=check)

        with patch.object(
            vault_coordination,
            "_git",
            side_effect=obscure_successful_push,
        ):
            result = vault_coordination.complete(manifest)

        self.assertEqual(result["status"], "success")
        self.assertEqual(lifecycle.snapshot().outcome, "published")

    def test_v2_fail_publishes_failure_then_finishes_manifest(self) -> None:
        manifest, lifecycle = self._v2_manifest()
        vault_coordination.acquire(
            manifest,
            harness="codex",
            owner="test-host",
        )

        result = vault_coordination.fail(
            manifest,
            message="deterministic schema failure",
        )

        self.assertEqual(result["status"], "failed")
        terminal = lifecycle.snapshot()
        self.assertEqual(terminal.outcome, "failed")
        inspected = vault_coordination.inspect_task_state(self.vault)
        self.assertEqual(inspected["task_state"]["status"], "failed")

    def test_cancel_uses_confirmed_remote_head_and_preserves_local_artifacts(
        self,
    ) -> None:
        manifest, _ = self._v2_manifest()
        acquired = vault_coordination.acquire(
            manifest,
            harness="codex",
            owner="failed-server",
        )
        local_artifact = manifest.parent / "enriched.json"
        local_artifact.write_text("[]\n", encoding="utf-8")
        local_head = git(self.vault, "rev-parse", "HEAD")

        proposal = vault_coordination.prepare_cancel(
            self.vault,
            acquired["run_id"],
        )
        result = vault_coordination.cancel(proposal)

        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(local_artifact.exists())
        self.assertEqual(git(self.vault, "rev-parse", "HEAD"), local_head)
        inspected = vault_coordination.inspect_task_state(self.vault)
        self.assertEqual(inspected["task_state"]["status"], "cancelled")
        self.assertEqual(
            inspected["task_state"]["run_id"],
            acquired["run_id"],
        )

    def test_cancel_rejects_a_stale_proposal(self) -> None:
        manifest, _ = self._v2_manifest()
        acquired = vault_coordination.acquire(
            manifest,
            harness="codex",
            owner="failed-server",
        )
        proposal = vault_coordination.prepare_cancel(
            self.vault,
            acquired["run_id"],
        )
        updater = self.root / "remote-updater"
        subprocess.run(
            ["git", "clone", str(self.remote), str(updater)],
            check=True,
            capture_output=True,
            text=True,
        )
        (updater / "unrelated.md").write_text("remote change\n", encoding="utf-8")
        git(updater, "add", "unrelated.md")
        git(
            updater,
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "advance remote",
        )
        git(updater, "push", "origin", "main")

        with self.assertRaises(vault_coordination.CoordinationError) as caught:
            vault_coordination.cancel(proposal)

        self.assertEqual(caught.exception.status, "cancel-stale")
        inspected = vault_coordination.inspect_task_state(self.vault)
        self.assertEqual(inspected["task_state"]["status"], "running")


if __name__ == "__main__":
    unittest.main()
