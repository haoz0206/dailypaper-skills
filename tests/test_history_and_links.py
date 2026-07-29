import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_ROOT = REPO_ROOT / "skills" / "daily-papers"
SHARED_DIR = SUITE_ROOT / "scripts" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import user_config


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HistoryAndLinksTests(unittest.TestCase):
    def tearDown(self) -> None:
        user_config.clear_config_cache()

    def test_history_uses_configured_daily_papers_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "paths": {
                            "daily_papers_folder": "ResearchDigest",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "DAILYPAPER_CONFIG": str(config_path),
                    "DAILYPAPER_VAULT": str(root),
                },
                clear=False,
            ):
                user_config.clear_config_cache()
                module = load_module(
                    "update_history_under_test",
                    REPO_ROOT
                    / "skills"
                    / "daily-papers"
                    / "scripts"
                    / "review"
                    / "update_history.py",
                )
                module.update_history(
                    [{"id": "2607.00001", "title": "Example"}],
                    "2026-07-26",
                )

            history_path = root / "ResearchDigest" / ".history.json"
            self.assertTrue(history_path.exists())
            history = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual(history[0]["id"], "2607.00001")

    def test_history_inputs_are_bounded_regular_files_with_strict_shapes(self) -> None:
        module = load_module(
            "update_history_boundaries_under_test",
            REPO_ROOT
            / "skills"
            / "daily-papers"
            / "scripts"
            / "review"
            / "update_history.py",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            oversized = root / "oversized.json"
            oversized.write_bytes(b"[" + b" " * 64 + b"]")
            with (
                patch.object(module, "MAX_ENRICHED_INPUT_BYTES", 16),
                self.assertRaisesRegex(module.HistoryError, "safety limit"),
            ):
                module.load_from_enriched(oversized)

            outside = root / "outside.json"
            outside.write_text("[]", encoding="utf-8")
            linked = root / "linked.json"
            linked.symlink_to(outside)
            with self.assertRaisesRegex(module.HistoryError, "regular file"):
                module.load_from_enriched(linked)

            malformed = root / "malformed.json"
            malformed.write_text('[{"arxiv_id": 42}]', encoding="utf-8")
            with self.assertRaisesRegex(module.HistoryError, "invalid arxiv_id"):
                module.load_from_enriched(malformed)

    def test_recommendation_input_rejects_invalid_utf8_and_excess_papers(self) -> None:
        module = load_module(
            "update_history_recommendation_under_test",
            REPO_ROOT
            / "skills"
            / "daily-papers"
            / "scripts"
            / "review"
            / "update_history.py",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid = root / "invalid.md"
            invalid.write_bytes(b"\xff")
            with self.assertRaisesRegex(module.HistoryError, "valid UTF-8"):
                module.load_from_recommendation(invalid)

            recommendation = root / "many.md"
            recommendation.write_text(
                "\n".join(
                    [
                        "https://arxiv.org/abs/2607.00001",
                        "https://arxiv.org/abs/2607.00002",
                    ]
                ),
                encoding="utf-8",
            )
            with (
                patch.object(module, "MAX_INPUT_PAPERS", 1),
                self.assertRaisesRegex(module.HistoryError, "paper safety limit"),
            ):
                module.load_from_recommendation(recommendation)

    def test_backfill_scan_honors_notes_dir_and_concepts_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            notes = root / "CustomNotes"
            concepts = notes / "_concepts"
            paper = notes / "Robotics" / "Paper.md"
            concept = concepts / "Topic" / "Concept.md"
            paper.parent.mkdir(parents=True)
            concept.parent.mkdir(parents=True)
            paper.write_text("# paper\n", encoding="utf-8")
            concept.write_text("# concept\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {"DAILYPAPER_VAULT": str(root)},
                clear=False,
            ):
                user_config.clear_config_cache()
                module = load_module(
                    "backfill_links_under_test",
                    REPO_ROOT
                    / "skills"
                    / "daily-papers"
                    / "scripts"
                    / "notes"
                    / "backfill_links.py",
                )
                index = module.scan_notes(
                    notes_dir=notes,
                    concepts_path=concepts,
                )

            self.assertEqual([record.stem for record in index.records], ["Paper"])

    def test_backfill_uses_exact_identity_and_refuses_ambiguous_name_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            notes = root / "论文笔记"
            first = notes / "A" / "Shared.md"
            second = notes / "B" / "Shared.md"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text(
                "\n".join(
                    [
                        "---",
                        'title: "First"',
                        'method_name: "Shared"',
                        'paper_id: "arxiv:2607.00001"',
                        "---",
                    ]
                ),
                encoding="utf-8",
            )
            second.write_text(
                "\n".join(
                    [
                        "---",
                        'title: "Second"',
                        'method_name: "Shared"',
                        'paper_id: "arxiv:2607.00002"',
                        "---",
                    ]
                ),
                encoding="utf-8",
            )
            recommendation = root / "DailyPapers" / "today.md"
            recommendation.parent.mkdir()
            recommendation.write_text(
                "\n".join(
                    [
                        "## 分流表",
                        "",
                        "| 等级 | 论文 |",
                        "|---|---|",
                        "| 必读 | [[Shared]] |",
                        "",
                        "## 论文点评",
                        "",
                        "### 1. Shared: Exact",
                        "- **链接**: [arXiv](https://arxiv.org/abs/2607.00002v2)",
                        "- **来源**: 📄 arXiv 关键词检索",
                        "",
                        "### 2. Shared: No identity",
                        "- **来源**: Local",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"DAILYPAPER_VAULT": str(root)},
                clear=False,
            ):
                user_config.clear_config_cache()
                module = load_module(
                    "backfill_links_identity_under_test",
                    REPO_ROOT
                    / "skills"
                    / "daily-papers"
                    / "scripts"
                    / "notes"
                    / "backfill_links.py",
                )
                index = module.scan_notes(notes_dir=notes)
                count = module.backfill_links(recommendation, index)

            content = recommendation.read_text(encoding="utf-8")
            self.assertEqual(count, 1)
            self.assertIn(
                "[[论文笔记/B/Shared]]",
                content,
            )
            self.assertEqual(content.count("- 📒 **笔记**:"), 1)

    def test_diversion_table_replaces_duplicate_method_links_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            notes = root / "论文笔记"
            for topic, paper_id in (("A", "2607.00001"), ("B", "2607.00002")):
                note = notes / topic / "Shared.md"
                note.parent.mkdir(parents=True)
                note.write_text(
                    "\n".join(
                        [
                            "---",
                            f'title: "{topic}"',
                            'method_name: "Shared"',
                            f'paper_id: "arxiv:{paper_id}"',
                            "---",
                        ]
                    ),
                    encoding="utf-8",
                )
            recommendation = root / "DailyPapers" / "today.md"
            recommendation.parent.mkdir()
            recommendation.write_text(
                "\n".join(
                    [
                        "## 分流表",
                        "",
                        "| 等级 | 论文 |",
                        "|---|---|",
                        "| 必读 | [[Shared]] · [[Shared]] |",
                        "",
                        "## 论文点评",
                        "",
                        "### 1. Shared: First",
                        "- **链接**: [arXiv](https://arxiv.org/abs/2607.00001)",
                        "- **来源**: 📄 arXiv",
                        "",
                        "### 2. Shared: Second",
                        "- **链接**: [arXiv](https://arxiv.org/abs/2607.00002)",
                        "- **来源**: 📄 arXiv",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"DAILYPAPER_VAULT": str(root)},
                clear=False,
            ):
                user_config.clear_config_cache()
                module = load_module(
                    "backfill_links_order_under_test",
                    REPO_ROOT
                    / "skills"
                    / "daily-papers"
                    / "scripts"
                    / "notes"
                    / "backfill_links.py",
                )
                count = module.backfill_links(
                    recommendation,
                    module.scan_notes(notes_dir=notes),
                )

            content = recommendation.read_text(encoding="utf-8")
            self.assertEqual(count, 2)
            table = content.split("## 论文点评", 1)[0]
            self.assertIn("[[论文笔记/A/Shared]]", table)
            self.assertIn("[[论文笔记/B/Shared]]", table)


if __name__ == "__main__":
    unittest.main()
