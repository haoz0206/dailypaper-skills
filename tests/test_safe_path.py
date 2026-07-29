import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath


SHARED_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "daily-papers"
    / "scripts"
    / "shared"
)
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import safe_path


class SafePathTests(unittest.TestCase):
    def test_accepts_normalized_unicode_relative_path(self) -> None:
        self.assertEqual(
            safe_path.relative_posix_path("论文笔记/视觉 模型.md"),
            PurePosixPath("论文笔记/视觉 模型.md"),
        )

    def test_rejects_non_portable_or_non_normalized_paths(self) -> None:
        invalid = (
            None,
            "",
            ".",
            "..",
            "../note.md",
            "notes/../note.md",
            "notes/./note.md",
            "notes//note.md",
            "/notes/note.md",
            "notes\\note.md",
            "notes/\nsecret.md",
            "notes/\x7fsecret.md",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(safe_path.SafePathError):
                safe_path.relative_posix_path(value)

    def test_enforces_explicit_character_budget(self) -> None:
        with self.assertRaisesRegex(
            safe_path.SafePathError,
            "character safety limit",
        ) as raised:
            safe_path.relative_posix_path("abcd", max_chars=3)
        self.assertEqual(raised.exception.code, "too-long")
        for invalid in (True, 0, -1, 1.5):
            with self.subTest(max_chars=invalid), self.assertRaises(ValueError):
                safe_path.relative_posix_path("note.md", max_chars=invalid)

    def test_resolve_within_accepts_missing_child_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.assertEqual(
                safe_path.resolve_within(root, "notes/new.md"),
                root / "notes" / "new.md",
            )

    def test_resolve_within_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "vault"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(
                safe_path.SafePathError,
                "traverses a symlink",
            ):
                safe_path.resolve_within(root, "linked/secret.md")

    def test_resolve_within_rejects_symlinks_that_remain_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            real = root / "real"
            real.mkdir()
            (real / "note.md").write_text("safe", encoding="utf-8")
            (root / "linked-dir").symlink_to(real, target_is_directory=True)
            (root / "linked-file.md").symlink_to(real / "note.md")

            for relative in ("linked-dir/note.md", "linked-file.md"):
                with (
                    self.subTest(relative=relative),
                    self.assertRaisesRegex(
                        safe_path.SafePathError,
                        "traverses a symlink",
                    ),
                ):
                    safe_path.resolve_within(root, relative)


if __name__ == "__main__":
    unittest.main()
