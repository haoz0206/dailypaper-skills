from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "skills"
    / "daily-papers"
    / "scripts"
    / "shared"
    / "safe_process.py"
)
SPEC = importlib.util.spec_from_file_location(
    "safe_process_under_test",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
safe_process = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = safe_process
SPEC.loader.exec_module(safe_process)


class SafeProcessTests(unittest.TestCase):
    def test_returns_bounded_stdout_and_stderr_without_shell(self) -> None:
        result = safe_process.run_bounded_tool(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stdout.buffer.write(b'out'); "
                    "sys.stderr.buffer.write(b'err')"
                ),
            ],
            timeout=2,
            max_stdout_bytes=16,
            max_stderr_bytes=16,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"out")
        self.assertEqual(result.stderr, b"err")

    def test_explicit_environment_is_copied_and_validated(self) -> None:
        environment = {"DAILYPAPER_SAFE_PROCESS_TEST": "isolated"}
        result = safe_process.run_bounded_tool(
            [
                sys.executable,
                "-c",
                (
                    "import os,sys; "
                    "sys.stdout.write(os.environ['DAILYPAPER_SAFE_PROCESS_TEST'])"
                ),
            ],
            timeout=2,
            max_stdout_bytes=32,
            max_stderr_bytes=32,
            max_file_bytes=1024,
            environment=environment,
        )

        self.assertEqual(result.stdout, b"isolated")
        self.assertEqual(environment, {"DAILYPAPER_SAFE_PROCESS_TEST": "isolated"})
        with self.assertRaisesRegex(ValueError, "safe string"):
            safe_process.run_bounded_tool(
                [sys.executable, "-c", "pass"],
                timeout=2,
                max_stdout_bytes=32,
                max_stderr_bytes=32,
                environment={"BAD=KEY": "value"},
            )

    def test_each_captured_stream_has_an_independent_hard_limit(self) -> None:
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                target = (
                    "sys.stdout.buffer"
                    if stream == "stdout"
                    else "sys.stderr.buffer"
                )
                with self.assertRaisesRegex(
                    safe_process.ProcessOutputLimitError,
                    stream,
                ):
                    safe_process.run_bounded_tool(
                        [
                            sys.executable,
                            "-c",
                            f"import sys; {target}.write(b'x' * 4096)",
                        ],
                        timeout=2,
                        max_stdout_bytes=32,
                        max_stderr_bytes=32,
                    )

    def test_timeout_kills_and_reaps_the_tool_promptly(self) -> None:
        started = time.monotonic()
        with self.assertRaises(safe_process.ProcessTimeoutError):
            safe_process.run_bounded_tool(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=0.1,
                max_stdout_bytes=16,
                max_stderr_bytes=16,
            )
        self.assertLess(time.monotonic() - started, 2)

    def test_descendant_cannot_outlive_tool_and_hold_output_pipes(self) -> None:
        started = time.monotonic()
        with self.assertRaisesRegex(
            safe_process.SafeProcessError,
            "descendant process",
        ):
            safe_process.run_bounded_tool(
                [
                    sys.executable,
                    "-c",
                    (
                        "import subprocess,sys; "
                        "subprocess.Popen([sys.executable, '-c', "
                        "'import time; time.sleep(60)'])"
                    ),
                ],
                timeout=5,
                max_stdout_bytes=16,
                max_stderr_bytes=16,
            )
        self.assertLess(time.monotonic() - started, 4)

    def test_child_output_file_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "large.bin"
            result = safe_process.run_bounded_tool(
                [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib,sys; "
                        "pathlib.Path(sys.argv[1]).write_bytes(b'x' * 4096)"
                    ),
                    str(output),
                ],
                timeout=2,
                max_stdout_bytes=1024,
                max_stderr_bytes=4096,
                max_file_bytes=128,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertLessEqual(output.stat().st_size, 128)

    def test_file_limit_uses_exec_wrapper_without_preexec_fn(self) -> None:
        original_popen = safe_process.subprocess.Popen
        invocations: list[tuple[tuple[str, ...], dict]] = []

        def recording_popen(command, **kwargs):
            invocations.append((tuple(command), dict(kwargs)))
            return original_popen(command, **kwargs)

        with patch.object(
            safe_process.subprocess,
            "Popen",
            side_effect=recording_popen,
        ):
            result = safe_process.run_bounded_tool(
                [sys.executable, "-c", "pass"],
                timeout=2,
                max_stdout_bytes=16,
                max_stderr_bytes=16,
                max_file_bytes=128,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(invocations), 1)
        command, options = invocations[0]
        self.assertIn(safe_process.CHILD_FILE_LIMIT_FLAG, command)
        self.assertTrue(options["start_new_session"])
        self.assertNotIn("preexec_fn", options)


if __name__ == "__main__":
    unittest.main()
