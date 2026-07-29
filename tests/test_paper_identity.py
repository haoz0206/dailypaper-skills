from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT
    / "skills"
    / "daily-papers"
    / "scripts"
    / "shared"
    / "paper_identity.py"
)
SHARED_DIR = MODULE_PATH.parent
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
SPEC = importlib.util.spec_from_file_location("paper_identity_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
paper_identity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = paper_identity
SPEC.loader.exec_module(paper_identity)


class PaperIdentityTests(unittest.TestCase):
    def test_note_index_bounds_tree_size_and_requires_vault_containment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            notes = vault / "论文笔记"
            notes.mkdir(parents=True)
            self._write_note(notes / "A.md", paper_id="arxiv:2607.00001")
            self._write_note(notes / "B.md", paper_id="arxiv:2607.00002")

            with (
                patch.object(paper_identity, "MAX_NOTE_INDEX_FILES", 1),
                self.assertRaisesRegex(
                    paper_identity.PaperIdentityError,
                    "file safety limit",
                ),
            ):
                paper_identity.build_note_index(notes, vault=vault)

            with (
                patch.object(paper_identity, "MAX_NOTE_INDEX_ENTRIES", 1),
                self.assertRaisesRegex(
                    paper_identity.PaperIdentityError,
                    "entry safety limit",
                ),
            ):
                paper_identity.build_note_index(notes, vault=vault)

            outside = root / "outside"
            outside.mkdir()
            with self.assertRaisesRegex(
                paper_identity.PaperIdentityError,
                "outside the configured Vault",
            ):
                paper_identity.build_note_index(outside, vault=vault)

    def test_note_index_prunes_concept_tree_before_reading_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            concepts = notes / "_概念"
            concepts.mkdir(parents=True)
            linked_target = vault / "outside.md"
            linked_target.write_text("# outside\n", encoding="utf-8")
            (concepts / "linked.md").symlink_to(linked_target)
            self._write_note(notes / "Paper.md", paper_id="arxiv:2607.00001")

            index = paper_identity.build_note_index(
                notes,
                concepts_dir=concepts,
                vault=vault,
            )

            self.assertEqual([record.stem for record in index.records], ["Paper"])

    def test_normalizes_arxiv_versions_urls_legacy_ids_and_dois(self) -> None:
        self.assertEqual(
            paper_identity.canonical_arxiv_id(
                "https://arxiv.org/pdf/2607.01234v3.pdf?download=1"
            ),
            "2607.01234",
        )
        self.assertEqual(
            paper_identity.canonical_arxiv_id("arXiv:math.GT/0309136v2"),
            "math.gt/0309136",
        )
        self.assertEqual(
            paper_identity.canonical_doi("https://doi.org/10.1145/ABC.123)."),
            "10.1145/abc.123",
        )
        self.assertIsNone(
            paper_identity.canonical_arxiv_id(
                "https://example.com/papers/2607.01234"
            )
        )

    def test_explicit_doi_with_arxiv_shaped_suffix_is_not_misclassified(self) -> None:
        metadata = paper_identity.identity_metadata("doi:10.1234/2607.01234")

        self.assertEqual(metadata["paper_id"], "doi:10.1234/2607.01234")
        self.assertEqual(metadata["doi"], "10.1234/2607.01234")

    def test_identity_prefers_explicit_valid_id_then_source_metadata(self) -> None:
        self.assertEqual(
            paper_identity.paper_identity(
                {
                    "paper_id": "arxiv:2607.01234v4",
                    "doi": "10.1000/ignored",
                }
            ),
            "arxiv:2607.01234",
        )
        self.assertEqual(
            paper_identity.paper_identity(
                {"url": "https://arxiv.org/abs/2607.00001v2"}
            ),
            "arxiv:2607.00001",
        )

    def test_frontmatter_parser_handles_quoted_identity_with_inline_comment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            note = Path(temp_dir) / "Paper.md"
            note.write_text(
                "\n".join(
                    [
                        "---",
                        'paper_id: "arxiv:2607.00001"  # canonical identity',
                        'source_url: "https://arxiv.org/abs/2607.00001"',
                        "---",
                    ]
                ),
                encoding="utf-8",
            )

            metadata = paper_identity.parse_frontmatter(note)

            self.assertEqual(metadata["paper_id"], "arxiv:2607.00001")

            link = Path(temp_dir) / "Linked.md"
            link.symlink_to(note)
            with self.assertRaises(paper_identity.PaperIdentityError):
                paper_identity.parse_frontmatter(link)

    def test_local_identity_and_match_output_do_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paper = root / "paper.pdf"
            paper.write_bytes(b"%PDF-user")
            linked_paper = root / "linked.pdf"
            linked_paper.symlink_to(paper)
            with self.assertRaisesRegex(
                paper_identity.PaperIdentityError,
                "must not be a symlink",
            ):
                paper_identity.identity_metadata(linked_paper)

            vault = root / "vault"
            notes = vault / "论文笔记"
            notes.mkdir(parents=True)
            papers = root / "papers.json"
            papers.write_text("[]", encoding="utf-8")
            outside = root / "outside.json"
            outside.write_text("user", encoding="utf-8")
            output = root / "matches.json"
            output.symlink_to(outside)
            with patch.object(
                sys,
                "argv",
                [
                    "paper_identity.py",
                    "match",
                    "--papers",
                    str(papers),
                    "--notes-dir",
                    str(notes),
                    "--vault",
                    str(vault),
                    "--output",
                    str(output),
                ],
            ):
                self.assertEqual(paper_identity.main(), 2)

            self.assertEqual(outside.read_text(encoding="utf-8"), "user")

    def test_exact_identity_wins_over_method_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            concepts = notes / "_概念"
            self._write_note(
                notes / "Robotics" / "Shared.md",
                paper_id="arxiv:2607.00001",
                method_name="Shared",
                title="First Paper",
            )
            self._write_note(
                notes / "WorldModel" / "Shared.md",
                paper_id="arxiv:2607.00002",
                method_name="Shared",
                title="Second Paper",
            )

            index = paper_identity.build_note_index(
                notes,
                concepts_dir=concepts,
                vault=vault,
            )
            result = paper_identity.match_paper_to_note(
                {
                    "arxiv_id": "2607.00002v3",
                    "title": "Shared: A New Model",
                    "method_names": ["Shared"],
                },
                index,
            )

            self.assertEqual(result["status"], "exact")
            self.assertEqual(
                result["note"]["path"],
                "论文笔记/WorldModel/Shared.md",
            )
            self.assertEqual(
                result["note"]["wikilink"],
                "论文笔记/WorldModel/Shared",
            )

    def test_ambiguous_legacy_method_match_never_selects_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            self._write_note(notes / "A" / "Shared.md", method_name="Shared")
            self._write_note(notes / "B" / "Other.md", method_name="Shared")

            index = paper_identity.build_note_index(notes, vault=vault)
            result = paper_identity.match_paper_to_note(
                {"title": "Shared: A Paper", "method_names": ["Shared"]},
                index,
            )

            self.assertEqual(result["status"], "ambiguous")
            self.assertIsNone(result["note"])
            self.assertEqual(len(result["candidates"]), 2)

    def test_different_stable_identity_never_falls_back_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            self._write_note(
                notes / "Robotics" / "Shared.md",
                paper_id="arxiv:2607.00001",
                method_name="Shared",
            )

            index = paper_identity.build_note_index(notes, vault=vault)
            result = paper_identity.match_paper_to_note(
                {
                    "paper_id": "arxiv:2607.00002",
                    "title": "Shared: A Different Paper",
                    "method_names": ["Shared"],
                },
                index,
            )

            self.assertEqual(result["status"], "ambiguous")
            self.assertEqual(result["basis"], "conflicting-paper-id")
            self.assertIsNone(result["note"])

    def test_unique_legacy_method_match_is_backward_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            self._write_note(notes / "Robotics" / "Legacy.md", method_name="Legacy")

            index = paper_identity.build_note_index(notes, vault=vault)
            result = paper_identity.match_paper_to_note(
                {"title": "Legacy: A Paper", "method_names": ["Legacy"]},
                index,
            )

            self.assertEqual(result["status"], "fallback")
            self.assertEqual(result["basis"], "unique-method-name")
            self.assertEqual(result["note"]["wikilink"], "Legacy")

    def test_cli_match_writes_compact_atomic_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            self._write_note(
                notes / "Robotics" / "Exact.md",
                paper_id="arxiv:2607.00003",
            )
            papers = vault / "candidates.json"
            papers.write_text(
                json.dumps(
                    [
                        {
                            "title": "Exact",
                            "url": "https://arxiv.org/abs/2607.00003v1",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            output = vault / "run" / "note-matches.json"

            result = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "match",
                    "--papers",
                    str(papers),
                    "--notes-dir",
                    str(notes),
                    "--vault",
                    str(vault),
                    "--output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["counts"]["exact"], 1)
            self.assertEqual(report["matches"][0]["status"], "exact")
            self.assertFalse(list(output.parent.glob(".note-matches.json.*.tmp")))

    @staticmethod
    def _write_note(
        path: Path,
        *,
        paper_id: str | None = None,
        method_name: str | None = None,
        title: str = "Example",
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "---",
            f'title: "{title}"',
            f'method_name: "{method_name or path.stem}"',
        ]
        if paper_id:
            fields.append(f'paper_id: "{paper_id}"')
        fields.extend(["---", "# Note"])
        path.write_text("\n".join(fields) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
