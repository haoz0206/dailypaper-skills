from __future__ import annotations

import importlib.util
import asyncio
import json
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


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
    / "enrich_papers.py"
)
SPEC = importlib.util.spec_from_file_location("enrich_papers_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
enrich_papers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(enrich_papers)


class EnrichPapersIOTests(unittest.TestCase):
    def test_html_fetch_uses_shared_bounded_http_interface(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.budget_kwargs = None
                self.fetch_kwargs = None

            def new_budget(self, **kwargs):
                self.budget_kwargs = kwargs
                return object()

            def fetch_bytes(self, _url, **kwargs):
                self.fetch_kwargs = kwargs
                return types.SimpleNamespace(body=b"<html>safe</html>")

        async def scenario() -> None:
            client = FakeClient()
            content = await enrich_papers.fetch_text(
                "https://arxiv.org/html/2607.00001",
                asyncio.Semaphore(1),
                retries=1,
                client=client,
            )
            self.assertEqual(content, "<html>safe</html>")
            self.assertEqual(
                client.budget_kwargs["max_total_bytes"],
                enrich_papers.MAX_HTML_BYTES,
            )
            self.assertEqual(
                client.fetch_kwargs["max_bytes"],
                enrich_papers.MAX_HTML_BYTES,
            )
            self.assertEqual(
                client.fetch_kwargs["allowed_media_types"],
                {"text/html", "application/xhtml+xml"},
            )

        asyncio.run(scenario())

    def test_html_retries_share_one_deadline_and_byte_budget(self) -> None:
        class RetryClient:
            def __init__(self) -> None:
                self.budget_calls = 0
                self.fetch_calls = 0

            def new_budget(self, **_kwargs):
                self.budget_calls += 1
                return types.SimpleNamespace(remaining_seconds=lambda: 1.0)

            def fetch_bytes(self, _url, **_kwargs):
                self.fetch_calls += 1
                if self.fetch_calls == 1:
                    raise enrich_papers.SafeHTTPError("temporary failure")
                return types.SimpleNamespace(body=b"<html>recovered</html>")

        async def scenario() -> None:
            client = RetryClient()
            with patch.object(
                enrich_papers.asyncio,
                "sleep",
                new=AsyncMock(),
            ):
                content = await enrich_papers.fetch_text(
                    "https://arxiv.org/html/2607.00001",
                    asyncio.Semaphore(1),
                    retries=2,
                    client=client,
                )
            self.assertEqual(content, "<html>recovered</html>")
            self.assertEqual(client.fetch_calls, 2)
            self.assertEqual(client.budget_calls, 1)

        asyncio.run(scenario())

    def test_html_does_not_retry_an_exhausted_byte_budget(self) -> None:
        class ExhaustedClient:
            def __init__(self) -> None:
                self.fetch_calls = 0

            def new_budget(self, **_kwargs):
                return types.SimpleNamespace(remaining_seconds=lambda: 1.0)

            def fetch_bytes(self, _url, **_kwargs):
                self.fetch_calls += 1
                raise enrich_papers.ResponseTooLargeError(
                    "aggregate budget exhausted"
                )

        async def scenario() -> None:
            client = ExhaustedClient()
            sleeper = AsyncMock()
            with patch.object(enrich_papers.asyncio, "sleep", new=sleeper):
                content = await enrich_papers.fetch_text(
                    "https://arxiv.org/html/2607.00001",
                    asyncio.Semaphore(1),
                    retries=3,
                    client=client,
                )
            self.assertEqual(content, "")
            self.assertEqual(client.fetch_calls, 1)
            sleeper.assert_not_awaited()

        asyncio.run(scenario())

    def test_enrichment_creates_only_one_concurrency_batch_of_tasks(self) -> None:
        active = 0
        peak = 0
        budgets: list[object] = []

        async def fake_enrich(paper, _semaphore, **kwargs):
            nonlocal active, peak
            budgets.append(kwargs["budget"])
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return {**paper, "enriched": True}

        papers = [{"index": index} for index in range(25)]
        budget = enrich_papers.FetchBudget(
            max_total_bytes=1024,
            request_timeout_seconds=1,
            run_timeout_seconds=10,
        )
        with patch.object(enrich_papers, "enrich_one", side_effect=fake_enrich):
            result = asyncio.run(
                enrich_papers.enrich_all(papers, budget=budget)
            )

        self.assertLessEqual(peak, enrich_papers.SEMAPHORE_LIMIT)
        self.assertTrue(all(item is budget for item in budgets))
        self.assertEqual(
            [paper["index"] for paper in result],
            list(range(25)),
        )

    def test_enrichment_stops_before_scheduling_after_budget_exhaustion(self) -> None:
        budget = enrich_papers.FetchBudget(
            max_total_bytes=1,
            request_timeout_seconds=1,
            run_timeout_seconds=10,
        )
        budget.consume(1)
        papers = [{"index": 0}, {"index": 1}]

        with patch.object(enrich_papers, "enrich_one") as enrich:
            result = asyncio.run(
                enrich_papers.enrich_all(papers, budget=budget)
            )

        enrich.assert_not_called()
        self.assertEqual(result, papers)

    def test_file_output_is_atomic_private_and_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "run" / "enriched.json"
            enrich_papers._write_output([], output)
            self.assertEqual(output.read_text(encoding="utf-8"), "[]\n")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

            outside = root / "outside"
            outside.write_text("user", encoding="utf-8")
            output.unlink()
            output.symlink_to(outside)
            with self.assertRaises(SystemExit) as raised:
                enrich_papers._write_output([1], output)
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(outside.read_text(encoding="utf-8"), "user")

    def test_file_output_is_bounded_before_any_file_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "enriched.json"
            with (
                patch.object(enrich_papers, "MAX_OUTPUT_BYTES", 2),
                self.assertRaises(SystemExit) as raised,
            ):
                enrich_papers._write_output([1], output)

            self.assertEqual(raised.exception.code, 2)
            self.assertFalse(output.exists())

    def test_main_rejects_non_array_input_and_writes_empty_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.json"
            output_path = root / "output.json"
            input_path.write_text(json.dumps({"not": "an array"}), encoding="utf-8")

            with patch.object(
                sys,
                "argv",
                [
                    "enrich_papers.py",
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                ],
            ):
                with self.assertRaises(SystemExit) as raised:
                    enrich_papers.main()

            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), [])

    def test_main_rejects_excessive_paper_count_before_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.json"
            output_path = root / "output.json"
            input_path.write_text("[{}, {}]", encoding="utf-8")

            with (
                patch.object(enrich_papers, "MAX_INPUT_PAPERS", 1),
                patch.object(
                    sys,
                    "argv",
                    [
                        "enrich_papers.py",
                        "--input",
                        str(input_path),
                        "--output",
                        str(output_path),
                    ],
                ),
                self.assertRaises(SystemExit) as raised,
            ):
                enrich_papers.main()

            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
