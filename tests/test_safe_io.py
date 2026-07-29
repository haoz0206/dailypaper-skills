from __future__ import annotations

import hashlib
import io
import os
import stat
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

import safe_io  # noqa: E402


class SafeIOTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_loads_strict_utf8_json_object(self) -> None:
        path = self.root / "document.json"
        path.write_bytes('{"标题":"论文","count":2}'.encode())

        self.assertEqual(
            safe_io.load_json_object(path, max_bytes=1024),
            {"标题": "论文", "count": 2},
        )

    def test_optional_missing_file_returns_none(self) -> None:
        self.assertIsNone(
            safe_io.load_json_object(
                self.root / "missing.json",
                max_bytes=1024,
                required=False,
            )
        )

    def test_rejects_symlink_directory_and_oversize_file(self) -> None:
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = self.root / "link.json"
        link.symlink_to(target)

        for path, limit in ((link, 1024), (self.root, 1024)):
            with self.subTest(path=path):
                with self.assertRaises(safe_io.SafeIOError):
                    safe_io.load_json_object(path, max_bytes=limit)

        oversized = self.root / "oversized.json"
        oversized.write_bytes(b"{" + b" " * 32 + b"}")
        with self.assertRaisesRegex(safe_io.SafeIOError, "safety limit"):
            safe_io.load_json_object(oversized, max_bytes=16)

    def test_rejects_duplicate_nonfinite_nonobject_and_invalid_utf8(self) -> None:
        documents = (
            (b'{"a":1,"a":2}', "duplicate JSON key"),
            (b'{"a":NaN}', "non-standard JSON value"),
            (b"[]", "root must be a JSON object"),
            (b'{"a":"\\xff"}'.replace(b"\\xff", b"\xff"), "UTF-8 JSON"),
        )
        for raw, message in documents:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(safe_io.SafeIOError, message):
                    safe_io.parse_json_object(
                        raw,
                        max_bytes=1024,
                        label="Test document",
                    )

    def test_json_encoding_is_deterministic_strict_and_bounded(self) -> None:
        value = {"标题": "论文", "a": 1}
        encoded = safe_io.encode_json_value(
            value,
            max_bytes=1024,
            label="State",
        )
        self.assertEqual(
            encoded,
            '{\n  "a": 1,\n  "标题": "论文"\n}\n'.encode("utf-8"),
        )
        self.assertEqual(
            safe_io.encode_json_value(value, max_bytes=1024, label="State"),
            encoded,
        )

        recursive = []
        recursive.append(recursive)
        invalid = (
            {"value": float("nan")},
            {"value": {1, 2}},
            recursive,
        )
        for document in invalid:
            with self.subTest(document=type(document).__name__):
                with self.assertRaisesRegex(
                    safe_io.JSONEncodingError,
                    "strict UTF-8 JSON",
                ):
                    safe_io.encode_json_value(
                        document,
                        max_bytes=1024,
                        label="State",
                    )
        with self.assertRaisesRegex(
            safe_io.DocumentTooLargeError,
            "safety limit",
        ):
            safe_io.encode_json_value(value, max_bytes=4, label="State")

    def test_atomic_json_combines_encoding_budget_and_safe_replace(self) -> None:
        path = self.root / "nested" / "state.json"
        safe_io.atomic_write_json(
            path,
            {"version": 1, "标题": "论文"},
            max_bytes=1024,
            mode=0o600,
            label="State",
        )
        self.assertEqual(
            safe_io.load_json_object(path, max_bytes=1024, label="State"),
            {"version": 1, "标题": "论文"},
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        outside = self.root / "outside"
        outside.write_bytes(b"user-data")
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(safe_io.SafeIOError, "regular non-symlink"):
            safe_io.atomic_write_json(
                path,
                {"version": 2},
                max_bytes=1024,
                mode=0o600,
                label="State",
            )
        self.assertEqual(outside.read_bytes(), b"user-data")

    def test_read_detects_growth_beyond_limit(self) -> None:
        path = self.root / "growing.bin"
        path.write_bytes(b"x" * 17)
        with self.assertRaisesRegex(safe_io.SafeIOError, "safety limit"):
            safe_io.read_regular_bytes(path, max_bytes=16)

    def test_prefix_read_is_bounded_but_allows_larger_regular_file(self) -> None:
        path = self.root / "note.md"
        path.write_bytes(b"frontmatter-and-body")

        self.assertEqual(
            safe_io.read_regular_prefix(path, max_bytes=5),
            b"front",
        )

        link = self.root / "linked-note.md"
        link.symlink_to(path)
        with self.assertRaises(safe_io.SafeIOError):
            safe_io.read_regular_prefix(link, max_bytes=5)

    def test_anchored_file_path_preserves_final_symlink_component(self) -> None:
        outside = self.root / "outside.json"
        outside.write_text("user", encoding="utf-8")
        link = self.root / "nested" / ".." / "linked.json"
        normalized_link = self.root / "linked.json"
        normalized_link.symlink_to(outside)

        anchored = safe_io.anchored_file_path(link, label="Test file")

        self.assertEqual(anchored, normalized_link)
        self.assertTrue(anchored.is_symlink())

    def test_streaming_hash_uses_nofollow_regular_file_boundary(self) -> None:
        path = self.root / "artifact.bin"
        payload = b"paper-artifact"
        path.write_bytes(payload)

        self.assertEqual(
            safe_io.sha256_regular_file(path),
            hashlib.sha256(payload).hexdigest(),
        )
        with self.assertRaisesRegex(safe_io.SafeIOError, "safety limit"):
            safe_io.sha256_regular_file(path, max_bytes=len(payload) - 1)

        link = self.root / "linked-artifact.bin"
        link.symlink_to(path)
        with self.assertRaises(safe_io.SafeIOError):
            safe_io.sha256_regular_file(link)

    def test_regular_file_snapshot_and_copy_use_the_same_bounded_stream(self) -> None:
        path = self.root / "artifact.bin"
        payload = b"paper-artifact-content"
        path.write_bytes(payload)

        inspected = safe_io.inspect_regular_file(
            path,
            max_bytes=len(payload),
            prefix_bytes=5,
            label="Artifact",
        )
        self.assertEqual(inspected.size, len(payload))
        self.assertEqual(inspected.prefix, b"paper")
        self.assertEqual(inspected.sha256, hashlib.sha256(payload).hexdigest())

        destination = io.BytesIO()
        copied = safe_io.copy_regular_file(
            path,
            destination,
            max_bytes=len(payload),
            prefix_bytes=8,
            label="Artifact",
        )
        self.assertEqual(destination.getvalue(), payload)
        self.assertEqual(copied.sha256, inspected.sha256)
        self.assertEqual(copied.prefix, b"paper-ar")

        with self.assertRaises(safe_io.DocumentTooLargeError):
            safe_io.inspect_regular_file(
                path,
                max_bytes=len(payload) - 1,
                label="Artifact",
            )
        link = self.root / "artifact-link"
        link.symlink_to(path)
        with self.assertRaises(safe_io.SafeIOError):
            safe_io.copy_regular_file(
                link,
                io.BytesIO(),
                max_bytes=len(payload),
                label="Artifact",
            )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_rejects_fifo_without_blocking(self) -> None:
        path = self.root / "pipe"
        os.mkfifo(path)
        with self.assertRaisesRegex(safe_io.SafeIOError, "regular file"):
            safe_io.read_regular_bytes(path, max_bytes=1024)

    def test_atomic_write_creates_exact_mode_and_preserves_existing_mode(self) -> None:
        path = self.root / "nested" / "state.json"
        safe_io.atomic_write_bytes(
            path,
            b"first\n",
            mode=0o600,
            label="State",
        )
        self.assertEqual(path.read_bytes(), b"first\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        path.chmod(0o640)
        safe_io.atomic_write_bytes(
            path,
            b"second\n",
            mode=0o644,
            preserve_existing_mode=True,
            label="State",
        )
        self.assertEqual(path.read_bytes(), b"second\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_atomic_write_rejects_symlink_without_touching_target(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"user-data")
        link = self.root / "state"
        link.symlink_to(outside)

        with self.assertRaisesRegex(
            safe_io.SafeIOError,
            "regular non-symlink",
        ):
            safe_io.atomic_write_bytes(
                link,
                b"replacement",
                mode=0o600,
                label="State",
            )

        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_bytes(), b"user-data")

    def test_atomic_write_cleans_temporary_file_after_failure(self) -> None:
        path = self.root / "state"
        original_replace = safe_io.os.replace

        def fail_replace(*args, **kwargs):
            raise OSError("injected replace failure")

        safe_io.os.replace = fail_replace
        try:
            with self.assertRaisesRegex(
                safe_io.SafeIOError,
                "written atomically",
            ):
                safe_io.atomic_write_bytes(
                    path,
                    b"new",
                    mode=0o600,
                    label="State",
                )
        finally:
            safe_io.os.replace = original_replace

        self.assertFalse(path.exists())
        self.assertEqual(list(self.root.glob(".state.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
