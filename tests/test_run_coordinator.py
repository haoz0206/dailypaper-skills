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
        self.shared_config = self.vault / ".dailypaper" / "config.json"
        self.shared_config.parent.mkdir()
        self.shared_config.write_text("{}\n", encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {"DAILYPAPER_RUN_ROOT": str(self.runs)},
            clear=False,
        )
        self.environment.start()
        self.runtime_context = {
            "paths": {"vault": str(self.vault)},
            "runtime": {"timezone": "Asia/Shanghai"},
            "repository": {"remote": "origin", "branch": "main"},
            "configuration_fingerprint": FINGERPRINT,
        }
        self.common_patches = [
            patch.object(
                run_coordinator,
                "_validated_runtime",
                return_value=self.runtime_context,
            ),
            patch.object(
                run_coordinator.runtime_context,
                "resolve_vault_path",
                return_value=self.vault,
            ),
            patch.object(
                run_coordinator.runtime_context,
                "resolve_shared_config_path",
                return_value=self.shared_config,
            ),
            patch.object(
                run_coordinator.vault_coordination,
                "bootstrap_vault",
                return_value={
                    "status": "already-bootstrapped",
                    "vault": str(self.vault),
                    "branch": "main",
                },
            ),
            patch.object(run_coordinator, "_dirty_paths", return_value=set()),
            patch.object(run_coordinator, "_spawn_guardian"),
            patch.object(run_coordinator, "_stop_guardian"),
        ]
        self.common_mocks = [item.start() for item in self.common_patches]
        self.bootstrap_mock = self.common_mocks[3]

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
        window_days: int = 1,
    ) -> Path:
        manifest = self.runs / run_id / "manifest.json"
        lifecycle = RunLifecycle.create(
            manifest,
            run_id=run_id,
            target_date=TARGET_DATE,
            window_days=window_days,
            timezone="Asia/Shanghai",
            vault=self.vault,
            contract=run_coordinator.WORKFLOW_CONTRACT,
            configuration_fingerprint=FINGERPRINT,
        )
        lifecycle.advance("fetching")
        if interrupted:
            lifecycle.interrupt(Interruption("network unavailable"))
        return manifest

    def _advance_to_reviewing(self, manifest: Path) -> RunLifecycle:
        lifecycle = run_coordinator._open_lifecycle(manifest)
        lifecycle.checkpoint(artifacts=self._fetch_artifacts(manifest))
        lifecycle.advance("reviewing")
        return lifecycle

    def _fetch_artifacts(self, manifest: Path):
        artifacts = []
        filenames = {
            "acquisition": "acquired-papers.json",
            "acquisition-summary": "acquisition-summary.json",
            "candidate-index": "candidate-index.json",
            "approval-summary": "approval-summary.json",
            "candidates": "candidates.json",
            "enriched": "enriched.json",
        }
        for role, filename in filenames.items():
            path = manifest.parent / filename
            path.write_text(
                "{}\n"
                if "summary" in role or role == "candidate-index"
                else "[]\n",
                encoding="utf-8",
            )
            artifacts.append(run_coordinator.ArtifactCandidate(role, path))
        return artifacts

    def _review_report(self, manifest: Path) -> tuple[Path, set[str]]:
        recommendation = self.vault / "DailyPapers" / "today.md"
        recommendation.parent.mkdir()
        recommendation.write_text("# Daily\n", encoding="utf-8")
        history = self.vault / "DailyPapers" / ".history.json"
        history.write_text("[]\n", encoding="utf-8")
        report = manifest.parent / "review-result.json"
        report.write_text(
            json.dumps(
                {
                    "version": 1,
                    "stage": "review",
                    "result": "success",
                    "artifacts": [
                        {
                            "role": "recommendation",
                            "scope": "vault",
                            "path": "DailyPapers/today.md",
                        },
                        {
                            "role": "history",
                            "scope": "vault",
                            "path": "DailyPapers/.history.json",
                        },
                    ],
                    "changed_paths": [
                        "DailyPapers/today.md",
                        "DailyPapers/.history.json",
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return report, {
            "DailyPapers/today.md",
            "DailyPapers/.history.json",
        }

    def test_pending_report_recovery_scan_is_bounded_before_parsing(self) -> None:
        run_dir = self.root / "pending-run"
        run_dir.mkdir()
        for suffix in ("a", "b"):
            (run_dir / f"fetch-progress-{suffix}.json").write_text(
                "{}",
                encoding="utf-8",
            )

        class Snapshot:
            phase = "fetching"

            def as_dict(inner_self):
                return {
                    "paths": {
                        "run_dir": str(run_dir),
                        "vault": str(self.vault),
                    }
                }

        with (
            patch.object(run_coordinator, "MAX_PENDING_STAGE_REPORTS", 1),
            patch.object(
                run_coordinator.stage_report,
                "load_stage_report",
            ) as loader,
            self.assertRaisesRegex(
                run_coordinator.CoordinatorError,
                "report recovery safety limit",
            ),
        ):
            run_coordinator._discover_pending_dirty_paths(Snapshot())
        loader.assert_not_called()

    @staticmethod
    def _running_state(run_id: str, *, window_days: int = 1) -> dict:
        return {
            "status": "running",
            "run_id": run_id,
            "target_date": TARGET_DATE,
            "window_days": window_days,
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
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
                window_days=7,
            )

        self.assertEqual(result["decision"], "ready")
        self.assertEqual(result["mode"], "started")
        self.assertEqual(result["phase"], "fetching")
        self.assertEqual(result["runtime_context"], self.runtime_context)
        context_file = Path(result["runtime_context_file"])
        self.assertEqual(
            json.loads(context_file.read_text(encoding="utf-8")),
            self.runtime_context,
        )
        self.assertEqual(
            result["vault_preparation"]["status"],
            "already-bootstrapped",
        )
        self.bootstrap_mock.assert_called_once_with(self.vault)
        manifest = Path(result["manifest"])
        self.assertTrue(manifest.exists())
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["workflow_contract"]["version"], 3)
        self.assertEqual(data["window_days"], 7)
        self.assertEqual(result["window_days"], 7)

    def test_start_rejects_out_of_range_window_before_remote_inspection(self) -> None:
        with patch.object(run_coordinator, "_prepare_start_runtime") as prepare:
            for value in (0, 32, True):
                with self.subTest(window_days=value):
                    with self.assertRaisesRegex(
                        run_coordinator.CoordinatorError,
                        "1 to 31",
                    ):
                        run_coordinator.start(
                            harness="codex",
                            target_date=TARGET_DATE,
                            window_days=value,
                        )
        prepare.assert_not_called()

    def test_same_day_published_different_window_returns_intent_conflict(
        self,
    ) -> None:
        state = {
            **self._running_state("published-run", window_days=1),
            "status": "success",
            "outputs": {"daily_note": "DailyPapers/today.md"},
        }
        with patch.object(
            run_coordinator,
            "_inspect_task_state",
            return_value=state,
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
                window_days=7,
            )

        self.assertEqual(result["decision"], "intent-conflict")
        self.assertEqual(result["existing_intent"]["window_days"], 1)
        self.assertEqual(result["requested_intent"]["window_days"], 7)

    def test_same_day_published_matching_window_remains_idempotent(self) -> None:
        state = {
            **self._running_state("published-run", window_days=7),
            "status": "success",
            "outputs": {"daily_note": "DailyPapers/today.md"},
        }
        with patch.object(
            run_coordinator,
            "_inspect_task_state",
            return_value=state,
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
                window_days=7,
            )

        self.assertEqual(result["decision"], "already-published")
        self.assertEqual(result["run_id"], "published-run")

    def test_published_local_manifest_must_match_remote_window(self) -> None:
        run_id = f"{TARGET_DATE}-published-mismatch"
        self._manifest_for_remote(run_id, window_days=1)
        state = {
            **self._running_state(run_id, window_days=7),
            "status": "success",
            "outputs": {"daily_note": "DailyPapers/today.md"},
        }
        with patch.object(
            run_coordinator,
            "_inspect_task_state",
            return_value=state,
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
                window_days=7,
            )

        self.assertEqual(result["decision"], "intent-conflict")
        self.assertEqual(result["manifest_intent"]["window_days"], 1)

    def test_same_day_running_different_window_does_not_resume_or_preempt(
        self,
    ) -> None:
        run_id = f"{TARGET_DATE}-one-day"
        self._manifest_for_remote(run_id, window_days=1)
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id, window_days=1),
            ),
            patch.object(run_coordinator, "_guardian_is_alive") as guardian,
            patch.object(run_coordinator, "_prepare_cancel") as prepare_cancel,
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
                window_days=3,
            )

        self.assertEqual(result["decision"], "intent-conflict")
        guardian.assert_not_called()
        prepare_cancel.assert_not_called()

    def test_resume_rejects_remote_manifest_window_mismatch(self) -> None:
        run_id = f"{TARGET_DATE}-mismatched-local"
        self._manifest_for_remote(run_id, window_days=1)
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id, window_days=3),
            ),
            patch.object(run_coordinator, "_guardian_is_alive") as guardian,
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
                window_days=3,
            )

        self.assertEqual(result["decision"], "intent-conflict")
        self.assertEqual(result["manifest_intent"]["window_days"], 1)
        guardian.assert_not_called()

    def test_start_inspects_remote_before_rejecting_invalid_local_runtime(self) -> None:
        with (
            patch.object(
                run_coordinator,
                "_validated_runtime",
                side_effect=run_coordinator.CoordinatorError(
                    "invalid-runtime-context",
                    "unsafe shared configuration",
                ),
            ),
            patch.object(run_coordinator, "_inspect_task_state") as inspect,
        ):
            with self.assertRaisesRegex(
                run_coordinator.CoordinatorError,
                "unsafe shared configuration",
            ):
                run_coordinator.start(
                    harness="codex",
                    target_date=TARGET_DATE,
                )

        inspect.assert_called_once_with(self.vault, snapshot=True)

    def test_cross_machine_run_does_not_require_local_shared_config(self) -> None:
        run_id = f"{TARGET_DATE}-other-machine"
        self.shared_config.unlink()
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
                "_validated_runtime",
                side_effect=AssertionError("must not read stale local config"),
            ),
            patch.object(
                run_coordinator,
                "_prepare_cancel",
                return_value=proposal,
            ),
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
            )

        self.assertEqual(result["decision"], "cancel-confirmation-required")
        self.assertEqual(result["proposal"], proposal)

    def test_start_resumes_matching_interrupted_local_run(self) -> None:
        run_id = f"{TARGET_DATE}-resume"
        manifest = self._manifest_for_remote(
            run_id,
            interrupted=True,
            window_days=3,
        )
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id, window_days=3),
            ),
            patch.object(run_coordinator, "_guardian_is_alive", return_value=False),
        ):
            result = run_coordinator.start(
                harness="claude-code",
                target_date=TARGET_DATE,
                window_days=3,
            )

        self.assertEqual(result["decision"], "ready")
        self.assertEqual(result["mode"], "resumed")
        self.assertEqual(result["phase"], "fetching")
        self.assertEqual(result["condition"], "active")
        self.assertEqual(result["window_days"], 3)
        self.assertEqual(Path(result["manifest"]), manifest)
        self.assertEqual(
            result["vault_preparation"]["status"],
            "preserved-for-recovery",
        )
        self.bootstrap_mock.assert_not_called()

    def test_start_reconciles_remote_lock_after_prepared_manifest_crash(
        self,
    ) -> None:
        run_id = f"{TARGET_DATE}-prepared"
        manifest = self.runs / run_id / "manifest.json"
        RunLifecycle.create(
            manifest,
            run_id=run_id,
            target_date=TARGET_DATE,
            timezone="Asia/Shanghai",
            vault=self.vault,
            contract=run_coordinator.WORKFLOW_CONTRACT,
            configuration_fingerprint=FINGERPRINT,
        )
        state = {
            **self._running_state(run_id),
            "config_sha256": FINGERPRINT,
        }
        inspected = {
            "status": "inspected",
            "vault": str(self.vault),
            "remote": "origin",
            "branch": "main",
            "remote_head": "lock-sha",
            "task_state": state,
        }
        acquisition = {
            "status": "acquired",
            "run_id": run_id,
            "lock_commit": "lock-sha",
            "remote": "origin",
            "branch": "main",
            "resumed": True,
        }
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=inspected,
            ),
            patch.object(run_coordinator, "_guardian_is_alive", return_value=False),
            patch.object(
                run_coordinator.vault_coordination,
                "acquire",
                return_value=acquisition,
            ) as acquire,
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
            )

        self.assertEqual(result["decision"], "ready")
        self.assertEqual(result["phase"], "fetching")
        acquire.assert_called_once_with(
            manifest,
            harness="codex",
            expected_remote_head="lock-sha",
            runtime_context=self.runtime_context,
            record_manifest=False,
        )
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            data["publication"]["acquisition_commit"],
            "lock-sha",
        )

    def test_start_bootstraps_missing_shared_config_before_runtime(self) -> None:
        self.shared_config.unlink()
        with (
            patch.object(run_coordinator, "_inspect_task_state", return_value=None),
            patch.object(
                run_coordinator.vault_coordination,
                "acquire",
                return_value={"status": "acquired", "lock_commit": "lock-sha"},
            ),
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
            )

        self.assertEqual(result["decision"], "ready")
        self.bootstrap_mock.assert_called_once_with(self.vault)

    def test_start_reinspects_after_bootstrap_and_observes_remote_race(self) -> None:
        run_id = f"{TARGET_DATE}-raced"
        proposal = {
            "version": 1,
            "run_id": run_id,
            "vault": str(self.vault),
        }
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                side_effect=[None, self._running_state(run_id)],
            ) as inspect,
            patch.object(
                run_coordinator,
                "_prepare_cancel",
                return_value=proposal,
            ),
            patch.object(run_coordinator.vault_coordination, "acquire") as acquire,
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
            )

        self.assertEqual(result["decision"], "cancel-confirmation-required")
        self.assertEqual(inspect.call_count, 2)
        acquire.assert_not_called()

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
        self.assertEqual(result["confirmation_run_id"], run_id)

    def test_start_resumes_live_guardian_only_after_exact_confirmation(self) -> None:
        run_id = f"{TARGET_DATE}-confirmed-live"
        manifest = self._manifest_for_remote(run_id, interrupted=True)
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id),
            ),
            patch.object(run_coordinator, "_guardian_is_alive", return_value=True),
        ):
            rejected = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
                confirm_running_run_id="different-run",
            )

            resumed = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
                confirm_running_run_id=run_id,
            )

        self.assertEqual(rejected["decision"], "blocked")
        self.assertEqual(rejected["code"], "confirmation-required")
        self.assertIn("exact run ID", rejected["message"])
        self.assertEqual(resumed["decision"], "ready")
        self.assertEqual(resumed["mode"], "resumed")
        self.assertEqual(resumed["run_id"], run_id)
        self.assertEqual(Path(resumed["manifest"]), manifest)
        self.common_mocks[6].assert_called_once_with(manifest.parent)

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

    def test_start_requires_cas_cancel_when_run_dir_exists_without_manifest(
        self,
    ) -> None:
        run_id = f"{TARGET_DATE}-partial-local"
        run_dir = self.runs / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "preserved.json").write_text("{}\n", encoding="utf-8")
        proposal = {
            "version": 1,
            "operation": "cancel-dailypaper-run",
            "expected_run_id": run_id,
            "remote_head": "abc",
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
            patch.object(
                run_coordinator,
                "_validated_runtime",
                side_effect=AssertionError(
                    "missing Manifest cancellation must not trust local config"
                ),
            ),
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
            )

        self.assertEqual(result["decision"], "cancel-confirmation-required")
        self.assertEqual(result["run"]["run_id"], run_id)
        self.assertEqual(result["proposal"], proposal)
        self.assertTrue((run_dir / "preserved.json").exists())
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

            first = run_coordinator.submit(
                manifest,
                result="success",
                artifacts=self._fetch_artifacts(manifest),
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

    def test_start_allows_artifact_backed_pending_report_after_guardian_crash(
        self,
    ) -> None:
        run_id = f"{TARGET_DATE}-pending-start"
        manifest = self._manifest_for_remote(run_id)
        self._advance_to_reviewing(manifest)
        _report, dirty = self._review_report(manifest)
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id),
            ),
            patch.object(run_coordinator, "_guardian_is_alive", return_value=False),
            patch.object(run_coordinator, "_dirty_paths", return_value=dirty),
        ):
            result = run_coordinator.start(
                harness="codex",
                target_date=TARGET_DATE,
            )

        self.assertEqual(result["decision"], "ready")
        self.assertEqual(result["mode"], "resumed")
        self.assertEqual(result["phase"], "reviewing")

    def test_submit_checkpoints_pending_report_after_guardian_crash(self) -> None:
        run_id = f"{TARGET_DATE}-pending-submit"
        manifest = self._manifest_for_remote(run_id)
        self._advance_to_reviewing(manifest)
        report, dirty = self._review_report(manifest)
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id),
            ),
            patch.object(run_coordinator, "_guardian_is_alive", return_value=False),
            patch.object(run_coordinator, "_dirty_paths", return_value=dirty),
        ):
            result = run_coordinator.submit(manifest, report=report)

        self.assertEqual(result["phase"], "writing-notes")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(set(data["run_change_set"]), dirty)

    def test_pending_report_does_not_allow_unrelated_dirty_paths(self) -> None:
        run_id = f"{TARGET_DATE}-pending-unrelated"
        manifest = self._manifest_for_remote(run_id)
        self._advance_to_reviewing(manifest)
        report, dirty = self._review_report(manifest)
        unrelated = self.vault / "manual-note.md"
        unrelated.write_text("user edit\n", encoding="utf-8")
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id),
            ),
            patch.object(run_coordinator, "_guardian_is_alive", return_value=False),
            patch.object(
                run_coordinator,
                "_dirty_paths",
                return_value={*dirty, "manual-note.md"},
            ),
        ):
            with self.assertRaisesRegex(
                run_coordinator.CoordinatorError,
                "manual-note.md",
            ):
                run_coordinator.submit(manifest, report=report)

    def test_reported_interruption_checkpoints_safe_evidence(self) -> None:
        run_id = f"{TARGET_DATE}-reported-interrupt"
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
            report = manifest.parent / "fetch-result.json"
            report.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "stage": "fetch",
                        "result": "recoverable",
                        "message": "arXiv rate limited",
                        "artifacts": [
                            {
                                "role": "partial",
                                "scope": "run",
                                "path": "partial.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = run_coordinator.submit(manifest, report=report)

        self.assertEqual(result["condition"], "interrupted")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        roles = {artifact["role"] for artifact in data["artifacts"].values()}
        self.assertEqual(roles, {"fetch-report", "partial"})
        self.assertIn("fetching", data["checkpoints"])

    def test_deterministic_failure_checkpoints_report_evidence(self) -> None:
        run_id = f"{TARGET_DATE}-deterministic-failure"
        manifest = self._manifest_for_remote(run_id)
        partial = manifest.parent / "partial.json"
        partial.write_text("{}\n", encoding="utf-8")
        report = manifest.parent / "fetch-result.json"
        report.write_text(
            json.dumps(
                {
                    "version": 1,
                    "stage": "fetch",
                    "result": "deterministic-failure",
                    "message": "candidate schema is invalid",
                    "artifacts": [
                        {
                            "role": "partial",
                            "scope": "run",
                            "path": "partial.json",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with (
            patch.object(
                run_coordinator,
                "_inspect_task_state",
                return_value=self._running_state(run_id),
            ),
            patch.object(run_coordinator, "_guardian_is_alive", return_value=True),
            patch.object(
                run_coordinator.vault_coordination,
                "fail",
                return_value={"status": "failed", "failure_commit": "fail-sha"},
            ),
        ):
            result = run_coordinator.submit(manifest, report=report)

        self.assertEqual(result["decision"], "failed")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        roles = {artifact["role"] for artifact in data["artifacts"].values()}
        self.assertEqual(roles, {"fetch-report", "partial"})

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

    def test_submit_accepts_scoped_stage_report_and_registers_it(self) -> None:
        run_id = f"{TARGET_DATE}-report"
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
            fetch_artifacts = self._fetch_artifacts(manifest)
            report = manifest.parent / "fetch-result.json"
            report.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "stage": "fetch",
                        "result": "success",
                        "artifacts": [
                            {
                                "role": artifact.role,
                                "scope": "run",
                                "path": artifact.path.name,
                            }
                            for artifact in fetch_artifacts
                        ],
                        "changed_paths": [],
                        "metadata": {"counts": {"candidates": 0}},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = run_coordinator.submit(manifest, report=report)

        self.assertEqual(result["phase"], "reviewing")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        roles = {artifact["role"] for artifact in data["artifacts"].values()}
        self.assertEqual(
            roles,
            {
                "fetch-report",
                "acquisition",
                "acquisition-summary",
                "candidate-index",
                "approval-summary",
                "candidates",
                "enriched",
            },
        )

    def test_submit_rejects_wrong_stage_and_escaping_report_paths(self) -> None:
        run_id = f"{TARGET_DATE}-bad-report"
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
            report = manifest.parent / "bad-result.json"
            report.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "stage": "review",
                        "result": "success",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                run_coordinator.stage_report.StageReportError,
                "does not match Run phase",
            ):
                run_coordinator.submit(manifest, report=report)

            report.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "stage": "fetch",
                        "result": "success",
                        "artifacts": [
                            {
                                "role": "candidates",
                                "scope": "run",
                                "path": "../candidates.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                run_coordinator.stage_report.StageReportError,
                "contain '..'",
            ):
                run_coordinator.submit(manifest, report=report)

            report.write_text(
                '{"version":1,"stage":"fetch","result":"success",'
                '"result":"progress"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                run_coordinator.stage_report.StageReportError,
                "duplicate JSON key",
            ):
                run_coordinator.submit(manifest, report=report)

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
