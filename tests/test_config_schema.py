from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "skills" / "daily-papers" / "scripts" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import config_schema  # noqa: E402
import user_config  # noqa: E402


class ConfigurationSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.defaults = copy.deepcopy(user_config.DEFAULT_CONFIG)

    def test_default_is_valid_and_callers_cannot_mutate_cached_config(self) -> None:
        validated = config_schema.validate_effective_config(
            copy.deepcopy(self.defaults),
            self.defaults,
        )
        self.assertEqual(validated["daily_papers"]["top_n"], 30)

        first = user_config.load_user_config()
        first["daily_papers"]["top_n"] = 1
        second = user_config.load_user_config()
        self.assertEqual(second["daily_papers"]["top_n"], 30)

    def test_overlay_rejects_machine_paths_fixed_policy_and_unknown_fields(self) -> None:
        cases = (
            (
                {"paths": {"zotero_db": "/srv/private.sqlite"}},
                "unsupported paths fields",
            ),
            (
                {"runtime": {"timezone": "UTC"}},
                "timezone must remain",
            ),
            (
                {"repository": {"branch": "dev"}},
                "branch must remain",
            ),
            (
                {"daily_papers": {"arxiv_date_mode": "calendar-day"}},
                "unsupported daily_papers fields",
            ),
        )
        for overlay, message in cases:
            with self.subTest(overlay=overlay):
                with self.assertRaisesRegex(
                    config_schema.ConfigurationError,
                    message,
                ):
                    config_schema.validate_overlay(
                        overlay,
                        self.defaults,
                        self.defaults,
                    )

    def test_effective_paths_must_be_distinct_non_reserved_directories(self) -> None:
        cases = (
            {"paper_notes_folder": "."},
            {"paper_notes_folder": ".dailypaper/notes"},
            {
                "paper_notes_folder": "Knowledge",
                "daily_papers_folder": "Knowledge/Daily",
            },
            {
                "concepts_folder": "_meta",
                "inbox_folder": "_meta/inbox",
            },
        )
        for paths in cases:
            with self.subTest(paths=paths):
                candidate = copy.deepcopy(self.defaults)
                candidate["paths"].update(paths)
                with self.assertRaises(config_schema.ConfigurationError):
                    config_schema.validate_effective_config(
                        candidate,
                        self.defaults,
                    )

    def test_daily_normalization_is_single_canonical_operation(self) -> None:
        daily = copy.deepcopy(self.defaults["daily_papers"])
        daily["keywords"] = [" Robot Learning ", "robot learning", "VLA"]

        normalized = config_schema.normalize_daily_config(daily)

        self.assertEqual(normalized["keywords"], ["robot learning", "vla"])
        candidate = copy.deepcopy(self.defaults)
        candidate["daily_papers"] = daily
        with self.assertRaisesRegex(
            config_schema.ConfigurationError,
            "duplicate, untrimmed, or uppercase",
        ):
            config_schema.validate_effective_config(candidate, self.defaults)

    def test_strict_json_rejects_duplicate_keys_nonstandard_values_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            for raw, message in (
                ('{"paths":{},"paths":{}}', "duplicate JSON key"),
                ('{"value":NaN}', "non-standard JSON value"),
            ):
                with self.subTest(raw=raw):
                    path.write_text(raw, encoding="utf-8")
                    with self.assertRaisesRegex(
                        config_schema.ConfigurationError,
                        message,
                    ):
                        config_schema.load_json_object(path)

            path.write_bytes(b" " * (config_schema.MAX_CONFIG_BYTES + 1))
            with self.assertRaisesRegex(
                config_schema.ConfigurationError,
                "safety limit",
            ):
                config_schema.load_json_object(path)

            target = Path(temp_dir) / "target.json"
            target.write_text("{}", encoding="utf-8")
            path.unlink()
            path.symlink_to(target)
            with self.assertRaisesRegex(
                config_schema.ConfigurationError,
                "regular file",
            ):
                config_schema.load_json_object(path)

            self.assertEqual(
                config_schema.load_json_object(
                    Path(temp_dir) / "missing.json",
                    required=False,
                ),
                {},
            )

    def test_fingerprint_excludes_machine_values_but_tracks_research_scope(self) -> None:
        first = copy.deepcopy(self.defaults)
        second = copy.deepcopy(self.defaults)
        second["paths"]["zotero_db"] = "/machine/zotero.sqlite"
        second["paths"]["zotero_storage"] = "/machine/storage"
        second["repository"]["remote"] = "mirror"

        self.assertEqual(
            config_schema.configuration_fingerprint(first),
            config_schema.configuration_fingerprint(second),
        )
        second["daily_papers"]["top_n"] = 12
        self.assertNotEqual(
            config_schema.configuration_fingerprint(first),
            config_schema.configuration_fingerprint(second),
        )


if __name__ == "__main__":
    unittest.main()
