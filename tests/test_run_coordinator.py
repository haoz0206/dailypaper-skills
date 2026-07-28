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

import run_coordinator
from run_lifecycle import Interruption, RunLifecycle


FINGERPRINT = "a" * 64
TARGET_DATE = "2026-07-28"


class RunCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.runs = self.root / "runs"
        self.vault.mkdir()
        self.environment = patch.dict(
            os.environ,
            {"DAILYPAPER_RUN_ROOT": str(self.runs)},
            clear=False,
        )
        self.environment.start()
        self.common_patches = [
            patch.object(run_coordinator, "obsidian_vault_path", return_value=self.vault),
            patch.object(run_coordinator, "timezone_name", return_value="Asia/Shanghai"),
            patch.object(
                run_coordinator,
                "_configuration_fingerprint",
                return_value=FINGERPRINT,
            ),
            patch.object(run_coordinator, "_dirty_paths", return_value=set()),
            patch.object(run_coordinator, "_spawn_guardian"),
            patch.object(run_coordinator, "_stop_guardian"),
        ]
        for item in self.common_patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.common_patches):
            item.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def _manifest_for_remote(
        self,
        run_id: str,
        *,
        interrupted: bool = False,
    ) -> Path:
        manifest = self.runs / run_id / "manifest.json"
        lifecycle = RunLifecycle.create(
            manifest,
            run_id=run_id,
            target_date=TARGET_DATE,
            timezone="Asia/Shanghai",
            vault=self.vault,
            contract=run_coordinator.WORKFLOW_CONTRACT,
            configuration_fingerprint=FINGERPRINT,
        )
        lifecycle.advance("fetching")
        if interrupted:
            lifecycle.interrupt(Interruption("network unavailable"))
        return manifest

    @staticmethod
    def _running_state(run_id: str) -> dict:
        return {
            "status": "running",
            "run_id": run_id,
            "target_date": TARGET_DATE,
            "harness": "codex",
            "owner": "server",
        }

    def test_start_creates_acquires_and_enters_fetching(self) -> None:
        with (
            patch.object(run_coordinator, "_inspect_task_state", return_value=None),
            patch.object(
                run_coordinator.vault_coordination,
                "acquire",
                return_value={"status": "acquired", "lock_commit": "lock-sha"},
            ),
            patch.object(
                run_coordinator,
                "repository_config",
                return_value={"remote": "origin", "branch": "main"},
            ),
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
            )

        self.assertEqual(result["decision"], "ready")
        self.assertEqual(result["mode"], "started")
        self.assertEqual(result["phase"], "fetching")
        manifest = Path(result["manifest"])
        self.assertTrue(manifest.exists())
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["workflow_contract"]["version"], 2)

    def test_start_resumes_matching_interrupted_local_run(self) -> None:
        run_id = f"{TARGET_DATE}-resume"
        manifest = self._manifest_for_remote(run_id, interrupted=True)
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id),
            ),
            patch.object(run_coordinator, "_guardian_is_alive", return_value=False),
        ):
            result = run_coordinator.start(
                harness="claude-code",
                target_date=TARGET_DATE,
            )

        self.assertEqual(result["decision"], "ready")
        self.assertEqual(result["mode"], "resumed")
        self.assertEqual(result["phase"], "fetching")
        self.assertEqual(result["condition"], "active")
        self.assertEqual(Path(result["manifest"]), manifest)

    def test_start_reports_still_running_when_guardian_is_alive(self) -> None:
        run_id = f"{TARGET_DATE}-active"
        manifest = self._manifest_for_remote(run_id)
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id),
            ),
            patch.object(run_coordinator, "_guardian_is_alive", return_value=True),
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
            )

        self.assertEqual(result["decision"], "still-running")
        self.assertEqual(result["manifest"], str(manifest))

    def test_start_requires_cancel_confirmation_when_local_run_is_missing(self) -> None:
        run_id = f"{TARGET_DATE}-remote"
        proposal = {
            "version": 1,
            "run_id": run_id,
            "vault": str(self.vault),
        }
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id),
            ),
            patch.object(
                run_coordinator,
                "_prepare_cancel",
                return_value=proposal,
            ) as prepare,
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
            )

        self.assertEqual(result["decision"], "cancel-confirmation-required")
        self.assertEqual(result["proposal"], proposal)
        prepare.assert_called_once_with(self.vault, expected_run_id=run_id)

    def test_submit_success_automatically_advances_and_publishes(self) -> None:
        run_id = f"{TARGET_DATE}-submit"
        manifest = self._manifest_for_remote(run_id)
        state = self._running_state(run_id)
        with (
            patch.object(run_coordinator, "_ensure_guardian") as ensure,
            patch.object(run_coordinator, "_inspect_task_state", return_value=state),
            patch.object(
                run_coordinator.vault_coordination,
                "complete",
                return_value={
                    "status": "success",
                    "content_commit": "content-sha",
                    "changed_paths": ["DailyPapers/today.md"],
                },
            ) as complete,
        ):
            ensure.side_effect = lambda opened, **_: opened.snapshot()

            fetched = manifest.parent / "candidates.json"
            fetched.write_text("[]\n", encoding="utf-8")
            enriched = manifest.parent / "enriched.json"
            enriched.write_text("[]\n", encoding="utf-8")
            first = run_coordinator.submit(
                manifest,
                result="success",
                artifacts=[
                    run_coordinator.ArtifactCandidate("candidates", fetched),
                    run_coordinator.ArtifactCandidate("enriched", enriched),
                ],
            )
            self.assertEqual(first["phase"], "reviewing")

            reviewed = self.vault / "DailyPapers" / "today.md"
            reviewed.parent.mkdir()
            reviewed.write_text("# Daily\n", encoding="utf-8")
            history = self.vault / "DailyPapers" / ".history.json"
            history.write_text("[]\n", encoding="utf-8")
            second = run_coordinator.submit(
                manifest,
                result="success",
                artifacts=[
                    run_coordinator.ArtifactCandidate(
                        "recommendation",
                        reviewed,
                    ),
                    run_coordinator.ArtifactCandidate("history", history),
                ],
                changed_paths=[reviewed, history],
            )
            self.assertEqual(second["phase"], "writing-notes")

            reviewed.write_text("# Daily\n\n[[Paper]]\n", encoding="utf-8")
            final = run_coordinator.submit(
                manifest,
                result="success",
                artifacts=[
                    run_coordinator.ArtifactCandidate("daily-note", reviewed),
                    run_coordinator.ArtifactCandidate("history", history),
                ],
                changed_paths=[reviewed, history],
            )

        self.assertEqual(final["decision"], "published")
        self.assertEqual(final["outcome"], "published")
        self.assertEqual(final["phase"], "publishing")
        complete.assert_called_once_with(manifest)

    def test_submit_recoverable_preserves_run_for_resume(self) -> None:
        run_id = f"{TARGET_DATE}-interrupt"
        manifest = self._manifest_for_remote(run_id)
        with (
            patch.object(run_coordinator, "_ensure_guardian") as ensure,
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id),
            ),
        ):
            ensure.side_effect = lambda opened, **_: opened.snapshot()
            result = run_coordinator.submit(
                manifest,
                result="recoverable",
                message="temporary network failure",
                retry_at="2026-07-28T12:00:00+08:00",
            )

        self.assertEqual(result["decision"], "interrupted")
        self.assertEqual(result["condition"], "interrupted")
        self.assertIsNone(result["outcome"])

    def test_submit_progress_checkpoints_without_advancing(self) -> None:
        run_id = f"{TARGET_DATE}-progress"
        manifest = self._manifest_for_remote(run_id)
        with (
            patch.object(run_coordinator, "_ensure_guardian") as ensure,
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id),
            ),
        ):
            ensure.side_effect = lambda opened, **_: opened.snapshot()
            partial = manifest.parent / "partial.json"
            partial.write_text("{}\n", encoding="utf-8")
            result = run_coordinator.submit(
                manifest,
                result="progress",
                artifacts=[
                    run_coordinator.ArtifactCandidate("partial", partial)
                ],
            )

        self.assertEqual(result["decision"], "ready")
        self.assertEqual(result["mode"], "checkpointed")
        self.assertEqual(result["phase"], "fetching")

    def test_submit_cannot_implicitly_resume_attention_required_run(self) -> None:
        run_id = f"{TARGET_DATE}-attention"
        manifest = self._manifest_for_remote(run_id)
        lifecycle = run_coordinator._open_lifecycle(manifest)
        lifecycle.interrupt(
            Interruption("retry budget exhausted", attention_required=True)
        )

        with self.assertRaisesRegex(
            run_coordinator.CoordinatorError,
            "explicit user decision",
        ):
            run_coordinator.submit(manifest, result="success")

    def test_start_resumes_attention_only_after_exact_confirmation(self) -> None:
        run_id = f"{TARGET_DATE}-attention-confirmed"
        manifest = self._manifest_for_remote(run_id)
        lifecycle = run_coordinator._open_lifecycle(manifest)
        lifecycle.interrupt(
            Interruption("retry budget exhausted", attention_required=True)
        )
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id),
            ),
            patch.object(run_coordinator, "_guardian_is_alive", return_value=False),
        ):
            waiting = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
            )
            resumed = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
                confirm_attention_run_id=run_id,
            )

        self.assertEqual(waiting["decision"], "attention-required")
        self.assertEqual(resumed["decision"], "ready")
        self.assertEqual(resumed["condition"], "active")

    def test_cancel_updates_local_outcome_only_after_vault_cas(self) -> None:
        run_id = f"{TARGET_DATE}-cancel"
        manifest = self._manifest_for_remote(run_id)
        proposal = {
            "version": 1,
            "operation": "cancel-dailypaper-run",
            "vault": str(self.vault),
            "remote": "origin",
            "branch": "main",
            "remote_head": "confirmed-head",
            "expected_run_id": run_id,
        }
        with patch.object(
            run_coordinator,
            "_cancel_vault",
            return_value={
                "status": "cancelled",
                "run_id": run_id,
                "cancellation_commit": "cancel-sha",
            },
        ) as vault_cancel:
            result = run_coordinator.cancel(proposal)

        vault_cancel.assert_called_once_with(proposal)
        self.assertEqual(result["decision"], "cancelled")
        self.assertTrue(result["local_manifest_updated"])
        lifecycle = RunLifecycle.open(
            manifest,
            contract=run_coordinator.WORKFLOW_CONTRACT,
            configuration_fingerprint=FINGERPRINT,
            expected_vault=self.vault,
            expected_run_id=run_id,
        )
        self.assertEqual(lifecycle.snapshot().outcome, "cancelled")


if __name__ == "__main__":
    unittest.main()
