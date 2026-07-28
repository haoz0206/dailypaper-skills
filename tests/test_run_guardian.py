import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


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

    def test_stop_releases_lock_and_allows_restart(self) -> None:
        first = self._launch()
        self._wait_until_ready(first)

        response = run_guardian.stop_guardian(self.run_dir)
        self.assertEqual(response["action"], "stop")
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
