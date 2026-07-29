from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "skills" / "daily-papers" / "scripts" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import active_run_guard  # noqa: E402
from tests.task_state_fixtures import make_task_state  # noqa: E402


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


class RemoteActiveRunGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "vault.git"
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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _publish_state(self, status: str) -> None:
        path = self.seed / ".dailypaper" / "tasks" / "daily-papers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                make_task_state(
                    status,
                    run_id="2026-07-29-remote",
                    owner="server",
                )
            ),
            encoding="utf-8",
        )
        git(self.seed, "add", ".dailypaper/tasks/daily-papers.json")
        git(
            self.seed,
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            f"state {status}",
        )
        git(self.seed, "push", "origin", "main")

    def _guard(self) -> dict:
        return active_run_guard.guard_remote_active_run(
            self.vault,
            repository_url=str(self.remote),
            remote="origin",
            branch="main",
        )

    def _prepare(self) -> dict:
        return active_run_guard.prepare_standalone_vault(
            self.vault,
            repository_url=str(self.remote),
            remote="origin",
            branch="main",
        )

    def test_remote_running_state_blocks_stale_clone(self) -> None:
        self._publish_state("running")
        self.assertFalse(
            (self.vault / ".dailypaper" / "tasks" / "daily-papers.json").exists()
        )

        with self.assertRaises(active_run_guard.ActiveRunError) as raised:
            self._guard()

        self.assertEqual(raised.exception.state["run_id"], "2026-07-29-remote")

    def test_remote_terminal_state_is_safe_without_touching_worktree(self) -> None:
        self._publish_state("published")
        dirty = self.vault / "personal.md"
        dirty.write_text("keep me\n", encoding="utf-8")

        result = self._guard()

        self.assertEqual(result["task_state"], "published")
        self.assertTrue(dirty.exists())
        self.assertIn("personal.md", git(self.vault, "status", "--porcelain"))

    def test_wrong_remote_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(active_run_guard.GuardError, "does not match"):
            active_run_guard.guard_remote_active_run(
                self.vault,
                repository_url="git@example.invalid:wrong.git",
                remote="origin",
                branch="main",
            )

    def test_repository_subdirectory_cannot_be_used_as_the_vault_root(self) -> None:
        nested = self.vault / "nested"
        nested.mkdir()

        with self.assertRaisesRegex(active_run_guard.GuardError, "not the Git root"):
            active_run_guard.prepare_standalone_vault(
                nested,
                repository_url=str(self.remote),
                remote="origin",
                branch="main",
            )

    def test_prepare_fast_forwards_a_clean_clone(self) -> None:
        self._publish_state("published")

        result = self._prepare()

        self.assertTrue(result["pulled"])
        self.assertFalse(result["dirty"])
        self.assertEqual(
            git(self.vault, "rev-parse", "HEAD"),
            git(self.seed, "rev-parse", "HEAD"),
        )

    def test_prepare_rejects_a_dirty_stale_clone_without_overwriting(self) -> None:
        dirty = self.vault / "personal.md"
        dirty.write_text("keep me\n", encoding="utf-8")
        self._publish_state("published")

        with self.assertRaisesRegex(
            active_run_guard.GuardError,
            "local changes",
        ):
            self._prepare()

        self.assertEqual(dirty.read_text(encoding="utf-8"), "keep me\n")
        self.assertNotEqual(
            git(self.vault, "rev-parse", "HEAD"),
            git(self.seed, "rev-parse", "HEAD"),
        )

    def test_prepare_accepts_dirty_but_current_clone(self) -> None:
        dirty = self.vault / "personal.md"
        dirty.write_text("keep me\n", encoding="utf-8")

        result = self._prepare()

        self.assertTrue(result["dirty"])
        self.assertFalse(result["pulled"])
        self.assertEqual(dirty.read_text(encoding="utf-8"), "keep me\n")


if __name__ == "__main__":
    unittest.main()
