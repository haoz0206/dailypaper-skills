import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "skills" / "paper-reader" / "paper_daemon.py"


class PaperDaemonCommandTests(unittest.TestCase):
    def test_command_uses_documented_noninteractive_flags(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("['claude', '-p', prompt", source)
        self.assertIn("'--model', 'opus'", source)
        self.assertIn("'--permission-mode', 'acceptEdits'", source)
        self.assertIn("'--dangerously-skip-permissions'", source)
        self.assertNotIn("build_codex_command", source)


if __name__ == "__main__":
    unittest.main()
