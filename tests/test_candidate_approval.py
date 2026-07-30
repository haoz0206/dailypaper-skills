from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


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
    / "candidate_approval.py"
)
SPEC = importlib.util.spec_from_file_location(
    "candidate_approval_under_test",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
candidate_approval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(candidate_approval)


def paper(paper_id: str, title: str, score: int) -> dict:
    arxiv_id = paper_id.removeprefix("arxiv:")
    return {
        "paper_id": paper_id,
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": "A. Author",
        "affiliations": "",
        "abstract": f"Abstract for {title} about robot learning.",
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
        "date": "2026-07-29",
        "score": score,
        "category": "cs.RO",
        "categories": ["cs.RO", "cs.AI"],
        "source": "arxiv",
    }


class CandidateApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name) / "run"
        self.run_dir.mkdir()
        self.acquired = self.run_dir / "acquired-papers.json"
        self.acquisition_summary = self.run_dir / "acquisition-summary.json"
        self.docs = self.run_dir / "candidate-docs"
        self.evaluations = self.run_dir / "relevance-evaluations"
        self.index = self.run_dir / "candidate-index.json"
        self.output = self.run_dir / "candidates.json"
        self.summary = self.run_dir / "approval-summary.json"
        self.papers = [
            paper("arxiv:2607.00001", "Directly Relevant", 0),
            paper("arxiv:2607.00002", "Borderline Work", 1),
            paper("arxiv:2607.00003", "Keyword Rescue", 5),
        ]
        self.acquired.write_text(
            json.dumps(self.papers),
            encoding="utf-8",
        )
        acquired_raw = self.acquired.read_bytes()
        self.acquisition_summary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "complete": True,
                    "target_date": "2026-07-29",
                    "window_days": 1,
                    "arxiv": {
                        "complete": True,
                        "query_total": len(self.papers),
                        "parsed": len(self.papers),
                        "start_date": "2026-07-29",
                        "end_date": "2026-07-29",
                        "categories": ["cs.RO"],
                    },
                    "huggingface_count": 0,
                    "acquired_count": len(self.papers),
                    "selection_eligible_count": len(self.papers),
                    "acquired_sha256": hashlib.sha256(acquired_raw).hexdigest(),
                }
            ),
            encoding="utf-8",
        )

    def _prepare(self) -> dict:
        candidate_approval.prepare(
            self.acquired,
            self.acquisition_summary,
            self.docs,
            self.evaluations,
            self.index,
        )
        return json.loads(self.index.read_text(encoding="utf-8"))

    def _write_evaluation(
        self,
        record: dict,
        decision: str,
        relevance: int,
    ) -> None:
        path = self.run_dir / record["evaluation_path"]
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "paper_id": record["paper_id"],
                    "input_sha256": record["candidate_sha256"],
                    "decision": decision,
                    "relevance": relevance,
                    "confidence": 0.8,
                    "topics": ["robot learning"],
                    "reason": "The abstract gives enough evidence for this decision.",
                    "evaluator": "test-low-cost",
                }
            ),
            encoding="utf-8",
        )

    def test_prepare_materializes_one_hashed_markdown_per_paper(self) -> None:
        index = self._prepare()

        self.assertEqual(len(index["papers"]), 3)
        for record in index["papers"]:
            candidate = self.run_dir / record["candidate_path"]
            text = candidate.read_text(encoding="utf-8")
            self.assertIn(record["paper_id"], text)
            self.assertIn("## Abstract", text)
            self.assertIn("untrusted paper content", text)

        status = candidate_approval.pending(self.index)
        self.assertEqual(status["status"], "pending")
        self.assertEqual(status["completed"], 0)
        self.assertEqual(len(status["pending"]), 3)

    def test_collect_keeps_approve_uncertain_and_keyword_rescue(self) -> None:
        index = self._prepare()
        decisions = (
            ("approve", 90),
            ("uncertain", 45),
            ("reject", 5),
        )
        for record, (decision, relevance) in zip(index["papers"], decisions):
            self._write_evaluation(record, decision, relevance)

        result = candidate_approval.collect(
            self.index,
            self.output,
            self.summary,
            top_n=3,
            min_score=2,
        )

        self.assertEqual(result["selected"], 3)
        selected = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(
            [
                item["approval"]["selection_reason"]
                for item in selected
            ],
            ["approve", "uncertain", "keyword-rescue"],
        )
        self.assertEqual(candidate_approval.pending(self.index)["status"], "complete")

    def test_collect_refuses_missing_or_stale_approval(self) -> None:
        index = self._prepare()
        self._write_evaluation(index["papers"][0], "approve", 90)
        with self.assertRaisesRegex(
            candidate_approval.ApprovalError,
            "approvals are still pending",
        ):
            candidate_approval.collect(
                self.index,
                self.output,
                self.summary,
                top_n=3,
                min_score=2,
            )

        for record in index["papers"][1:]:
            self._write_evaluation(record, "reject", 1)
        stale_path = self.run_dir / index["papers"][1]["evaluation_path"]
        stale = json.loads(stale_path.read_text(encoding="utf-8"))
        stale["input_sha256"] = "0" * 64
        stale_path.write_text(json.dumps(stale), encoding="utf-8")
        with self.assertRaisesRegex(
            candidate_approval.ApprovalError,
            "input hash mismatch",
        ):
            candidate_approval.collect(
                self.index,
                self.output,
                self.summary,
                top_n=3,
                min_score=2,
            )

    def test_collect_detects_candidate_mutation(self) -> None:
        index = self._prepare()
        for record in index["papers"]:
            self._write_evaluation(record, "approve", 80)
        candidate = self.run_dir / index["papers"][0]["candidate_path"]
        candidate.write_text("changed", encoding="utf-8")

        with self.assertRaisesRegex(
            candidate_approval.ApprovalError,
            "Candidate Markdown changed",
        ):
            candidate_approval.collect(
                self.index,
                self.output,
                self.summary,
                top_n=3,
                min_score=2,
            )

    def test_collect_evaluates_but_defers_history_ineligible_paper(self) -> None:
        self.papers[0]["daily_selection_eligible"] = False
        self.acquired.write_text(json.dumps(self.papers), encoding="utf-8")
        summary = json.loads(self.acquisition_summary.read_text(encoding="utf-8"))
        summary["selection_eligible_count"] = 2
        summary["acquired_sha256"] = hashlib.sha256(
            self.acquired.read_bytes()
        ).hexdigest()
        self.acquisition_summary.write_text(json.dumps(summary), encoding="utf-8")
        index = self._prepare()
        for record in index["papers"]:
            self._write_evaluation(record, "approve", 90)

        result = candidate_approval.collect(
            self.index,
            self.output,
            self.summary,
            top_n=3,
            min_score=2,
        )

        self.assertEqual(result["history_deferred"], 1)
        self.assertEqual(result["counts"]["approve"], 3)
        selected = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(len(selected), 2)

    def test_prepare_requires_summary_bound_to_acquired_metadata(self) -> None:
        summary = json.loads(self.acquisition_summary.read_text(encoding="utf-8"))
        summary["acquired_sha256"] = "0" * 64
        self.acquisition_summary.write_text(json.dumps(summary), encoding="utf-8")

        with self.assertRaisesRegex(
            candidate_approval.ApprovalError,
            "hash does not match",
        ):
            self._prepare()

    def test_index_payload_cannot_drift_from_acquired_metadata(self) -> None:
        index = self._prepare()
        index["papers"][0]["paper"]["title"] = "Tampered title"
        self.index.write_text(json.dumps(index), encoding="utf-8")

        with self.assertRaisesRegex(
            candidate_approval.ApprovalError,
            "payload changed after preparation",
        ):
            candidate_approval.pending(self.index)

    def test_index_paths_use_shared_portable_containment_rules(self) -> None:
        index = self._prepare()
        index["papers"][0]["candidate_path"] = "../outside.md"
        self.index.write_text(json.dumps(index), encoding="utf-8")

        with self.assertRaisesRegex(
            candidate_approval.ApprovalError,
            "normalized relative POSIX path",
        ):
            candidate_approval.pending(self.index)


if __name__ == "__main__":
    unittest.main()
