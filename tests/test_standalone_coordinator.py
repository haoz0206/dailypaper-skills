import copy
import hashlib
import json
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

import run_guardian
import standalone_coordinator


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class StandaloneCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.vault = self.root / "vault"
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
        (self.seed / ".gitignore").write_text(
            ".dailypaper/runs/\n",
            encoding="utf-8",
        )
        (self.seed / "README.md").write_text("# Vault\n", encoding="utf-8")
        git(self.seed, "add", ".gitignore", "README.md")
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
        self.base_head = git(self.vault, "rev-parse", "HEAD")
        self.context = {
            "version": 1,
            "status": "ready",
            "paths": {
                "vault": str(self.vault),
                "paper_notes": str(self.vault / "论文笔记"),
                "concepts": str(self.vault / "论文笔记" / "_概念"),
                "inbox": str(self.vault / "论文笔记" / "_待整理"),
                "daily_papers": str(self.vault / "DailyPapers"),
            },
            "repository": {
                "url": str(self.remote),
                "remote": "origin",
                "branch": "main",
                "task_state_file": ".dailypaper/tasks/daily-papers.json",
            },
            "automation": {
                "auto_refresh_indexes": True,
                "git_commit": False,
                "git_push": False,
            },
            "preparation": {
                "prepared": True,
                "remote_head": self.base_head,
                "local_head": self.base_head,
                "dirty": False,
                "pulled": False,
            },
            "configuration_fingerprint": "a" * 64,
        }
        self.vault_patch = patch.object(
            standalone_coordinator.runtime_context,
            "resolve_vault_path",
            return_value=self.vault,
        )
        self.context_patch = patch.object(
            standalone_coordinator.runtime_context,
            "resolve_runtime_context",
            side_effect=lambda **_kwargs: copy.deepcopy(self.context),
        )
        self.vault_patch.start()
        self.context_patch.start()

    def tearDown(self) -> None:
        runs = self.vault / ".dailypaper" / "runs"
        if runs.is_dir():
            for run_dir in runs.glob("standalone-*"):
                try:
                    run_guardian.stop_guardian(run_dir)
                except run_guardian.GuardianError:
                    pass
        self.context_patch.stop()
        self.vault_patch.stop()
        self.temporary.cleanup()

    def _start(
        self,
        intent: str = "arxiv:2607.00001",
        *,
        confirm_running_session_id: str | None = None,
    ) -> dict:
        return standalone_coordinator.start(
            operation="paper-reader",
            harness="codex",
            intent=intent,
            confirm_running_session_id=confirm_running_session_id,
        )

    def test_active_session_scan_and_artifact_hashing_are_bounded(self) -> None:
        runs = self.vault / ".dailypaper" / "runs"
        for suffix in ("a", "b"):
            session = runs / f"standalone-paper-reader-{suffix}" / "standalone-session.json"
            session.parent.mkdir(parents=True)
            session.write_text("{}", encoding="utf-8")

        with (
            patch.object(
                standalone_coordinator,
                "MAX_STANDALONE_SESSION_FILES",
                1,
            ),
            patch.object(standalone_coordinator, "_load_manifest") as loader,
            self.assertRaisesRegex(
                standalone_coordinator.StandaloneError,
                "session safety limit",
            ),
        ):
            standalone_coordinator._active_manifests(self.vault)
        loader.assert_not_called()

        artifact = self.vault / "oversized.bin"
        artifact.write_bytes(b"x" * 32)
        with (
            patch.object(standalone_coordinator, "MAX_ARTIFACT_BYTES", 8),
            self.assertRaisesRegex(
                standalone_coordinator.StandaloneError,
                "byte safety limit",
            ),
        ):
            standalone_coordinator._file_sha256(artifact)

    def _write_report(
        self,
        started: dict,
        paths: list[tuple[str, str]],
        *,
        result: str = "success",
    ) -> Path:
        artifacts = []
        for relative, kind in paths:
            digest = hashlib.sha256((self.vault / relative).read_bytes()).hexdigest()
            artifacts.append(
                {"path": relative, "sha256": digest, "kind": kind}
            )
        report = {
            "version": 1,
            "session_id": started["session_id"],
            "operation": "paper-reader",
            "result": result,
            "artifacts": artifacts,
            "changed_paths": [relative for relative, _kind in paths],
            "message": None,
        }
        path = Path(started["manifest"]).parent / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def test_local_completion_tracks_exact_artifacts_and_releases_lock(self) -> None:
        started = self._start()
        note = self.vault / "论文笔记" / "Robotics" / "Method.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Method\n", encoding="utf-8")
        report = self._write_report(
            started,
            [("论文笔记/Robotics/Method.md", "note")],
        )

        result = standalone_coordinator.submit(
            session_id=started["session_id"],
            report_path=report,
        )
        inspected = standalone_coordinator.inspect(
            session_id=started["session_id"],
        )

        self.assertEqual(result["decision"], "completed-local")
        self.assertEqual(
            result["changed_paths"],
            ["论文笔记/Robotics/Method.md"],
        )
        self.assertFalse(inspected["guardian_alive"])
        self.assertEqual(inspected["registered_conflicts"], [])
        self.assertEqual(note.read_text(encoding="utf-8"), "# Method\n")

    def test_active_session_cannot_be_preempted_by_different_intent(self) -> None:
        started = self._start()
        with self.assertRaises(standalone_coordinator.StandaloneError) as caught:
            self._start("arxiv:2607.00002")
        self.assertEqual(caught.exception.code, "active-session")

        cancelled = standalone_coordinator.cancel(
            session_id=started["session_id"],
            confirm_session_id=started["session_id"],
        )
        self.assertEqual(cancelled["decision"], "cancelled")

    def test_live_session_requires_exact_confirmation_before_resume(self) -> None:
        started = self._start()
        run_dir = Path(started["manifest"]).parent
        note = self.vault / "论文笔记" / "Method.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Partial method\n", encoding="utf-8")
        report = self._write_report(
            started,
            [("论文笔记/Method.md", "note")],
            result="progress",
        )
        checkpoint = standalone_coordinator.submit(
            session_id=started["session_id"],
            report_path=report,
        )
        self.assertEqual(checkpoint["decision"], "checkpointed")
        first_status = run_guardian.guardian_status(run_dir)
        self.assertIsNone(first_status["idle_timeout_seconds"])

        still_running = self._start()
        self.assertEqual(still_running["decision"], "still-running")
        self.assertEqual(
            still_running["confirmation_session_id"],
            started["session_id"],
        )
        self.assertEqual(
            run_guardian.guardian_status(run_dir)["pid"],
            first_status["pid"],
        )

        with self.assertRaises(standalone_coordinator.StandaloneError) as caught:
            self._start(
                confirm_running_session_id=(
                    "standalone-paper-reader-0000000000000000"
                ),
            )
        self.assertEqual(caught.exception.code, "confirmation-required")
        self.assertEqual(
            run_guardian.guardian_status(run_dir)["pid"],
            first_status["pid"],
        )

        resumed = self._start(
            confirm_running_session_id=started["session_id"],
        )
        self.assertEqual(resumed["decision"], "ready")
        self.assertEqual(resumed["mode"], "resumed-after-confirmed-stop")
        self.assertEqual(resumed["session_id"], started["session_id"])
        replacement_status = run_guardian.guardian_status(run_dir)
        self.assertNotEqual(replacement_status["pid"], first_status["pid"])
        self.assertIsNone(replacement_status["idle_timeout_seconds"])
        self.assertEqual(note.read_text(encoding="utf-8"), "# Partial method\n")
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), self.base_head)

    def test_stale_confirmation_does_not_create_a_new_session(self) -> None:
        with self.assertRaises(standalone_coordinator.StandaloneError) as caught:
            self._start(
                confirm_running_session_id=(
                    "standalone-paper-reader-0000000000000000"
                ),
            )
        self.assertEqual(caught.exception.code, "confirmation-required")
        self.assertEqual(
            list(
                (
                    self.vault / ".dailypaper" / "runs"
                ).glob("standalone-*")
            ),
            [],
        )

    def test_cli_forwards_exact_live_guardian_confirmation(self) -> None:
        session_id = "standalone-paper-reader-0123456789abcdef"
        result = {"decision": "ready", "session_id": session_id}
        with (
            patch.object(
                sys,
                "argv",
                [
                    "standalone_coordinator.py",
                    "start",
                    "--operation",
                    "paper-reader",
                    "--harness",
                    "codex",
                    "--intent",
                    "arxiv:2607.00001",
                    "--confirm-running-session-id",
                    session_id,
                ],
            ),
            patch.object(
                standalone_coordinator,
                "start",
                return_value=result,
            ) as start_mock,
            patch.object(standalone_coordinator, "_print") as print_mock,
        ):
            self.assertEqual(standalone_coordinator.main(), 0)

        start_mock.assert_called_once_with(
            operation="paper-reader",
            harness="codex",
            intent="arxiv:2607.00001",
            confirm_running_session_id=session_id,
        )
        print_mock.assert_called_once_with(result)

    def test_report_rejects_traversal_without_touching_user_file(self) -> None:
        started = self._start()
        outside = self.root / "outside.md"
        outside.write_text("keep\n", encoding="utf-8")
        report = Path(started["manifest"]).parent / "report.json"
        report.write_text(
            json.dumps(
                {
                    "version": 1,
                    "session_id": started["session_id"],
                    "operation": "paper-reader",
                    "result": "success",
                    "artifacts": [
                        {
                            "path": "../outside.md",
                            "sha256": hashlib.sha256(b"keep\n").hexdigest(),
                            "kind": "note",
                        }
                    ],
                    "changed_paths": ["../outside.md"],
                    "message": None,
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(standalone_coordinator.StandaloneError) as caught:
            standalone_coordinator.submit(
                session_id=started["session_id"],
                report_path=report,
            )
        self.assertEqual(caught.exception.code, "invalid-path")
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep\n")

    def test_submit_does_not_follow_symlinked_report(self) -> None:
        started = self._start()
        target = Path(started["manifest"]).parent / "target-report.json"
        report = self._write_report(started, [])
        target.write_bytes(report.read_bytes())
        expected = target.read_bytes()
        report.unlink()
        report.symlink_to(target)

        with self.assertRaises(standalone_coordinator.StandaloneError):
            standalone_coordinator.submit(
                session_id=started["session_id"],
                report_path=report,
            )

        self.assertTrue(report.is_symlink())
        self.assertEqual(target.read_bytes(), expected)

    def test_unknown_dirty_path_is_preserved_and_blocks_completion(self) -> None:
        started = self._start()
        note = self.vault / "论文笔记" / "Method.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Method\n", encoding="utf-8")
        unrelated = self.vault / "user-edit.md"
        unrelated.write_text("preserve\n", encoding="utf-8")
        report = self._write_report(
            started,
            [("论文笔记/Method.md", "note")],
        )

        with self.assertRaises(standalone_coordinator.StandaloneError) as caught:
            standalone_coordinator.submit(
                session_id=started["session_id"],
                report_path=report,
            )

        self.assertEqual(caught.exception.code, "unknown-changes")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve\n")
        inspected = standalone_coordinator.inspect(
            session_id=started["session_id"],
        )
        self.assertFalse(inspected["guardian_alive"])
        self.assertEqual(
            inspected["session"]["condition"],
            "attention-required",
        )

    def test_direct_submission_computes_artifact_hashes(self) -> None:
        started = self._start()
        note = self.vault / "论文笔记" / "Method.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Method\n", encoding="utf-8")

        result = standalone_coordinator.submit(
            session_id=started["session_id"],
            result="success",
            paths=["论文笔记/Method.md"],
        )
        inspected = standalone_coordinator.inspect(
            session_id=started["session_id"],
        )

        self.assertEqual(result["decision"], "completed-local")
        self.assertEqual(result["changed_paths"], ["论文笔记/Method.md"])
        self.assertEqual(
            inspected["session"]["artifacts"]["论文笔记/Method.md"],
            {
                "kind": "file",
                "sha256": hashlib.sha256(b"# Method\n").hexdigest(),
            },
        )

    def test_submit_rejects_mixed_report_and_direct_arguments(self) -> None:
        started = self._start()
        report = self._write_report(started, [], result="recoverable")

        with self.assertRaises(standalone_coordinator.StandaloneError) as caught:
            standalone_coordinator.submit(
                session_id=started["session_id"],
                report_path=report,
                result="recoverable",
            )

        self.assertEqual(caught.exception.code, "mixed-submission")

    def test_failed_resume_submission_releases_new_guardian(self) -> None:
        started = self._start()
        run_dir = Path(started["manifest"]).parent
        run_guardian.stop_guardian(run_dir)
        note = self.vault / "论文笔记" / "Method.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Method\n", encoding="utf-8")

        with self.assertRaises(standalone_coordinator.StandaloneError) as caught:
            standalone_coordinator.submit(
                session_id=started["session_id"],
                result="success",
                paths=["论文笔记/Missing.md"],
            )

        self.assertEqual(caught.exception.code, "artifact-missing")
        self.assertFalse(
            standalone_coordinator.inspect(
                session_id=started["session_id"],
            )["guardian_alive"]
        )

    def test_no_change_success_does_not_create_empty_commit(self) -> None:
        self.context["automation"]["git_commit"] = True
        self.context["automation"]["git_push"] = True
        started = self._start()

        result = standalone_coordinator.submit(
            session_id=started["session_id"],
            result="success",
            paths=[],
        )

        self.assertEqual(result["decision"], "unchanged")
        self.assertEqual(git(self.vault, "rev-parse", "HEAD"), self.base_head)
        self.assertEqual(
            git(self.remote, "rev-parse", "refs/heads/main"),
            self.base_head,
        )
        self.assertFalse(
            standalone_coordinator.inspect(
                session_id=started["session_id"],
            )["guardian_alive"]
        )

    def test_publication_preserves_unregistered_staged_artifact_version(
        self,
    ) -> None:
        self.context["automation"]["git_commit"] = True
        self.context["automation"]["git_push"] = True
        started = self._start()
        note = self.vault / "论文笔记" / "Method.md"
        note.parent.mkdir(parents=True)
        note.write_text("# User staged version\n", encoding="utf-8")
        git(self.vault, "add", "--", "论文笔记/Method.md")
        staged = subprocess.run(
            [
                "git",
                "-C",
                str(self.vault),
                "show",
                ":论文笔记/Method.md",
            ],
            check=True,
            capture_output=True,
        ).stdout
        note.write_text("# Session version\n", encoding="utf-8")

        with self.assertRaises(standalone_coordinator.StandaloneError) as caught:
            standalone_coordinator.submit(
                session_id=started["session_id"],
                result="success",
                paths=["论文笔记/Method.md"],
            )

        self.assertEqual(caught.exception.code, "index-conflict")
        self.assertEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.vault),
                    "show",
                    ":论文笔记/Method.md",
                ],
                check=True,
                capture_output=True,
            ).stdout,
            staged,
        )
        self.assertEqual(
            note.read_text(encoding="utf-8"),
            "# Session version\n",
        )
        self.assertFalse(
            standalone_coordinator.inspect(
                session_id=started["session_id"],
            )["guardian_alive"]
        )

    def test_registered_progress_resumes_after_guardian_loss(self) -> None:
        started = self._start()
        note = self.vault / "论文笔记" / "Method.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Partial method\n", encoding="utf-8")
        report = self._write_report(
            started,
            [("论文笔记/Method.md", "note")],
            result="progress",
        )
        checkpoint = standalone_coordinator.submit(
            session_id=started["session_id"],
            report_path=report,
        )
        self.assertEqual(checkpoint["decision"], "checkpointed")
        self.assertTrue(
            standalone_coordinator.inspect(
                session_id=started["session_id"],
            )["guardian_alive"]
        )
        run_guardian.stop_guardian(Path(started["manifest"]).parent)

        resumed = self._start()

        self.assertEqual(resumed["decision"], "ready")
        self.assertEqual(resumed["mode"], "resumed")
        self.assertEqual(resumed["session_id"], started["session_id"])
        self.assertEqual(note.read_text(encoding="utf-8"), "# Partial method\n")

    def test_cancel_requires_exact_confirmation_and_preserves_artifacts(self) -> None:
        started = self._start()
        note = self.vault / "论文笔记" / "Method.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Keep\n", encoding="utf-8")
        with self.assertRaises(standalone_coordinator.StandaloneError) as caught:
            standalone_coordinator.cancel(
                session_id=started["session_id"],
                confirm_session_id="standalone-paper-reader-0000000000000000",
            )
        self.assertEqual(caught.exception.code, "confirmation-required")

        result = standalone_coordinator.cancel(
            session_id=started["session_id"],
            confirm_session_id=started["session_id"],
        )
        self.assertEqual(result["decision"], "cancelled")
        self.assertEqual(note.read_text(encoding="utf-8"), "# Keep\n")
        self.assertFalse(
            standalone_coordinator.inspect(
                session_id=started["session_id"],
            )["guardian_alive"]
        )

    def test_failed_guardian_stop_does_not_mark_session_cancelled(self) -> None:
        started = self._start()
        with (
            patch.object(
                standalone_coordinator.run_guardian,
                "stop_guardian",
                side_effect=run_guardian.GuardianUnavailable("simulated failure"),
            ),
            patch.object(standalone_coordinator, "_guardian_alive", return_value=True),
            self.assertRaises(standalone_coordinator.StandaloneError) as caught,
        ):
            standalone_coordinator.cancel(
                session_id=started["session_id"],
                confirm_session_id=started["session_id"],
            )

        self.assertEqual(caught.exception.code, "guardian-stop-failed")
        inspected = standalone_coordinator.inspect(
            session_id=started["session_id"],
        )
        self.assertIsNone(inspected["session"]["outcome"])

    def test_publication_reuses_commit_after_failed_push(self) -> None:
        self.context["automation"]["git_commit"] = True
        self.context["automation"]["git_push"] = True
        started = self._start()
        note = self.vault / "论文笔记" / "Method.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Method\n", encoding="utf-8")
        report = self._write_report(
            started,
            [("论文笔记/Method.md", "note")],
        )
        real_git = standalone_coordinator._git

        def reject_push(vault: Path, *args: str, check: bool = True):
            if args and args[0] == "push":
                return subprocess.CompletedProcess(
                    ["git", *args],
                    1,
                    "",
                    "simulated network failure",
                )
            return real_git(vault, *args, check=check)

        with patch.object(
            standalone_coordinator,
            "_git",
            side_effect=reject_push,
        ):
            with self.assertRaises(standalone_coordinator.StandaloneError) as caught:
                standalone_coordinator.submit(
                    session_id=started["session_id"],
                    report_path=report,
                )
        self.assertEqual(caught.exception.code, "push-failed")
        preserved = git(self.vault, "rev-parse", "HEAD")

        result = standalone_coordinator.submit(
            session_id=started["session_id"],
            report_path=report,
        )

        self.assertEqual(result["decision"], "published")
        self.assertEqual(result["commit"], preserved)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/main"), preserved)
        self.assertEqual(git(self.vault, "status", "--porcelain"), "")

    def test_successful_push_with_lost_response_is_still_success(self) -> None:
        self.context["automation"]["git_commit"] = True
        self.context["automation"]["git_push"] = True
        started = self._start()
        note = self.vault / "论文笔记" / "Method.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Method\n", encoding="utf-8")
        report = self._write_report(
            started,
            [("论文笔记/Method.md", "note")],
        )
        real_git = standalone_coordinator._git

        def ambiguous_push(vault: Path, *args: str, check: bool = True):
            result = real_git(vault, *args, check=check)
            if args and args[0] == "push":
                return subprocess.CompletedProcess(
                    result.args,
                    1,
                    result.stdout,
                    "response lost",
                )
            return result

        with patch.object(
            standalone_coordinator,
            "_git",
            side_effect=ambiguous_push,
        ):
            result = standalone_coordinator.submit(
                session_id=started["session_id"],
                report_path=report,
            )

        self.assertEqual(result["decision"], "published")
        self.assertEqual(
            git(self.remote, "rev-parse", "refs/heads/main"),
            result["commit"],
        )


if __name__ == "__main__":
    unittest.main()
