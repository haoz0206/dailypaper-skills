import os
import stat
import subprocess
import sys
import tempfile
import time
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


SCRIPT = SHARED_DIR / "run_guardian.py"


class RunGuardianTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temporary.name) / "run"
        self.processes: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        self.temporary.cleanup()

    def _launch(self, *, idle_timeout: float = 30.0) -> subprocess.Popen[str]:
        return self._launch_for(
            self.run_dir,
            idle_timeout=idle_timeout,
        )

    def _launch_for(
        self,
        run_dir: Path,
        *,
        idle_timeout: float = 30.0,
        owner_pid: int | None = None,
        vault_lock: Path | None = None,
    ) -> subprocess.Popen[str]:
        command = [
            sys.executable,
            str(SCRIPT),
            "serve",
            str(run_dir),
            "--idle-timeout",
            str(idle_timeout),
        ]
        if owner_pid is not None:
            command.extend(
                [
                    "--owner-pid",
                    str(owner_pid),
                    "--owner-start-marker",
                    run_guardian.process_start_marker(owner_pid),
                ]
            )
        if vault_lock is not None:
            command.extend(["--vault-lock", str(vault_lock)])
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.processes.append(process)
        return process

    def _wait_until_ready(
        self,
        process: subprocess.Popen[str],
        run_dir: Path | None = None,
    ) -> dict:
        target = run_dir or self.run_dir
        deadline = time.monotonic() + 5
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(
                    f"guardian exited before ready ({process.returncode})"
                )
            try:
                return run_guardian.probe_guardian(target, timeout=0.1)
            except run_guardian.GuardianUnavailable as exc:
                last_error = exc
                time.sleep(0.02)
        self.fail(f"guardian did not become ready: {last_error}")

    def _wait_until_exit(self, process: subprocess.Popen[str]) -> None:
        process.wait(timeout=5)

    def test_probe_status_and_private_session_metadata(self) -> None:
        process = self._launch()
        ping = self._wait_until_ready(process)

        self.assertEqual(ping["status"], "ok")
        self.assertEqual(ping["action"], "ping")
        status_result = run_guardian.guardian_status(self.run_dir)
        self.assertEqual(status_result["pid"], process.pid)
        self.assertTrue(status_result["owner_alive"])

        session_path = self.run_dir / run_guardian.SESSION_NAME
        mode = stat.S_IMODE(session_path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        with self.assertRaises(run_guardian.GuardianUnauthorized):
            run_guardian.request(
                self.run_dir,
                "ping",
                capability="wrong-capability",
            )

    def test_session_reader_rejects_symlink_and_oversize_file(self) -> None:
        self.run_dir.mkdir()
        paths = run_guardian.GuardianPaths.for_run(self.run_dir)
        outside = Path(self.temporary.name) / "outside-session.json"
        outside.write_text(
            '{"version":1,"capability":"outside"}',
            encoding="utf-8",
        )
        paths.session.symlink_to(outside)
        with self.assertRaises(run_guardian.GuardianUnavailable):
            run_guardian.probe_guardian(self.run_dir)

        paths.session.unlink()
        paths.session.write_bytes(
            b"{" + b" " * run_guardian.MAX_SESSION_BYTES + b"}"
        )
        with self.assertRaises(run_guardian.GuardianUnavailable):
            run_guardian.probe_guardian(self.run_dir)

    def test_execution_lock_does_not_follow_symlink(self) -> None:
        self.run_dir.mkdir()
        outside = Path(self.temporary.name) / "outside.lock"
        outside.write_text("user", encoding="utf-8")
        lock = self.run_dir / run_guardian.LOCK_NAME
        lock.symlink_to(outside)
        guardian = run_guardian.RunGuardian(self.run_dir)

        with self.assertRaisesRegex(
            run_guardian.GuardianUnavailable,
            "Run execution lock must not be a symlink",
        ):
            guardian._acquire()

        self.assertEqual(outside.read_text(encoding="utf-8"), "user")
        self.assertTrue(lock.is_symlink())

    def test_second_guardian_fails_non_blocking(self) -> None:
        first = self._launch()
        self._wait_until_ready(first)

        second = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "serve",
                str(self.run_dir),
                "--idle-timeout",
                "30",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )

        self.assertEqual(second.returncode, 3)
        self.assertIn("already-running", second.stdout)
        self.assertEqual(run_guardian.probe_guardian(self.run_dir)["pid"], first.pid)

    def test_vault_writer_lock_excludes_different_run_directories(self) -> None:
        vault_lock = Path(self.temporary.name) / "git" / "vault-writer.lock"
        first_run = Path(self.temporary.name) / "first-run"
        second_run = Path(self.temporary.name) / "second-run"
        first = self._launch_for(first_run, vault_lock=vault_lock)
        self._wait_until_ready(first, first_run)

        second = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "serve",
                str(second_run),
                "--vault-lock",
                str(vault_lock),
                "--idle-timeout",
                "30",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )

        self.assertEqual(second.returncode, 3)
        self.assertIn("already-running", second.stdout)
        status_result = run_guardian.guardian_status(first_run)
        self.assertEqual(status_result["vault_lock_path"], str(vault_lock.resolve()))

    def test_short_lived_writer_uses_the_same_non_blocking_lock(self) -> None:
        vault = Path(self.temporary.name) / "vault"
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(vault)],
            check=True,
            capture_output=True,
            text=True,
        )

        with run_guardian.hold_vault_writer_lock(vault):
            with self.assertRaises(run_guardian.GuardianAlreadyRunning):
                with run_guardian.hold_vault_writer_lock(vault):
                    self.fail("nested writer lock must not be acquired")

        with run_guardian.hold_vault_writer_lock(vault):
            pass

    def test_ensure_guardian_running_is_idempotent_and_uses_vault_lock(self) -> None:
        vault = Path(self.temporary.name) / "vault"
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(vault)],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            first = run_guardian.ensure_guardian_running(
                self.run_dir,
                vault=vault,
                idle_timeout_seconds=30,
                ready_timeout_seconds=3,
            )
            second = run_guardian.ensure_guardian_running(
                self.run_dir,
                vault=vault,
                idle_timeout_seconds=30,
                ready_timeout_seconds=3,
            )
            self.assertEqual(second["pid"], first["pid"])
            status_result = run_guardian.guardian_status(self.run_dir)
            self.assertEqual(
                status_result["vault_lock_path"],
                str(run_guardian.vault_writer_lock_path(vault)),
            )
        finally:
            try:
                run_guardian.stop_guardian(self.run_dir)
            except run_guardian.GuardianError:
                pass

    def test_ensure_guardian_running_defaults_to_no_idle_expiry(self) -> None:
        vault = Path(self.temporary.name) / "vault"
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(vault)],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            started = run_guardian.ensure_guardian_running(
                self.run_dir,
                vault=vault,
                ready_timeout_seconds=3,
            )
            status_result = run_guardian.guardian_status(self.run_dir)
            self.assertEqual(status_result["pid"], started["pid"])
            self.assertIsNone(status_result["idle_timeout_seconds"])
        finally:
            try:
                run_guardian.stop_guardian(self.run_dir)
            except run_guardian.GuardianError:
                pass

    def test_ensure_guardian_running_rejects_mismatched_vault_lock(self) -> None:
        first_vault = Path(self.temporary.name) / "first-vault"
        second_vault = Path(self.temporary.name) / "second-vault"
        for vault in (first_vault, second_vault):
            subprocess.run(
                ["git", "init", "--initial-branch=main", str(vault)],
                check=True,
                capture_output=True,
                text=True,
            )
        try:
            run_guardian.ensure_guardian_running(
                self.run_dir,
                vault=first_vault,
                idle_timeout_seconds=30,
                ready_timeout_seconds=3,
            )
            with self.assertRaisesRegex(
                run_guardian.GuardianUnavailable,
                "different Vault writer lock",
            ):
                run_guardian.ensure_guardian_running(
                    self.run_dir,
                    vault=second_vault,
                    idle_timeout_seconds=30,
                    ready_timeout_seconds=3,
                )
        finally:
            try:
                run_guardian.stop_guardian(self.run_dir)
            except run_guardian.GuardianError:
                pass

    def test_ensure_guardian_running_cleans_up_startup_timeout(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.terminated = False

            def poll(self):
                return 0 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, *, timeout: float):
                return 0

            def kill(self) -> None:
                raise AssertionError("kill should not be needed after terminate")

        process = FakeProcess()
        unavailable = run_guardian.GuardianUnavailable("not ready")
        with (
            patch.object(run_guardian, "guardian_status", side_effect=unavailable),
            patch.object(
                run_guardian,
                "vault_writer_lock_path",
                return_value=Path(self.temporary.name) / "writer.lock",
            ),
            patch.object(run_guardian.subprocess, "Popen", return_value=process),
            self.assertRaisesRegex(
                run_guardian.GuardianUnavailable,
                "did not become ready",
            ),
        ):
            run_guardian.ensure_guardian_running(
                self.run_dir,
                vault=Path(self.temporary.name),
                idle_timeout_seconds=30,
                ready_timeout_seconds=0.001,
            )
        self.assertTrue(process.terminated)

    def test_ensure_guardian_running_rejects_non_finite_timeouts(self) -> None:
        for value in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_guardian.ensure_guardian_running(
                    self.run_dir,
                    vault=Path(self.temporary.name),
                    idle_timeout_seconds=value,
                )
            with self.subTest(ready=value), self.assertRaises(ValueError):
                run_guardian.ensure_guardian_running(
                    self.run_dir,
                    vault=Path(self.temporary.name),
                    ready_timeout_seconds=value,
                )

    def test_stop_releases_lock_and_allows_restart(self) -> None:
        first = self._launch()
        self._wait_until_ready(first)

        response = run_guardian.stop_guardian(self.run_dir)
        self.assertEqual(response["action"], "stop")
        with self.assertRaises(run_guardian.GuardianUnavailable):
            run_guardian.probe_guardian(self.run_dir)
        self._wait_until_exit(first)

        second = self._launch()
        status_result = self._wait_until_ready(second)
        self.assertEqual(status_result["pid"], second.pid)

    @unittest.skipUnless(hasattr(os, "kill"), "requires POSIX process signals")
    def test_process_crash_releases_os_lock(self) -> None:
        first = self._launch()
        self._wait_until_ready(first)

        first.kill()
        self._wait_until_exit(first)

        # SIGKILL leaves stale socket/session files; the next lock owner must
        # safely replace them after acquiring the released OS lock.
        second = self._launch()
        status_result = self._wait_until_ready(second)
        self.assertEqual(status_result["pid"], second.pid)

    def test_idle_timeout_is_fallback_for_abandoned_guardian(self) -> None:
        process = self._launch(idle_timeout=0.15)
        self._wait_until_ready(process)
        self._wait_until_exit(process)

        second = self._launch()
        self._wait_until_ready(second)

    def test_live_owner_is_not_released_by_idle_timeout(self) -> None:
        process = self._launch_for(
            self.run_dir,
            idle_timeout=0.1,
            owner_pid=os.getpid(),
        )
        self._wait_until_ready(process)
        time.sleep(0.25)
        self.assertIsNone(process.poll())
        status_result = run_guardian.guardian_status(self.run_dir)
        self.assertTrue(status_result["owner_alive"])

    def test_dead_owner_releases_lock(self) -> None:
        owner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        self.processes.append(owner)
        guardian = self._launch_for(
            self.run_dir,
            idle_timeout=30,
            owner_pid=owner.pid,
        )
        self._wait_until_ready(guardian)
        owner.terminate()
        owner.wait(timeout=3)
        self._wait_until_exit(guardian)

        replacement = self._launch()
        self._wait_until_ready(replacement)

    def test_long_run_directory_uses_short_tmp_socket(self) -> None:
        deep = Path(self.temporary.name)
        for index in range(8):
            deep = deep / (f"very-long-vault-segment-{index}-" + "x" * 24)
        process = self._launch_for(deep)
        self._wait_until_ready(process, deep)

        paths = run_guardian.GuardianPaths.for_run(deep)
        self.assertEqual(paths.socket.parent, Path("/tmp"))
        self.assertLess(len(os.fsencode(paths.socket)), 100)
        self.assertNotEqual(paths.socket, deep / run_guardian.SOCKET_NAME)

    def test_acquire_failure_releases_partially_acquired_lock(self) -> None:
        paths = run_guardian.GuardianPaths.for_run(self.run_dir)
        paths.socket.mkdir()
        try:
            guardian = run_guardian.RunGuardian(
                self.run_dir,
                idle_timeout_seconds=30,
            )
            with self.assertRaises(OSError):
                guardian.serve_forever()
        finally:
            paths.socket.rmdir()

        replacement = self._launch()
        self._wait_until_ready(replacement)


if __name__ == "__main__":
    unittest.main()
