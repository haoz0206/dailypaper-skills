from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "skills" / "daily-papers" / "scripts" / "paper-reader"
sys.path.insert(0, str(SCRIPT_DIR))

import validate_paper_note  # noqa: E402


class PaperNoteValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_note(self, lines: list[str]) -> Path:
        note = self.root / "Paper.md"
        note.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return note

    def test_validates_markdown_and_obsidian_image_forms(self) -> None:
        lines = [
            "---",
            'title: "Paper"',
            "---",
            "## 关键公式",
            "$$x = y$$",
            "另一个公式是 $z = 1$。",
            "## 关键图表",
            "![[assets/figure-1.png]]",
            "## 实验结果",
        ]
        lines.extend(f"内容 {index}" for index in range(120 - len(lines)))
        report = validate_paper_note.validate_note(self._write_note(lines))

        self.assertTrue(report["valid"])
        self.assertEqual(report["checks"]["line_count"]["actual"], 120)
        self.assertEqual(report["checks"]["formula_count"]["actual"], 2)
        self.assertEqual(report["checks"]["image_count"]["actual"], 1)
        self.assertEqual(report["failures"], [])

    def test_reports_every_failed_structural_check_without_mutating_note(self) -> None:
        note = self._write_note(["# 骨架笔记", "没有公式或图片"])
        original = note.read_bytes()

        report = validate_paper_note.validate_note(note)

        self.assertFalse(report["valid"])
        self.assertEqual(
            set(report["failures"]),
            {
                "line_count",
                "formula_count",
                "image_count",
                "required_sections",
            },
        )
        self.assertEqual(note.read_bytes(), original)

    def test_cli_returns_one_for_invalid_note_and_json_report(self) -> None:
        note = self._write_note(["# incomplete"])
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "validate_paper_note.py"), str(note)],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["valid"])
        self.assertEqual(result.stderr, "")

    def test_expected_identity_is_required_for_new_note_validation(self) -> None:
        lines = [
            "---",
            'title: "Paper"',
            'paper_id: "arxiv:2607.00001v2"',
            'arxiv_id: "2607.00001"',
            "---",
            "## 关键公式",
            "$$x = y$$",
            "另一个公式是 $z = 1$。",
            "## 关键图表",
            "![[assets/figure-1.png]]",
            "## 实验结果",
        ]
        lines.extend(f"内容 {index}" for index in range(120 - len(lines)))
        note = self._write_note(lines)

        valid = validate_paper_note.validate_note(
            note,
            expected_paper_id="arxiv:2607.00001",
        )
        invalid = validate_paper_note.validate_note(
            note,
            expected_paper_id="arxiv:2607.00002",
        )

        self.assertTrue(valid["checks"]["paper_identity"]["valid"])
        self.assertFalse(invalid["valid"])
        self.assertIn("paper_identity", invalid["failures"])

    def test_legacy_note_without_identity_remains_structurally_compatible(self) -> None:
        lines = [
            "---",
            'title: "Legacy Paper"',
            "---",
            "## 关键公式",
            "$$x = y$$",
            "另一个公式是 $z = 1$。",
            "## 关键图表",
            "![[assets/figure-1.png]]",
            "## 实验结果",
        ]
        lines.extend(f"内容 {index}" for index in range(120 - len(lines)))

        report = validate_paper_note.validate_note(self._write_note(lines))

        self.assertTrue(report["valid"])
        self.assertTrue(report["checks"]["paper_identity"]["legacy_missing"])

    def test_invalid_declared_identity_cannot_hide_behind_arxiv_field(self) -> None:
        lines = [
            "---",
            'title: "Paper"',
            'paper_id: "not-a-stable-id"',
            'arxiv_id: "2607.00001"',
            "---",
            "## 关键公式",
            "$$x = y$$",
            "另一个公式是 $z = 1$。",
            "## 关键图表",
            "![[assets/figure-1.png]]",
            "## 实验结果",
        ]
        lines.extend(f"内容 {index}" for index in range(120 - len(lines)))

        report = validate_paper_note.validate_note(
            self._write_note(lines),
            expected_paper_id="arxiv:2607.00001",
        )

        self.assertFalse(report["valid"])
        self.assertIsNone(report["checks"]["paper_identity"]["declared"])
        self.assertIn("paper_identity", report["failures"])

    def test_rejects_symlink_and_oversized_note(self) -> None:
        target = self._write_note(["# target"])
        link = self.root / "Linked.md"
        link.symlink_to(target)

        with self.assertRaisesRegex(
            validate_paper_note.NoteValidationError,
            "readable regular file",
        ):
            validate_paper_note.validate_note(link)

        oversized = self.root / "Oversized.md"
        oversized.write_bytes(b"x" * 9)
        original_limit = validate_paper_note.MAX_NOTE_BYTES
        validate_paper_note.MAX_NOTE_BYTES = 8
        try:
            with self.assertRaisesRegex(
                validate_paper_note.NoteValidationError,
                "safety limit",
            ):
                validate_paper_note.validate_note(oversized)
        finally:
            validate_paper_note.MAX_NOTE_BYTES = original_limit


if __name__ == "__main__":
    unittest.main()
