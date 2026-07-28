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

from moc_builder import build_tree_mocs


class MocBuilderTests(unittest.TestCase):
    def test_reports_exact_changed_paths_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            topic = notes / "Robotics"
            topic.mkdir(parents=True)
            (topic / "OpenVLA.md").write_text("# OpenVLA\n", encoding="utf-8")

            first = build_tree_mocs(
                vault_root=vault,
                root_dir=notes,
                title_prefix="论文目录页",
                intro="导航",
            )
            second = build_tree_mocs(
                vault_root=vault,
                root_dir=notes,
                title_prefix="论文目录页",
                intro="导航",
            )

            self.assertEqual(
                first.changed_paths,
                ["论文笔记/论文笔记.md", "论文笔记/Robotics/Robotics.md"],
            )
            self.assertEqual(first.created_files, 2)
            self.assertEqual(second.changed_paths, [])
            self.assertEqual(second.unchanged_files, 2)

    def test_excluded_concept_tree_is_not_linked_from_paper_moc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            concepts = notes / "_概念"
            concepts.mkdir(parents=True)
            (concepts / "Flow Matching.md").write_text(
                "# Flow Matching\n",
                encoding="utf-8",
            )

            summary = build_tree_mocs(
                vault_root=vault,
                root_dir=notes,
                title_prefix="论文目录页",
                intro="导航",
                exclude_dir_names={"_概念"},
            )

            root_moc = (notes / "论文笔记.md").read_text(encoding="utf-8")
            self.assertNotIn("_概念", root_moc)
            self.assertEqual(summary.total_directories, 1)


if __name__ == "__main__":
    unittest.main()
