import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "skills" / "_shared"
MODULE_PATH = REPO_ROOT / "skills" / "paper-reader" / "paper_daemon.py"
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

    def test_command_uses_documented_noninteractive_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_VAULT": temp_dir,
                    "PAPER_DAEMON_STATE_DIR": str(state_dir),
                },
                clear=False,
            ):
                user_config.clear_config_cache()
                module = load_daemon_module()
                command = module.build_codex_command("read the paper")

            self.assertEqual(command[0], "codex")
            self.assertIn("-a", command)
            self.assertIn("never", command)
            self.assertIn("-s", command)
            self.assertIn("workspace-write", command)
            self.assertIn("exec", command)
            self.assertIn("--ephemeral", command)
            self.assertNotIn("--full-auto", command)
            self.assertEqual(command[-1], "read the paper")


if __name__ == "__main__":
    unittest.main()
