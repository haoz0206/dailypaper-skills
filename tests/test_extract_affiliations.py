from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "skills"
    / "daily-papers"
    / "scripts"
    / "daily"
    / "extract_affiliations.py"
)
SPEC = importlib.util.spec_from_file_location(
    "extract_affiliations_under_test",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
extract_affiliations = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extract_affiliations)


class ExtractAffiliationsTests(unittest.TestCase):
    def test_cli_stdin_is_bounded(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        oversized = "x" * (extract_affiliations.MAX_INPUT_CHARS + 1)

        with (
            patch.object(sys, "stdin", io.StringIO(oversized)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = extract_affiliations.main()

        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("input exceeds", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
