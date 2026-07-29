import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SHARED_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "daily-papers"
    / "scripts"
    / "shared"
)
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import moc_builder
from moc_builder import (
    MOCApplyError,
    MOCConflictError,
    apply_moc_plans,
    build_tree_mocs,
    plan_tree_mocs,
)
from refresh_mocs import refresh_mocs


class MocBuilderTests(unittest.TestCase):
    def test_planning_snapshots_each_directory_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            topic = notes / "Robotics"
            topic.mkdir(parents=True)
            (topic / "Paper.md").write_text("# Paper\n", encoding="utf-8")
            original = moc_builder._directory_entries

            with patch.object(
                moc_builder,
                "_directory_entries",
                wraps=original,
            ) as scanner:
                plan_tree_mocs(
                    vault_root=vault,
                    root_dir=notes,
                    title_prefix="论文目录页",
                    intro="导航",
                )

            self.assertEqual(
                [call.args[0] for call in scanner.call_args_list],
                [notes, topic],
            )

    def test_tree_and_generated_output_limits_fail_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            notes.mkdir()
            (notes / "A").mkdir()
            (notes / "B").mkdir()

            with (
                patch.object(moc_builder, "MAX_MOC_DIRECTORIES", 2),
                self.assertRaisesRegex(MOCConflictError, "directory safety limit"),
            ):
                plan_tree_mocs(
                    vault_root=vault,
                    root_dir=notes,
                    title_prefix="论文目录页",
                    intro="导航",
                )
            self.assertEqual(list(notes.rglob("*.md")), [])

            with (
                patch.object(moc_builder, "MAX_DIRECTORY_ENTRIES", 1),
                self.assertRaisesRegex(MOCConflictError, "entry safety limit"),
            ):
                plan_tree_mocs(
                    vault_root=vault,
                    root_dir=notes,
                    title_prefix="论文目录页",
                    intro="导航",
                )

            (notes / "Paper.md").write_text("# Paper\n", encoding="utf-8")
            with (
                patch.object(moc_builder, "MAX_MOC_NOTES", 0),
                self.assertRaisesRegex(MOCConflictError, "note safety limit"),
            ):
                plan_tree_mocs(
                    vault_root=vault,
                    root_dir=notes,
                    title_prefix="论文目录页",
                    intro="导航",
                )

            with (
                patch.object(moc_builder, "MAX_MOC_BYTES", 32),
                self.assertRaisesRegex(MOCConflictError, "Generated MOC exceeds"),
            ):
                plan_tree_mocs(
                    vault_root=vault,
                    root_dir=notes,
                    title_prefix="论文目录页",
                    intro="导航",
                )

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
            root_moc = (notes / "论文笔记.md").read_text(encoding="utf-8")
            self.assertIn("- 根目录：`论文笔记`", root_moc)
            self.assertNotIn(str(vault), root_moc)

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

    def test_refresh_interface_combines_scopes_and_exact_changed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            concepts = notes / "_概念"
            topic = notes / "Robotics"
            concepts.mkdir(parents=True)
            topic.mkdir()
            (concepts / "Flow Matching.md").write_text(
                "# Flow Matching\n",
                encoding="utf-8",
            )
            (topic / "OpenVLA.md").write_text("# OpenVLA\n", encoding="utf-8")

            first = refresh_mocs(
                vault_root=vault,
                notes_root=notes,
                concepts_root=concepts,
                concepts_folder_name="_概念",
            )
            second = refresh_mocs(
                vault_root=vault,
                notes_root=notes,
                concepts_root=concepts,
                concepts_folder_name="_概念",
            )

            self.assertEqual(first["version"], 1)
            self.assertEqual(first["scope"], "all")
            self.assertEqual(set(first["summaries"]), {"concepts", "papers"})
            self.assertEqual(
                first["changed_paths"],
                [
                    "论文笔记/_概念/_概念.md",
                    "论文笔记/论文笔记.md",
                    "论文笔记/Robotics/Robotics.md",
                ],
            )
            self.assertEqual(second["changed_paths"], [])

    def test_moc_updates_are_atomic_and_preserve_existing_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            topic = notes / "Robotics"
            topic.mkdir(parents=True)
            (topic / "OpenVLA.md").write_text("# OpenVLA\n", encoding="utf-8")
            build_tree_mocs(
                vault_root=vault,
                root_dir=notes,
                title_prefix="论文目录页",
                intro="导航",
            )
            topic_moc = topic / "Robotics.md"
            topic_moc.chmod(0o640)
            (topic / "Octo.md").write_text("# Octo\n", encoding="utf-8")

            summary = build_tree_mocs(
                vault_root=vault,
                root_dir=notes,
                title_prefix="论文目录页",
                intro="导航",
            )

            self.assertIn("论文笔记/Robotics/Robotics.md", summary.changed_paths)
            self.assertEqual(topic_moc.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(topic.glob(".Robotics.md.*.tmp")), [])

    def test_generated_markdown_is_identical_across_machine_roots(self) -> None:
        outputs = []
        for _ in range(2):
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            vault = Path(temporary.name) / "vault"
            topic = vault / "论文笔记" / "Robotics"
            topic.mkdir(parents=True)
            (topic / "OpenVLA.md").write_text("# OpenVLA\n", encoding="utf-8")
            build_tree_mocs(
                vault_root=vault,
                root_dir=vault / "论文笔记",
                title_prefix="论文目录页",
                intro="导航",
            )
            outputs.append(
                (vault / "论文笔记" / "论文笔记.md").read_bytes()
            )

        self.assertEqual(outputs[0], outputs[1])

    def test_symlink_and_assets_directories_are_not_traversed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            vault = root / "vault"
            notes = vault / "论文笔记"
            assets = notes / "assets"
            outside = root / "outside"
            assets.mkdir(parents=True)
            outside.mkdir()
            (assets / "image-note.md").write_text("# not a note\n", encoding="utf-8")
            (outside / "secret.md").write_text("# secret\n", encoding="utf-8")
            (notes / "linked").symlink_to(outside, target_is_directory=True)

            summary = build_tree_mocs(
                vault_root=vault,
                root_dir=notes,
                title_prefix="论文目录页",
                intro="导航",
            )

            self.assertEqual(summary.total_directories, 1)
            self.assertFalse((assets / "assets.md").exists())
            self.assertFalse((outside / "outside.md").exists())
            root_moc = (notes / "论文笔记.md").read_text(encoding="utf-8")
            self.assertNotIn("assets", root_moc)
            self.assertNotIn("linked", root_moc)

    def test_refuses_user_owned_or_preexisting_dirty_moc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            notes.mkdir()
            moc = notes / "论文笔记.md"
            moc.write_text("# My handwritten index\n", encoding="utf-8")

            with self.assertRaisesRegex(MOCConflictError, "user-owned"):
                build_tree_mocs(
                    vault_root=vault,
                    root_dir=notes,
                    title_prefix="论文目录页",
                    intro="导航",
                )

            moc.write_text(
                "---\ngenerated_by: dailypaper-skills\n---\nold\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MOCConflictError, "dirty MOC"):
                build_tree_mocs(
                    vault_root=vault,
                    root_dir=notes,
                    title_prefix="论文目录页",
                    intro="导航",
                    protected_paths={"论文笔记/论文笔记.md"},
                )

    def test_refresh_plans_all_scopes_before_writing_any_moc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            concepts = notes / "_概念"
            concepts.mkdir(parents=True)
            (concepts / "Flow Matching.md").write_text(
                "# Flow Matching\n",
                encoding="utf-8",
            )
            (notes / "论文笔记.md").write_text(
                "# User-owned paper index\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(MOCConflictError, "user-owned"):
                refresh_mocs(
                    vault_root=vault,
                    notes_root=notes,
                    concepts_root=concepts,
                    concepts_folder_name="_概念",
                )

            self.assertFalse((concepts / "_概念.md").exists())
            self.assertEqual(
                (notes / "论文笔记.md").read_text(encoding="utf-8"),
                "# User-owned paper index\n",
            )

    def test_apply_rejects_target_changed_after_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            notes.mkdir()
            build_tree_mocs(
                vault_root=vault,
                root_dir=notes,
                title_prefix="论文目录页",
                intro="导航",
            )
            (notes / "Paper.md").write_text("# Paper\n", encoding="utf-8")
            plan = plan_tree_mocs(
                vault_root=vault,
                root_dir=notes,
                title_prefix="论文目录页",
                intro="导航",
            )
            target = notes / "论文笔记.md"
            target.write_text("# User edited after planning\n", encoding="utf-8")

            with self.assertRaisesRegex(MOCConflictError, "changed after planning"):
                apply_moc_plans([plan])

            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "# User edited after planning\n",
            )

    def test_apply_rejects_target_replaced_by_symlink_after_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            notes.mkdir()
            build_tree_mocs(
                vault_root=vault,
                root_dir=notes,
                title_prefix="论文目录页",
                intro="导航",
            )
            (notes / "Paper.md").write_text("# Paper\n", encoding="utf-8")
            plan = plan_tree_mocs(
                vault_root=vault,
                root_dir=notes,
                title_prefix="论文目录页",
                intro="导航",
            )
            target = notes / "论文笔记.md"
            outside = vault / "outside.md"
            outside.write_text("# user data\n", encoding="utf-8")
            target.unlink()
            target.symlink_to(outside)

            with self.assertRaisesRegex(MOCConflictError, "safely verify"):
                apply_moc_plans([plan])

            self.assertEqual(outside.read_text(encoding="utf-8"), "# user data\n")

    def test_apply_error_reports_already_durable_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes = vault / "论文笔记"
            topic = notes / "Robotics"
            topic.mkdir(parents=True)
            (topic / "Paper.md").write_text("# Paper\n", encoding="utf-8")
            plan = plan_tree_mocs(
                vault_root=vault,
                root_dir=notes,
                title_prefix="论文目录页",
                intro="导航",
            )
            real_write = moc_builder._atomic_write_text
            calls = 0

            def fail_second(path: Path, content: str, *, mode: int | None = None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated disk failure")
                return real_write(path, content, mode=mode)

            with patch.object(
                moc_builder,
                "_atomic_write_text",
                side_effect=fail_second,
            ):
                with self.assertRaises(MOCApplyError) as caught:
                    apply_moc_plans([plan])

            self.assertEqual(
                list(caught.exception.changed_paths),
                ["论文笔记/论文笔记.md"],
            )


if __name__ == "__main__":
    unittest.main()
