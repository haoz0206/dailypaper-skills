import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_ROOT = REPO_ROOT / "skills" / "daily-papers"
SHARED_DIR = SUITE_ROOT / "scripts" / "shared"
MODULE_PATH = SUITE_ROOT / "scripts" / "paper-reader" / "paper_daemon.py"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import user_config


def load_daemon_module():
    spec = importlib.util.spec_from_file_location(
        "paper_daemon_under_test",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PaperDaemonCommandTests(unittest.TestCase):
    def tearDown(self) -> None:
        user_config.clear_config_cache()

    def _load_for_harness(self, harness: str):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state_dir = Path(temporary.name) / "state"
        environment = patch.dict(
            os.environ,
            {
                "DAILYPAPER_VAULT": temporary.name,
                "PAPER_DAEMON_STATE_DIR": str(state_dir),
                "PAPER_DAEMON_HARNESS": harness,
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)
        user_config.clear_config_cache()
        module = load_daemon_module()
        self.addCleanup(module._DAEMON_TEMP_DIR.cleanup)
        return module

    def test_codex_command_uses_documented_noninteractive_flags(self) -> None:
        module = self._load_for_harness("codex")
        command = module.build_harness_command(
            module.resolve_daemon_harness(),
            "read the paper",
        )

        self.assertEqual(command[0], "codex")
        self.assertIn("-a", command)
        self.assertIn("never", command)
        self.assertIn("-s", command)
        self.assertIn("workspace-write", command)
        self.assertIn("exec", command)
        self.assertIn("--ephemeral", command)
        self.assertNotIn("--full-auto", command)
        self.assertEqual(command[-1], "read the paper")

    def test_claude_command_is_noninteractive_without_safety_bypass(self) -> None:
        module = self._load_for_harness("claude-code")
        command = module.build_harness_command(
            module.resolve_daemon_harness(),
            "read the paper",
        )

        self.assertEqual(command[0], "claude")
        self.assertIn("--print", command)
        self.assertIn("--permission-mode", command)
        self.assertIn("acceptEdits", command)
        self.assertIn("--no-session-persistence", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertEqual(command[-1], "read the paper")

    def test_auto_mode_rejects_two_installed_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_VAULT": temp_dir,
                    "PAPER_DAEMON_STATE_DIR": str(state_dir),
                    "PAPER_DAEMON_HARNESS": "auto",
                    "CLAUDECODE": "",
                    "CODEX_HOME": "",
                    "CODEX_THREAD_ID": "",
                },
                clear=False,
            ), patch("shutil.which", return_value="/usr/bin/harness"):
                user_config.clear_config_cache()
                module = load_daemon_module()
                with self.assertRaises(RuntimeError) as caught:
                    module.resolve_daemon_harness()
                module._DAEMON_TEMP_DIR.cleanup()

        self.assertIn("Both Claude Code and Codex", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
