from __future__ import annotations

import importlib.util
import json
import contextlib
import io
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "skills" / "daily-papers" / "scripts" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
MODULE_PATH = (
    REPO_ROOT
    / "skills"
    / "daily-papers"
    / "scripts"
    / "daily"
    / "fetch_and_score.py"
)
SPEC = importlib.util.spec_from_file_location("fetch_identity_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
fetch_and_score = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch_and_score)


class FetchIdentityTests(unittest.TestCase):
    def test_days_argument_has_an_explicit_supported_range(self) -> None:
        self.assertEqual(fetch_and_score.bounded_days("1"), 1)
        self.assertEqual(
            fetch_and_score.bounded_days(str(fetch_and_score.MAX_FETCH_DAYS)),
            fetch_and_score.MAX_FETCH_DAYS,
        )
        for value in (
            "0",
            "-1",
            str(fetch_and_score.MAX_FETCH_DAYS + 1),
            "not-a-number",
        ):
            with self.subTest(value=value):
                with self.assertRaises(fetch_and_score.argparse.ArgumentTypeError):
                    fetch_and_score.bounded_days(value)

    def test_hf_response_contract_rejects_bad_roots_and_skips_bad_items(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(
                fetch_and_score._decode_hf_items(
                    '{"not":"an array"}',
                    label="test-hf",
                ),
                [],
            )
            self.assertEqual(
                fetch_and_score._decode_hf_items(
                    '[{"paper":{}}, 1, null]',
                    label="test-hf",
                ),
                [{"paper": {}}],
            )
            self.assertEqual(
                fetch_and_score._decode_hf_items(
                    '[{"paper":{},"paper":{"id":"2607.00001"}}]',
                    label="test-hf",
                ),
                [],
            )
            with patch.object(
                fetch_and_score,
                "MAX_HF_ITEMS_PER_RESPONSE",
                1,
            ):
                self.assertEqual(
                    fetch_and_score._decode_hf_items(
                        "[{}, {}]",
                        label="test-hf",
                    ),
                    [],
                )

        self.assertIn("root is not an array", stderr.getvalue())
        self.assertIn("skipped 2 non-object items", stderr.getvalue())
        self.assertIn("duplicate JSON key", stderr.getvalue())
        self.assertIn("1-item limit", stderr.getvalue())

    def test_fetch_phase_reuses_one_aggregate_budget_across_endpoints(self) -> None:
        budget = object()
        target = date(2026, 7, 29)
        with patch.object(
            fetch_and_score,
            "fetch_url",
            return_value="",
        ) as fetch:
            fetch_and_score.fetch_hf_papers(
                target,
                target,
                budget=budget,
            )
            fetch_and_score.fetch_arxiv_papers(
                target,
                target,
                days=1,
                budget=budget,
            )

        self.assertEqual(fetch.call_count, 3)
        self.assertTrue(
            all(call.kwargs["budget"] is budget for call in fetch.call_args_list)
        )

    def test_hf_fetch_survives_malformed_item_field_types(self) -> None:
        valid_id = "2607.00001"
        response = json.dumps(
            [
                "not-an-object",
                {"paper": "not-an-object"},
                {
                    "paper": {
                        "id": valid_id,
                        "title": ["wrong"],
                        "summary": {"wrong": True},
                        "publishedAt": 123,
                        "authors": {"wrong": True},
                        "upvotes": "many",
                    }
                },
            ]
        )
        with patch.object(
            fetch_and_score,
            "fetch_url",
            return_value=response,
        ):
            papers = fetch_and_score.fetch_hf_papers()

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["paper_id"], f"arxiv:{valid_id}")
        self.assertEqual(papers[0]["title"], "")
        self.assertEqual(papers[0]["abstract"], "")
        self.assertEqual(papers[0]["hf_upvotes"], 0)

    def test_arxiv_fetch_skips_entries_with_empty_required_text(self) -> None:
        xml = """\
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title />
    <summary />
    <id>https://arxiv.org/abs/2607.00001</id>
  </entry>
</feed>
"""
        with patch.object(
            fetch_and_score,
            "fetch_url",
            return_value=xml,
        ):
            self.assertEqual(fetch_and_score.fetch_arxiv_papers(), [])

    def test_arxiv_response_cannot_exceed_the_requested_entry_count(self) -> None:
        entry = """\
  <entry>
    <title>Robot Paper</title>
    <summary>robot world model</summary>
    <id>https://arxiv.org/abs/2607.00001</id>
  </entry>
"""
        xml = (
            '<feed xmlns="http://www.w3.org/2005/Atom">\n'
            + entry
            + entry.replace("2607.00001", "2607.00002")
            + "</feed>"
        )
        with (
            patch.object(fetch_and_score, "ARXIV_RESULTS_PER_DAY", 1),
            patch.object(fetch_and_score, "fetch_url", return_value=xml),
        ):
            self.assertEqual(fetch_and_score.fetch_arxiv_papers(days=1), [])

    def test_arxiv_xml_rejects_dtd_before_standard_parser(self) -> None:
        payload = """\
<?xml version="1.0"?>
<!DOCTYPE feed [<!ENTITY repeated "paper">]>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>&repeated;</title>
</feed>
"""
        with (
            patch.object(fetch_and_score, "fetch_url", return_value=payload),
            patch.object(fetch_and_score.ET, "fromstring") as parser,
        ):
            result = fetch_and_score.fetch_arxiv_papers(
                date(2026, 7, 29),
                date(2026, 7, 29),
                days=1,
            )

        self.assertEqual(result, [])
        parser.assert_not_called()

    def test_frozen_runtime_context_configures_fetch_without_shared_reread(
        self,
    ) -> None:
        original = {
            "KEYWORDS": fetch_and_score.KEYWORDS,
            "NEGATIVE_KEYWORDS": fetch_and_score.NEGATIVE_KEYWORDS,
            "DOMAIN_BOOST_KEYWORDS": fetch_and_score.DOMAIN_BOOST_KEYWORDS,
            "ARXIV_CATEGORIES": fetch_and_score.ARXIV_CATEGORIES,
            "MIN_SCORE": fetch_and_score.MIN_SCORE,
            "TOP_N": fetch_and_score.TOP_N,
            "DAILYPAPERS_DIR": fetch_and_score.DAILYPAPERS_DIR,
            "HISTORY_PATH": fetch_and_score.HISTORY_PATH,
        }
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                context_path = root / "runtime-context.json"
                daily = root / "Vault" / "DailyPapers"
                context_path.write_text(
                    json.dumps(
                        {
                            "status": "ready",
                            "paths": {"daily_papers": str(daily)},
                            "runtime": {"timezone": "Asia/Shanghai"},
                            "daily_papers": {
                                "keywords": ["frozen keyword"],
                                "negative_keywords": ["frozen negative"],
                                "domain_boost_keywords": ["frozen boost"],
                                "arxiv_categories": ["cs.RO"],
                                "min_score": 7,
                                "top_n": 9,
                            },
                        }
                    ),
                    encoding="utf-8",
                )

                timezone = fetch_and_score.configure_runtime_context(context_path)

                self.assertEqual(timezone, "Asia/Shanghai")
                self.assertEqual(fetch_and_score.KEYWORDS, ["frozen keyword"])
                self.assertEqual(fetch_and_score.TOP_N, 9)
                self.assertEqual(
                    fetch_and_score.HISTORY_PATH,
                    daily / ".history.json",
                )
        finally:
            for name, value in original.items():
                setattr(fetch_and_score, name, value)

    def test_huggingface_item_emits_canonical_stable_identity(self) -> None:
        parsed = fetch_and_score._parse_hf_item(
            {
                "paper": {
                    "id": "2607.01234v3",
                    "title": "Robot World Model",
                    "summary": "A world model for robot manipulation.",
                    "authors": [{"name": "A. Author"}],
                    "publishedAt": "2026-07-29T00:00:00Z",
                }
            },
            "hf-daily",
        )

        self.assertIsNotNone(parsed)
        arxiv_id, paper = parsed
        self.assertEqual(arxiv_id, "2607.01234")
        self.assertEqual(paper["arxiv_id"], "2607.01234")
        self.assertEqual(paper["paper_id"], "arxiv:2607.01234")
        self.assertEqual(paper["url"], "https://arxiv.org/abs/2607.01234")

    def test_runtime_context_does_not_follow_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.json"
            outside.write_text('{"status":"ready"}', encoding="utf-8")
            context_path = root / "runtime-context.json"
            context_path.symlink_to(outside)

            with self.assertRaisesRegex(
                fetch_and_score.RuntimeContextError,
                "regular file",
            ):
                fetch_and_score.configure_runtime_context(context_path)

    def test_runtime_context_reuses_canonical_daily_config_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context_path = root / "runtime-context.json"
            context_path.write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "paths": {
                            "daily_papers": str(root / "Vault" / "DailyPapers")
                        },
                        "runtime": {"timezone": "Asia/Shanghai"},
                        "daily_papers": {
                            "keywords": ["robot"],
                            "negative_keywords": [],
                            "domain_boost_keywords": [],
                            "arxiv_categories": ["cs.RO"],
                            "min_score": 0,
                            "top_n": 10_000,
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                fetch_and_score.RuntimeContextError,
                "top_n must be an integer from 1 to 200",
            ):
                fetch_and_score.configure_runtime_context(context_path)

    def test_remote_fetch_is_bounded(self) -> None:
        class OversizedClient:
            def new_budget(self, **_kwargs):
                return object()

            def fetch_bytes(self, *_args, **_kwargs):
                raise fetch_and_score.SafeHTTPError(
                    "remote response exceeds byte budget"
                )

        self.assertEqual(
            fetch_and_score.fetch_url(
                "https://example.invalid",
                client=OversizedClient(),
            ),
            "",
        )

    def test_merge_deduplicates_arxiv_versions_by_identity(self) -> None:
        first = {
            "paper_id": "arxiv:2607.01234",
            "arxiv_id": "2607.01234",
            "title": "First",
            "abstract": "robot world model",
            "url": "https://arxiv.org/abs/2607.01234v1",
            "score": 4,
        }
        second = {
            **first,
            "title": "Second",
            "url": "https://arxiv.org/abs/2607.01234v3",
            "score": 5,
        }

        merged = fetch_and_score.merge_and_dedup(
            [first],
            [second],
            date(2026, 7, 29),
            days=2,
            top_n=10,
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "Second")

    def test_fallback_history_is_anchored_to_the_run_target_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            daily_dir = Path(temp_dir)
            (daily_dir / "2026-07-28-论文推荐.md").write_text(
                "https://arxiv.org/abs/2607.01234\n",
                encoding="utf-8",
            )
            (daily_dir / "2026-07-27-论文推荐.md").write_text(
                "https://arxiv.org/abs/2607.05678\n",
                encoding="utf-8",
            )
            with patch.object(
                fetch_and_score,
                "DAILYPAPERS_DIR",
                daily_dir,
            ):
                ids = fetch_and_score.load_fallback_ids(
                    date(2026, 7, 29),
                    days=2,
                )

        self.assertEqual(ids, {"2607.01234", "2607.05678"})


if __name__ == "__main__":
    unittest.main()
