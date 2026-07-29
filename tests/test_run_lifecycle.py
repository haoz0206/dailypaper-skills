from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


SHARED = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "daily-papers"
    / "scripts"
    / "shared"
)
sys.path.insert(0, str(SHARED))

import run_lifecycle  # noqa: E402


class RunLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.run_dir = self.vault / ".dailypaper" / "runs" / "run-1"
        self.vault.mkdir()
        self.contract = run_lifecycle.WorkflowContract(
            name="daily-papers",
            version=2,
            phases=(
                "prepared",
                "fetching",
                "reviewing",
                "writing-notes",
                "validated",
                "publishing",
            ),
            required_artifact_roles_by_phase={
                "fetching": ("candidates",),
            },
        )
        self.fingerprint = hashlib.sha256(b"config").hexdigest()
        self.manifest = self.run_dir / "manifest.json"
        self.lifecycle = run_lifecycle.RunLifecycle.create(
            self.manifest,
            run_id="2026-07-28-run-1",
            target_date="2026-07-28",
            window_days=7,
            timezone="Asia/Shanghai",
            vault=self.vault,
            contract=self.contract,
            configuration_fingerprint=self.fingerprint,
        )

    def test_manifest_freezes_window_and_validates_bounds(self) -> None:
        self.assertEqual(self.lifecycle.snapshot().window_days, 7)
        self.assertEqual(
            json.loads(self.manifest.read_text(encoding="utf-8"))["window_days"],
            7,
        )
        for value in (0, 32, True):
            with self.subTest(window_days=value):
                with self.assertRaisesRegex(ValueError, "1 to 31"):
                    run_lifecycle.RunLifecycle.create(
                        self.root / f"invalid-{value}" / "manifest.json",
                        run_id=f"invalid-{value}",
                        target_date="2026-07-28",
                        window_days=value,
                        timezone="Asia/Shanghai",
                        vault=self.vault,
                        contract=self.contract,
                        configuration_fingerprint=self.fingerprint,
                    )

    def test_open_normalizes_pre_release_manifest_to_one_day(self) -> None:
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        data.pop("window_days")
        self.manifest.write_text(
            json.dumps(data, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        opened = run_lifecycle.RunLifecycle.open(
            self.manifest,
            contract=self.contract,
            configuration_fingerprint=self.fingerprint,
            expected_vault=self.vault,
            expected_run_id="2026-07-28-run-1",
        )

        self.assertEqual(opened.snapshot().window_days, 1)

    def _complete_current_phase(self, name: str) -> None:
        artifact = self.run_dir / f"{name}.json"
        artifact.write_text(json.dumps({"phase": name}), encoding="utf-8")
        role = "candidates" if name == "fetching" else name
        self.lifecycle.checkpoint(
            artifacts=[run_lifecycle.ArtifactCandidate(role, artifact)]
        )

    def test_rejects_phase_jump_and_requires_checkpoint(self) -> None:
        with self.assertRaises(run_lifecycle.InvalidTransition):
            self.lifecycle.advance("reviewing")

        self.lifecycle.advance("fetching")
        with self.assertRaises(run_lifecycle.CheckpointRequired):
            self.lifecycle.checkpoint()
        with self.assertRaises(run_lifecycle.CheckpointRequired):
            self.lifecycle.advance("reviewing")

        self._complete_current_phase("fetching")
        snapshot = self.lifecycle.advance("reviewing")
        self.assertEqual(snapshot.phase, "reviewing")

    def test_same_phase_resume_is_idempotent_and_tracks_recovery(self) -> None:
        self.lifecycle.advance("fetching")
        interrupted = self.lifecycle.interrupt(
            run_lifecycle.Interruption(
                "arXiv timeout",
                retry_at="2026-07-28T10:05:00+08:00",
            )
        )
        self.assertEqual(interrupted.condition, "interrupted")
        revision = interrupted.revision

        with self.assertRaises(run_lifecycle.InvalidTransition):
            self.lifecycle.advance("fetching")
        resumed = self.lifecycle.resume(observed_dirty_paths=[])
        self.assertEqual(resumed.condition, "active")
        self.assertEqual(
            resumed.as_dict()["recovery"]["attempts_by_phase"]["fetching"],
            1,
        )
        self.assertGreater(resumed.revision, revision)

        again = self.lifecycle.advance("fetching")
        self.assertEqual(again.revision, resumed.revision)

    def test_resume_rejects_artifact_hash_conflict_and_unowned_dirty_path(self) -> None:
        self.lifecycle.advance("fetching")
        artifact = self.run_dir / "candidates.json"
        artifact.write_text('{"complete": true}', encoding="utf-8")
        note = self.vault / "DailyPapers" / "2026-07-28-论文推荐.md"
        note.parent.mkdir()
        note.write_text("generated", encoding="utf-8")
        self.lifecycle.checkpoint(
            artifacts=[
                run_lifecycle.ArtifactCandidate("candidates", artifact),
                run_lifecycle.ArtifactCandidate("daily-note", note),
            ],
            changed_paths=[note],
        )
        self.lifecycle.interrupt(run_lifecycle.Interruption("process exited"))

        with self.assertRaises(run_lifecycle.UnexpectedDirtyPaths):
            self.lifecycle.resume(observed_dirty_paths=["私人笔记/想法.md"])

        artifact.write_text('{"complete": false}', encoding="utf-8")
        with self.assertRaises(run_lifecycle.ArtifactConflict):
            self.lifecycle.resume(
                observed_dirty_paths=["DailyPapers/2026-07-28-论文推荐.md"]
            )

        self.assertEqual(self.lifecycle.snapshot().condition, "interrupted")

    def test_checkpoint_and_resume_reject_symlinked_artifacts(self) -> None:
        self.lifecycle.advance("fetching")
        target = self.run_dir / "target.json"
        target.write_text('{"complete": true}', encoding="utf-8")
        linked = self.run_dir / "linked.json"
        linked.symlink_to(target)

        with self.assertRaises(run_lifecycle.ArtifactConflict):
            self.lifecycle.checkpoint(
                artifacts=[
                    run_lifecycle.ArtifactCandidate("candidates", linked),
                ]
            )

        artifact = self.run_dir / "candidates.json"
        artifact.write_bytes(target.read_bytes())
        self.lifecycle.checkpoint(
            artifacts=[
                run_lifecycle.ArtifactCandidate("candidates", artifact),
            ]
        )
        artifact.unlink()
        artifact.symlink_to(target)

        with self.assertRaises(run_lifecycle.ArtifactConflict):
            self.lifecycle.advance("reviewing")

    def test_checkpoint_bounds_artifact_hashing(self) -> None:
        self.lifecycle.advance("fetching")
        artifact = self.run_dir / "candidates.json"
        artifact.write_bytes(b"oversized")

        with (
            patch.object(run_lifecycle, "MAX_ARTIFACT_BYTES", 4),
            self.assertRaisesRegex(
                run_lifecycle.ArtifactConflict,
                "safety limit",
            ),
        ):
            self.lifecycle.checkpoint(
                artifacts=[
                    run_lifecycle.ArtifactCandidate("candidates", artifact),
                ]
            )

    def test_terminal_outcomes_are_immutable(self) -> None:
        cancelled = self.lifecycle.finish("cancelled", reason="confirmed by user")
        self.assertIsNone(cancelled.condition)
        self.assertEqual(cancelled.outcome, "cancelled")

        mutations = (
            lambda: self.lifecycle.advance("prepared"),
            lambda: self.lifecycle.interrupt(
                run_lifecycle.Interruption("too late")
            ),
            lambda: self.lifecycle.checkpoint(),
            lambda: self.lifecycle.finish("failed", reason="too late"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                with self.assertRaises(run_lifecycle.TerminalRun):
                    mutate()

    def test_published_requires_final_phase_and_commit(self) -> None:
        with self.assertRaises(run_lifecycle.InvalidTransition):
            self.lifecycle.finish("published", content_commit="abc")

        for phase in self.contract.phases[1:]:
            current = self.lifecycle.snapshot().phase
            if current != "prepared":
                self._complete_current_phase(current)
            self.lifecycle.advance(phase)

        with self.assertRaises(ValueError):
            self.lifecycle.finish("published")
        published = self.lifecycle.finish(
            "published",
            content_commit="0123456789abcdef",
        )
        self.assertEqual(published.outcome, "published")
        self.assertEqual(
            published.as_dict()["publication"]["content_commit"],
            "0123456789abcdef",
        )

    def test_acquisition_and_content_commit_are_idempotent_and_immutable(self) -> None:
        acquired = self.lifecycle.record_acquisition(
            acquisition_commit="acquisition-commit",
            remote="origin",
            branch="main",
        )
        repeated = self.lifecycle.record_acquisition(
            acquisition_commit="acquisition-commit",
            remote="origin",
            branch="main",
        )
        self.assertEqual(repeated.revision, acquired.revision)
        with self.assertRaises(run_lifecycle.InvalidTransition):
            self.lifecycle.record_acquisition(
                acquisition_commit="other-commit",
                remote="origin",
                branch="main",
            )

        for phase in self.contract.phases[1:]:
            current = self.lifecycle.snapshot().phase
            if current != "prepared":
                self._complete_current_phase(current)
            self.lifecycle.advance(phase)

        recorded = self.lifecycle.record_content_commit("content-commit")
        repeated_content = self.lifecycle.record_content_commit("content-commit")
        self.assertEqual(repeated_content.revision, recorded.revision)
        with self.assertRaises(run_lifecycle.InvalidTransition):
            self.lifecycle.record_content_commit("different-content-commit")
        with self.assertRaises(run_lifecycle.InvalidTransition):
            self.lifecycle.finish(
                "published",
                content_commit="different-content-commit",
            )

    def test_open_recovers_previous_atomic_snapshot(self) -> None:
        self.lifecycle.advance("fetching")
        expected_previous = self.lifecycle.snapshot()
        self._complete_current_phase("fetching")
        self.assertTrue(self.manifest.with_name("manifest.prev.json").exists())
        self.manifest.write_text("{broken", encoding="utf-8")

        recovered = run_lifecycle.RunLifecycle.open(
            self.manifest,
            contract=self.contract,
            configuration_fingerprint=self.fingerprint,
            expected_vault=self.vault,
            expected_run_id="2026-07-28-run-1",
        )
        self.assertTrue(recovered.recovered_from_previous)
        self.assertEqual(recovered.snapshot().revision, expected_previous.revision)
        self.assertEqual(recovered.snapshot().phase, "fetching")

    def test_open_does_not_follow_symlinked_current_snapshot(self) -> None:
        self.lifecycle.advance("fetching")
        expected_previous = json.loads(
            self.manifest.with_name("manifest.prev.json").read_text(
                encoding="utf-8"
            )
        )
        outside = self.root / "outside-manifest.json"
        outside.write_bytes(self.manifest.read_bytes())
        self.manifest.unlink()
        self.manifest.symlink_to(outside)

        recovered = run_lifecycle.RunLifecycle.open(
            self.manifest,
            contract=self.contract,
            configuration_fingerprint=self.fingerprint,
            expected_vault=self.vault,
            expected_run_id="2026-07-28-run-1",
        )

        self.assertTrue(recovered.recovered_from_previous)
        self.assertFalse(self.manifest.is_symlink())
        self.assertEqual(recovered.snapshot().revision, expected_previous["revision"])

    def test_open_rejects_duplicate_and_oversize_manifest_documents(self) -> None:
        previous = self.manifest.with_name("manifest.prev.json")
        previous.unlink(missing_ok=True)
        duplicate = self.manifest.read_text(encoding="utf-8").replace(
            '"version": 2,',
            '"version": 2,\n  "version": 2,',
            1,
        )
        self.manifest.write_text(duplicate, encoding="utf-8")
        with self.assertRaisesRegex(
            run_lifecycle.ManifestCorrupt,
            "duplicate JSON key",
        ):
            run_lifecycle.RunLifecycle.open(
                self.manifest,
                contract=self.contract,
                configuration_fingerprint=self.fingerprint,
                expected_vault=self.vault,
                expected_run_id="2026-07-28-run-1",
            )

        self.manifest.write_bytes(
            b"{" + b" " * run_lifecycle.MAX_MANIFEST_BYTES + b"}"
        )
        with self.assertRaisesRegex(run_lifecycle.ManifestCorrupt, "safety limit"):
            run_lifecycle.RunLifecycle.open(
                self.manifest,
                contract=self.contract,
                configuration_fingerprint=self.fingerprint,
                expected_vault=self.vault,
                expected_run_id="2026-07-28-run-1",
            )

    def test_open_rejects_non_portable_manifest_paths(self) -> None:
        base = self.lifecycle.snapshot().as_dict()
        unsafe_paths = (
            "DailyPapers\\today.md",
            "DailyPapers/\nsecret.md",
            "DailyPapers/\x7fsecret.md",
            "x" * 4097,
        )
        for path in unsafe_paths:
            with self.subTest(path=path[:80]):
                data = copy.deepcopy(base)
                data["change_set"] = [path]
                self.manifest.write_text(json.dumps(data), encoding="utf-8")
                self.manifest.with_name("manifest.prev.json").unlink(missing_ok=True)
                with self.assertRaises(run_lifecycle.ManifestCorrupt):
                    run_lifecycle.RunLifecycle.open(
                        self.manifest,
                        contract=self.contract,
                        configuration_fingerprint=self.fingerprint,
                        expected_vault=self.vault,
                        expected_run_id="2026-07-28-run-1",
                    )

    def test_manifest_lock_must_not_be_a_symlink(self) -> None:
        lock = self.run_dir / run_lifecycle.MANIFEST_LOCK_NAME
        lock.unlink()
        outside = self.root / "outside.lock"
        outside.touch()
        lock.symlink_to(outside)

        with self.assertRaisesRegex(
            run_lifecycle.LifecycleError,
            "cannot be opened safely",
        ):
            self.lifecycle.interrupt(run_lifecycle.Interruption("test"))

    def test_attention_required_and_schema_validation(self) -> None:
        attention = self.lifecycle.interrupt(
            run_lifecycle.Interruption(
                "retry budget exhausted",
                attention_required=True,
            )
        )
        self.assertEqual(attention.condition, "attention-required")
        self.assertEqual(
            attention.as_dict()["recovery"]["last_error"]["attempt"],
            1,
        )
        with self.assertRaises(run_lifecycle.InvalidTransition):
            self.lifecycle.advance("prepared")
        with self.assertRaises(run_lifecycle.InvalidTransition):
            self.lifecycle.resume(observed_dirty_paths=[])
        resumed = self.lifecycle.resume(
            observed_dirty_paths=[],
            require_user_confirmation=True,
        )
        self.assertEqual(resumed.condition, "active")

        data = resumed.as_dict()
        data["condition"] = "unknown"
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        self.manifest.with_name("manifest.prev.json").unlink()
        with self.assertRaises(run_lifecycle.ManifestCorrupt):
            run_lifecycle.RunLifecycle.open(
                self.manifest,
                contract=self.contract,
                configuration_fingerprint=self.fingerprint,
                expected_vault=self.vault,
                expected_run_id="2026-07-28-run-1",
            )

    def test_open_rejects_contract_and_configuration_drift(self) -> None:
        changed_contract = run_lifecycle.WorkflowContract(
            name="daily-papers",
            version=3,
            phases=self.contract.phases,
        )
        with self.assertRaises(run_lifecycle.ContractMismatch):
            run_lifecycle.RunLifecycle.open(
                self.manifest,
                contract=changed_contract,
                configuration_fingerprint=self.fingerprint,
                expected_vault=self.vault,
                expected_run_id="2026-07-28-run-1",
            )
        with self.assertRaises(run_lifecycle.ConfigurationMismatch):
            run_lifecycle.RunLifecycle.open(
                self.manifest,
                contract=self.contract,
                configuration_fingerprint=hashlib.sha256(b"changed").hexdigest(),
                expected_vault=self.vault,
                expected_run_id="2026-07-28-run-1",
            )

    def test_forward_advance_rechecks_artifact_hash(self) -> None:
        self.lifecycle.advance("fetching")
        artifact = self.run_dir / "candidates.json"
        artifact.write_text("original", encoding="utf-8")
        self.lifecycle.checkpoint(
            artifacts=[run_lifecycle.ArtifactCandidate("candidates", artifact)]
        )
        artifact.write_text("user edit", encoding="utf-8")
        with self.assertRaises(run_lifecycle.ArtifactConflict):
            self.lifecycle.advance("reviewing")
        self.assertEqual(self.lifecycle.snapshot().phase, "fetching")

    def test_open_rejects_cross_run_previous_snapshot_and_path_tampering(self) -> None:
        original = self.lifecycle.snapshot().as_dict()
        foreign = dict(original)
        foreign["run_id"] = "2026-07-28-other"
        self.manifest.write_text("{broken", encoding="utf-8")
        self.manifest.with_name("manifest.prev.json").write_text(
            json.dumps(foreign),
            encoding="utf-8",
        )
        with self.assertRaises(run_lifecycle.ManifestCorrupt):
            run_lifecycle.RunLifecycle.open(
                self.manifest,
                contract=self.contract,
                configuration_fingerprint=self.fingerprint,
                expected_vault=self.vault,
                expected_run_id="2026-07-28-run-1",
            )

        tampered = dict(original)
        tampered["paths"] = dict(original["paths"])
        tampered["paths"]["vault"] = "/"
        self.manifest.write_text(json.dumps(tampered), encoding="utf-8")
        self.manifest.with_name("manifest.prev.json").write_text(
            json.dumps(tampered),
            encoding="utf-8",
        )
        with self.assertRaises(run_lifecycle.ManifestCorrupt):
            run_lifecycle.RunLifecycle.open(
                self.manifest,
                contract=self.contract,
                configuration_fingerprint=self.fingerprint,
                expected_vault=self.vault,
                expected_run_id="2026-07-28-run-1",
            )

    def test_manifest_lock_prevents_lost_concurrent_updates(self) -> None:
        other = run_lifecycle.RunLifecycle.open(
            self.manifest,
            contract=self.contract,
            configuration_fingerprint=self.fingerprint,
            expected_vault=self.vault,
            expected_run_id="2026-07-28-run-1",
        )
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def update(lifecycle: run_lifecycle.RunLifecycle, message: str) -> None:
            barrier.wait()
            try:
                lifecycle.interrupt(run_lifecycle.Interruption(message))
            except Exception as exc:  # exactly one stale revision may lose the CAS
                errors.append(exc)

        threads = [
            threading.Thread(target=update, args=(self.lifecycle, "one")),
            threading.Thread(target=update, args=(other, "two")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        snapshot = self.lifecycle.snapshot()
        self.assertGreaterEqual(snapshot.revision, 1)
        self.assertTrue(
            not errors
            or all(isinstance(exc, run_lifecycle.LifecycleError) for exc in errors)
        )
        self.assertTrue(self.manifest.with_name("manifest.prev.json").exists())

    def test_canonical_contract_has_required_artifacts_and_fingerprint(self) -> None:
        contract = run_lifecycle.DAILY_WORKFLOW_CONTRACT.as_dict()
        self.assertEqual(contract["version"], 2)
        self.assertIn("sha256", contract)
        self.assertEqual(
            contract["required_artifact_roles_by_phase"]["fetching"],
            ["candidates", "enriched"],
        )


if __name__ == "__main__":
    unittest.main()
