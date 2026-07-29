from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SHARED = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "daily-papers"
    / "scripts"
    / "shared"
)
sys.path.insert(0, str(SHARED))

import stage_report  # noqa: E402


def valid_report() -> dict:
    return {
        "version": 1,
        "stage": "fetch",
        "result": "progress",
        "artifacts": [],
        "changed_paths": [],
        "message": None,
        "retry_at": None,
        "metadata": {},
    }


class StageReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.run_dir = self.vault / ".dailypaper" / "runs" / "run-1"
        self.run_dir.mkdir(parents=True)

    def test_report_is_bounded_strict_and_does_not_follow_symlink(self) -> None:
        outside = self.run_dir / "outside.json"
        outside.write_text(json.dumps(valid_report()), encoding="utf-8")
        report = self.run_dir / "fetch-result.json"
        report.symlink_to(outside)

        with self.assertRaisesRegex(stage_report.StageReportError, "regular file"):
            stage_report.load_stage_report(
                report,
                phase="fetching",
                run_dir=self.run_dir,
                vault=self.vault,
            )

        report.unlink()
        report.write_text(
            '{"version":1,"version":1}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            stage_report.StageReportError,
            "duplicate JSON key",
        ):
            stage_report.load_stage_report(
                report,
                phase="fetching",
                run_dir=self.run_dir,
                vault=self.vault,
            )

    def test_unchanged_verification_uses_the_same_safe_boundary(self) -> None:
        report = self.run_dir / "fetch-result.json"
        report.write_text(json.dumps(valid_report()), encoding="utf-8")
        submission = stage_report.load_stage_report(
            report,
            phase="fetching",
            run_dir=self.run_dir,
            vault=self.vault,
        )
        submission.verify_unchanged()

        outside = self.run_dir / "replacement.json"
        outside.write_text(json.dumps(valid_report()), encoding="utf-8")
        report.unlink()
        report.symlink_to(outside)
        with self.assertRaisesRegex(
            stage_report.StageReportError,
            "became unreadable",
        ):
            submission.verify_unchanged()

    def test_paths_reject_control_characters_and_symlink_escape(self) -> None:
        report_path = self.run_dir / "fetch-result.json"
        control = valid_report()
        control["changed_paths"] = ["DailyPapers/\nsecret.md"]
        report_path.write_text(json.dumps(control), encoding="utf-8")
        with self.assertRaisesRegex(stage_report.StageReportError, "control character"):
            stage_report.load_stage_report(
                report_path,
                phase="fetching",
                run_dir=self.run_dir,
                vault=self.vault,
            )

        outside = self.root / "outside"
        outside.mkdir()
        (self.vault / "linked").symlink_to(outside, target_is_directory=True)
        escaped = valid_report()
        escaped["artifacts"] = [
            {"role": "candidates", "scope": "vault", "path": "linked/data.json"}
        ]
        report_path.write_text(json.dumps(escaped), encoding="utf-8")
        with self.assertRaisesRegex(stage_report.StageReportError, "escapes"):
            stage_report.load_stage_report(
                report_path,
                phase="fetching",
                run_dir=self.run_dir,
                vault=self.vault,
            )


if __name__ == "__main__":
    unittest.main()
