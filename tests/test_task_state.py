from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "skills" / "daily-papers" / "scripts" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import task_state  # noqa: E402
from tests.task_state_fixtures import make_task_state  # noqa: E402


class TaskStateCodecTests(unittest.TestCase):
    def test_roundtrip_preserves_every_legal_status(self) -> None:
        for status in sorted(task_state.STATUSES):
            with self.subTest(status=status):
                expected = make_task_state(status)
                encoded = task_state.encode_task_state(expected, source="roundtrip")
                self.assertLessEqual(len(encoded), task_state.MAX_TASK_STATE_BYTES)
                self.assertEqual(
                    task_state.parse_task_state(encoded, source="roundtrip"),
                    expected,
                )

    def test_rejects_path_traversal_and_unsafe_run_ids(self) -> None:
        cases = (
            ("run_id", "../../outside"),
            ("run_id", "/absolute"),
            ("run_id", r"machine\run"),
            ("run_id", "run..other"),
            ("outputs.daily_note", "../outside.md"),
            ("outputs.daily_note", "/absolute.md"),
            ("outputs.daily_note", r"DailyPapers\today.md"),
            ("outputs.daily_note", ".git/config"),
            ("changed_paths[0]", "DailyPapers/../../outside.md"),
            ("changed_paths[0]", ".dailypaper/config.json"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                state = make_task_state(
                    "success" if field.startswith("changed_paths") else "running"
                )
                if field == "run_id":
                    state["run_id"] = value
                elif field.startswith("outputs"):
                    state["outputs"]["daily_note"] = value
                else:
                    state["changed_paths"][0] = value
                with self.assertRaisesRegex(
                    task_state.TaskStateError,
                    "safe|run_id",
                ):
                    task_state.validate_task_state(state, source="malicious")

    def test_rejects_oversize_duplicate_nan_invalid_utf8_and_recursion(self) -> None:
        valid = make_task_state()
        encoded = json.dumps(valid, separators=(",", ":"))
        duplicate = encoded.replace(
            '"version":1',
            '"version":1,"version":1',
            1,
        ).encode()
        nan = encoded.replace(
            '"version":1',
            '"version":1,"unknown":NaN',
            1,
        ).encode()
        cases = (
            (
                b" " * (task_state.MAX_TASK_STATE_BYTES + 1),
                "safety limit",
            ),
            (duplicate, "duplicate JSON key"),
            (nan, "non-standard JSON value"),
            (b"\xff", "UTF-8 JSON"),
            (
                ("[" * 2000 + "]" * 2000).encode(),
                "bounded UTF-8 JSON|JSON object",
            ),
        )
        for raw, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(task_state.TaskStateError, message):
                    task_state.parse_task_state(raw, source="hostile")

    def test_rejects_unknown_status_specific_and_wrongly_typed_fields(self) -> None:
        cases: tuple[tuple[str, object, str], ...] = (
            ("version", True, "version"),
            ("target_date", "2026-02-30", "calendar date"),
            ("window_days", 0, "1 to 31"),
            ("window_days", 32, "1 to 31"),
            ("window_days", True, "1 to 31"),
            ("status", "done", "status"),
            ("harness", "other", "harness"),
            ("owner", 3, "owner"),
            ("started_at", "2026-07-29T08:00:00", "UTC offset"),
            ("base_commit", "not-a-commit", "commit hash"),
            ("config_sha256", "f" * 40, "SHA-256"),
            ("outputs", [], "outputs"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                state = make_task_state()
                state[field] = value
                with self.assertRaisesRegex(task_state.TaskStateError, message):
                    task_state.validate_task_state(state, source="wrong-type")

        unknown = make_task_state()
        unknown["future_field"] = "unsafe ambiguity"
        with self.assertRaisesRegex(task_state.TaskStateError, "unknown fields"):
            task_state.validate_task_state(unknown, source="unknown")

        incompatible = make_task_state("cancelled")
        incompatible["lease_until"] = "2026-07-30T08:00:00+08:00"
        with self.assertRaisesRegex(task_state.TaskStateError, "incompatible"):
            task_state.validate_task_state(incompatible, source="incompatible")

        missing = make_task_state()
        missing.pop("config_sha256")
        with self.assertRaisesRegex(task_state.TaskStateError, "missing required"):
            task_state.validate_task_state(missing, source="missing")

    def test_pre_release_state_without_window_is_one_day_only(self) -> None:
        legacy = make_task_state()
        legacy.pop("window_days")

        normalized = task_state.validate_task_state(
            legacy,
            source="pre-release",
        )

        self.assertEqual(normalized["window_days"], 1)
        self.assertNotIn("window_days", legacy)

    def test_rejects_duplicate_changed_paths_and_oversize_encoded_state(self) -> None:
        duplicate = make_task_state("success")
        duplicate["changed_paths"].append(duplicate["changed_paths"][0])
        with self.assertRaisesRegex(task_state.TaskStateError, "duplicate path"):
            task_state.validate_task_state(duplicate)

        oversize = make_task_state("failed")
        oversize["message"] = "x" * (task_state.MAX_MESSAGE_LENGTH + 1)
        with self.assertRaisesRegex(task_state.TaskStateError, "must not exceed"):
            task_state.encode_task_state(oversize)

    def test_validation_returns_an_isolated_value(self) -> None:
        original = make_task_state()
        validated = task_state.validate_task_state(original)
        validated["outputs"]["daily_note"] = "DailyPapers/other.md"
        self.assertNotEqual(validated, original)
        self.assertEqual(original, make_task_state())

    def test_file_codec_is_atomic_bounded_and_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "tasks" / "daily-papers.json"
            expected = make_task_state()
            task_state.write_task_state_file(state_path, expected)
            self.assertEqual(task_state.read_task_state_file(state_path), expected)
            self.assertFalse(list(state_path.parent.glob(".*.tmp")))
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o644)
            state_path.chmod(0o640)
            task_state.write_task_state_file(state_path, expected)
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o640)

            state_path.write_bytes(
                b" " * (task_state.MAX_TASK_STATE_BYTES + 1)
            )
            with self.assertRaisesRegex(task_state.TaskStateError, "safety limit"):
                task_state.read_task_state_file(state_path)

            target = root / "outside.json"
            target.write_bytes(task_state.encode_task_state(expected))
            state_path.unlink()
            state_path.symlink_to(target)
            with self.assertRaisesRegex(task_state.TaskStateError, "regular file"):
                task_state.read_task_state_file(state_path)
            with self.assertRaisesRegex(task_state.TaskStateError, "regular file"):
                task_state.write_task_state_file(state_path, expected)


if __name__ == "__main__":
    unittest.main()
