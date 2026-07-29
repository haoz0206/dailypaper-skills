from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "skills" / "daily-papers" / "scripts" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
SCRIPT_PATH = SHARED_DIR / "safe_git.py"
SPEC = importlib.util.spec_from_file_location("canonical_safe_git", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
safe_git = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = safe_git
SPEC.loader.exec_module(safe_git)


class SafeGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(self.repo)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.com"],
            check=True,
            capture_output=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _commit(self, name: str, payload: bytes) -> None:
        (self.repo / name).write_bytes(payload)
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "--", name],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "fixture"],
            check=True,
            capture_output=True,
        )

    def test_reads_commit_and_index_blobs_without_a_shell(self) -> None:
        self._commit("note.md", b"bounded\n")
        (self.repo / "index.md").write_bytes(b"index snapshot\n")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "--", "index.md"],
            check=True,
            capture_output=True,
        )

        self.assertEqual(
            safe_git.read_git_blob(
                self.repo,
                "HEAD:note.md",
                max_bytes=1024,
            ),
            b"bounded\n",
        )
        self.assertEqual(
            safe_git.read_git_blob(
                self.repo,
                ":index.md",
                max_bytes=1024,
            ),
            b"index snapshot\n",
        )

    def test_index_version_guard_accepts_only_base_or_registered_blob(self) -> None:
        self._commit("note.md", b"base\n")
        artifact = b"artifact\n"
        expected = hashlib.sha256(artifact).hexdigest()

        safe_git.verify_index_versions(
            self.repo,
            base_commit=subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            expected_sha256_by_path={"note.md": expected},
            max_blob_bytes=1024,
        )

        (self.repo / "note.md").write_bytes(b"user staged\n")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "--", "note.md"],
            check=True,
            capture_output=True,
        )
        with self.assertRaisesRegex(
            safe_git.SafeGitError,
            "unregistered version",
        ):
            safe_git.verify_index_versions(
                self.repo,
                base_commit=subprocess.run(
                    ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                expected_sha256_by_path={"note.md": expected},
                max_blob_bytes=1024,
            )

        (self.repo / "note.md").write_bytes(artifact)
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "--", "note.md"],
            check=True,
            capture_output=True,
        )
        safe_git.verify_index_versions(
            self.repo,
            base_commit=subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            expected_sha256_by_path={"note.md": expected},
            max_blob_bytes=1024,
        )

    def test_repository_commands_have_bounded_text_streams(self) -> None:
        self._commit("note.md", b"bounded\n")
        (self.repo / "untracked-file-with-a-long-name.md").write_text(
            "untracked",
            encoding="utf-8",
        )

        result = safe_git.run_git_command(
            self.repo,
            "status",
            "--short",
            "--untracked-files=all",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("untracked-file-with-a-long-name.md", result.stdout)

        with self.assertRaisesRegex(safe_git.SafeGitError, "byte limit"):
            safe_git.run_git_command(
                self.repo,
                "status",
                "--short",
                "--untracked-files=all",
                max_stdout_bytes=8,
            )

        missing = safe_git.run_git_command(
            self.repo,
            "rev-parse",
            "--verify",
            "refs/heads/missing",
        )
        self.assertNotEqual(missing.returncode, 0)
        version = safe_git.run_git_program("--version")
        self.assertEqual(version.returncode, 0)
        self.assertTrue(version.stdout.startswith("git version "))

    def test_dirty_snapshot_uses_one_command_and_preserves_rename_paths(self) -> None:
        self._commit("tracked.md", b"original\n")
        self._commit("old name.md", b"rename me\n")
        (self.repo / "tracked.md").write_bytes(b"modified\n")
        (self.repo / "staged.md").write_bytes(b"staged\n")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "--", "staged.md"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "mv",
                "old name.md",
                "renamed name.md",
            ],
            check=True,
            capture_output=True,
        )
        (self.repo / "untracked.md").write_bytes(b"untracked\n")

        with patch.object(
            safe_git,
            "run_git_command",
            wraps=safe_git.run_git_command,
        ) as runner:
            paths = safe_git.repository_dirty_paths(self.repo)

        self.assertEqual(runner.call_count, 1)
        self.assertEqual(
            paths,
            {
                "tracked.md",
                "staged.md",
                "old name.md",
                "renamed name.md",
                "untracked.md",
            },
        )

    def test_dirty_snapshot_rejects_unsafe_or_excess_paths(self) -> None:
        self._commit("tracked.md", b"original\n")
        (self.repo / "unsafe\nname.md").write_bytes(b"unsafe\n")
        with self.assertRaisesRegex(safe_git.SafeGitError, "unsafe repository path"):
            safe_git.repository_dirty_paths(self.repo)

        (self.repo / "unsafe\nname.md").unlink()
        (self.repo / "one.md").write_bytes(b"one\n")
        (self.repo / "two.md").write_bytes(b"two\n")
        with (
            patch.object(safe_git, "MAX_DIRTY_PATHS", 1),
            self.assertRaisesRegex(safe_git.SafeGitError, "path safety limit"),
        ):
            safe_git.repository_dirty_paths(self.repo)

        malformed = safe_git.GitCommandResult(
            args=("git", "status"),
            returncode=0,
            stdout="?? incomplete.md",
            stderr="",
        )
        with (
            patch.object(safe_git, "run_git_command", return_value=malformed),
            self.assertRaisesRegex(safe_git.SafeGitError, "NUL terminator"),
        ):
            safe_git.repository_dirty_paths(self.repo)

    def test_repository_snapshot_reports_root_remote_and_detached_branch(self) -> None:
        self._commit("tracked.md", b"tracked\n")
        remote_url = "git@example.com:owner/vault.git"
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin", remote_url],
            check=True,
            capture_output=True,
        )
        child = self.repo / "nested"
        child.mkdir()

        snapshot = safe_git.inspect_repository(child, remote="origin")

        self.assertEqual(snapshot.root, self.repo.resolve())
        self.assertEqual(snapshot.remote_url, remote_url)
        self.assertEqual(snapshot.branch, "main")
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "--detach"],
            check=True,
            capture_output=True,
        )
        self.assertEqual(
            safe_git.inspect_repository(self.repo, remote="origin").branch,
            "",
        )
        with self.assertRaisesRegex(ValueError, "safe Git remote"):
            safe_git.inspect_repository(self.repo, remote="../origin")

    def test_missing_blob_is_explicitly_optional_or_required(self) -> None:
        self._commit("note.md", b"bounded\n")

        self.assertIsNone(
            safe_git.read_git_blob(
                self.repo,
                "HEAD:missing.md",
                max_bytes=1024,
            )
        )
        with self.assertRaisesRegex(safe_git.SafeGitError, "Could not resolve"):
            safe_git.read_git_blob(
                self.repo,
                "HEAD:missing.md",
                max_bytes=1024,
                missing_ok=False,
            )

        not_a_repository = Path(self.temporary.name) / "not-a-repository"
        not_a_repository.mkdir()
        with self.assertRaisesRegex(safe_git.SafeGitError, "Could not resolve"):
            safe_git.read_git_blob(
                not_a_repository,
                "HEAD:missing.md",
                max_bytes=1024,
            )

    def test_pins_resolved_blob_id_for_size_and_content_reads(self) -> None:
        self._commit("note.md", b"bounded\n")
        object_id = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "rev-parse",
                "HEAD:note.md",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        with patch.object(
            safe_git,
            "run_bounded_tool",
            wraps=safe_git.run_bounded_tool,
        ) as runner:
            safe_git.read_git_blob(
                self.repo,
                "HEAD:note.md",
                max_bytes=1024,
            )

        commands = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(commands[0][-1], "HEAD:note.md")
        self.assertEqual(commands[1][-1], object_id)
        self.assertEqual(commands[2][-1], object_id)

    def test_rejects_oversize_blob_before_materializing_content(self) -> None:
        self._commit("large.bin", b"x" * 4096)

        with patch.object(
            safe_git,
            "run_bounded_tool",
            wraps=safe_git.run_bounded_tool,
        ) as runner:
            with self.assertRaisesRegex(safe_git.SafeGitError, "safety limit"):
                safe_git.read_git_blob(
                    self.repo,
                    "HEAD:large.bin",
                    max_bytes=32,
                )

        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args.args[0][-2], "-s")


if __name__ == "__main__":
    unittest.main()
