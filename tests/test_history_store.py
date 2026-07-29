import json
import sys
import tempfile
import unittest
from pathlib import Path


SHARED_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "daily-papers"
    / "scripts"
    / "shared"
)
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import history_store


class HistoryStoreTests(unittest.TestCase):
    def test_round_trip_is_atomic_and_preserves_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".history.json"
            path.write_text("[]\n", encoding="utf-8")
            path.chmod(0o640)
            history = [
                {
                    "id": "2607.01234",
                    "date": "2026-07-29",
                    "title": "A paper",
                }
            ]

            history_store.save_history(path, history)

            self.assertEqual(history_store.load_history(path), history)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(path.parent.glob("..history.json.*.tmp")), [])

    def test_malformed_history_is_never_treated_as_empty(self) -> None:
        cases = (
            b'{"not":"an array"}',
            b'[{"id":"2607.00001","id":"2607.00002","date":"2026-07-29"}]',
            b'[{"id":"2607.00001","date":NaN}]',
            b'[{"id":"../../escape","date":"2026-07-29"}]',
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".history.json"
            for raw in cases:
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    with self.assertRaises(history_store.HistoryError):
                        history_store.load_history(path)
                    self.assertEqual(path.read_bytes(), raw)

    def test_rejects_oversized_history_before_json_parse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".history.json"
            path.write_bytes(b"[" + b" " * history_store.MAX_HISTORY_BYTES + b"]")

            with self.assertRaisesRegex(
                history_store.HistoryError,
                "safety limit",
            ):
                history_store.load_history(path)

    def test_save_rejects_invalid_entries_without_touching_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".history.json"
            original = json.dumps(
                [
                    {
                        "id": "2607.01234",
                        "date": "2026-07-29",
                        "title": "",
                    }
                ]
            ).encode()
            path.write_bytes(original)

            with self.assertRaises(history_store.HistoryError):
                history_store.save_history(
                    path,
                    [{"id": "bad", "date": "2026-07-29", "title": ""}],
                )

            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
