import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import sync_public_skills


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
PUBLIC_SKILLS = (
    "daily-papers",
    "paper-reader",
    "generate-mocs",
    "configure-dailypaper",
)


class InstallationTests(unittest.TestCase):
    def test_sync_refuses_generated_tree_symlinks_without_touching_targets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            generated_root = Path(temp_dir) / "generated"
            outside = Path(temp_dir) / "outside.md"
            outside.write_text("user-owned\n", encoding="utf-8")
            with patch.object(
                sync_public_skills,
                "SKILLS_ROOT",
                generated_root,
            ):
                sync_public_skills.sync(check=False)
                skill_file = generated_root / "paper-reader" / "SKILL.md"
                skill_file.unlink()
                skill_file.symlink_to(outside)

                problems = sync_public_skills.sync(check=True)
                self.assertTrue(
                    any(problem.startswith("unsafe:") for problem in problems)
                )
                with self.assertRaises(sync_public_skills.SyncError):
                    sync_public_skills.sync(check=False)

            self.assertEqual(
                outside.read_text(encoding="utf-8"),
                "user-owned\n",
            )

    def test_sync_reports_and_prunes_obsolete_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            generated_root = Path(temp_dir)
            with patch.object(
                sync_public_skills,
                "SKILLS_ROOT",
                generated_root,
            ):
                sync_public_skills.sync(check=False)
                obsolete = generated_root / "paper-reader" / "obsolete"
                obsolete.mkdir()

                problems = sync_public_skills.sync(check=True)
                self.assertTrue(
                    any(
                        problem.startswith("unexpected directory:")
                        for problem in problems
                    )
                )
                sync_public_skills.sync(check=False)

            self.assertFalse(obsolete.exists())

    def test_sync_bounds_generated_tree_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            generated_root = Path(temp_dir)
            with patch.object(
                sync_public_skills,
                "SKILLS_ROOT",
                generated_root,
            ):
                sync_public_skills.sync(check=False)
                with patch.object(
                    sync_public_skills,
                    "MAX_GENERATED_TREE_ENTRIES",
                    1,
                ):
                    problems = sync_public_skills.sync(check=True)
                    self.assertTrue(
                        any(
                            "exceeds the 1-entry limit" in problem
                            for problem in problems
                        )
                    )
                    with self.assertRaises(sync_public_skills.SyncError):
                        sync_public_skills.sync(check=False)

    def test_public_resource_manifests_reject_duplicate_paths(self) -> None:
        source = sync_public_skills.PUBLIC_SKILLS[0]
        duplicate = sync_public_skills.PublicSkill(
            name=source.name,
            description=source.description,
            workflow=source.workflow,
            resources=source.resources + (source.resources[0],),
        )
        with (
            patch.object(
                sync_public_skills,
                "PUBLIC_SKILLS",
                (duplicate,),
            ),
            self.assertRaisesRegex(
                sync_public_skills.SyncError,
                "Duplicate public resource",
            ),
        ):
            sync_public_skills.sync(check=True)

    def test_sync_rejects_canonical_resource_ancestor_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            suite = root / "suite"
            suite.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "workflow.md").write_text("workflow\n", encoding="utf-8")
            (outside / "resource.txt").write_text("resource\n", encoding="utf-8")
            (suite / "linked").symlink_to(outside, target_is_directory=True)
            skill = sync_public_skills.PublicSkill(
                name="test-skill",
                description="Test skill.",
                workflow="linked/workflow.md",
                resources=("linked/resource.txt",),
            )

            with (
                patch.object(sync_public_skills, "SUITE_ROOT", suite),
                patch.object(
                    sync_public_skills,
                    "SKILLS_ROOT",
                    root / "generated",
                ),
                patch.object(sync_public_skills, "PUBLIC_SKILLS", (skill,)),
                self.assertRaisesRegex(
                    sync_public_skills.SyncError,
                    "escapes the suite",
                ),
            ):
                sync_public_skills.sync(check=True)

    def test_sync_does_not_rewrite_identical_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            generated_root = Path(temp_dir)
            with (
                patch.object(
                    sync_public_skills,
                    "SKILLS_ROOT",
                    generated_root,
                ),
                patch.object(
                    sync_public_skills,
                    "atomic_write_bytes",
                    wraps=sync_public_skills.atomic_write_bytes,
                ) as write,
            ):
                sync_public_skills.sync(check=False)
                self.assertGreater(write.call_count, 0)
                write.reset_mock()

                sync_public_skills.sync(check=False)

            write.assert_not_called()

    def test_sync_prunes_obsolete_generated_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            generated_root = Path(temp_dir)
            with patch.object(
                sync_public_skills,
                "SKILLS_ROOT",
                generated_root,
            ):
                sync_public_skills.sync(check=False)
                obsolete = (
                    generated_root
                    / "paper-reader"
                    / "scripts"
                    / "paper-reader"
                    / "obsolete.py"
                )
                obsolete.write_text("obsolete\n", encoding="utf-8")

                sync_public_skills.sync(check=False)

            self.assertFalse(obsolete.exists())

    def test_sync_prunes_unsupported_per_skill_configuration_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            generated_root = Path(temp_dir)
            with patch.object(
                sync_public_skills,
                "SKILLS_ROOT",
                generated_root,
            ):
                sync_public_skills.sync(check=False)
                local_config = (
                    generated_root
                    / "paper-reader"
                    / "scripts"
                    / "shared"
                    / "user-config.local.json"
                )
                local_config.write_text(
                    '{"daily_papers":{"top_n":12}}\n',
                    encoding="utf-8",
                )

                problems = sync_public_skills.sync(check=True)
                self.assertTrue(
                    any("unexpected:" in problem for problem in problems)
                )
                sync_public_skills.sync(check=False)

            self.assertFalse(local_config.exists())

    def test_generated_public_skills_match_canonical_suite(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "sync_public_skills.py"),
                "--check",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_public_runtime_skills_bundle_only_the_lightweight_preflight(self) -> None:
        for skill_name in ("paper-reader", "generate-mocs"):
            root = SKILLS_ROOT / skill_name
            self.assertTrue(
                (root / "scripts" / "shared" / "runtime_context.py").is_file()
            )
            self.assertTrue(
                (root / "scripts" / "shared" / "active_run_guard.py").is_file()
            )
            self.assertFalse(
                (root / "scripts" / "configure" / "config_manager.py").exists()
            )

    def test_every_public_skill_bundles_the_single_configuration_schema(self) -> None:
        for skill_name in PUBLIC_SKILLS:
            self.assertTrue(
                (
                    SKILLS_ROOT
                    / skill_name
                    / "scripts"
                    / "shared"
                    / "config_schema.py"
                ).is_file(),
                skill_name,
            )

    def test_every_public_skill_bundles_strict_task_state_validation(self) -> None:
        for skill_name in PUBLIC_SKILLS:
            self.assertTrue(
                (
                    SKILLS_ROOT
                    / skill_name
                    / "scripts"
                    / "shared"
                    / "task_state.py"
                ).is_file(),
                skill_name,
            )

    def test_paper_workflows_bundle_the_single_identity_module(self) -> None:
        for skill_name in ("daily-papers", "paper-reader"):
            self.assertTrue(
                (
                    SKILLS_ROOT
                    / skill_name
                    / "scripts"
                    / "shared"
                    / "paper_identity.py"
                ).is_file(),
                skill_name,
            )

    def test_paper_reader_manifest_bundles_shared_reading_core(self) -> None:
        skill = next(
            item
            for item in sync_public_skills.PUBLIC_SKILLS
            if item.name == "paper-reader"
        )
        core = Path("references/paper-reader/reading-core.md")
        self.assertIn(core.as_posix(), skill.resources)
        self.assertIn(core, sync_public_skills._expected_files(skill))

    def test_every_documented_skill_root_dependency_is_bundled(self) -> None:
        for skill_name in PUBLIC_SKILLS:
            root = SKILLS_ROOT / skill_name
            referenced: set[str] = set()
            for document in [root / "SKILL.md", *root.rglob("*.md")]:
                text = document.read_text(encoding="utf-8")
                referenced.update(
                    re.findall(r"\{SKILL_ROOT\}/([A-Za-z0-9_./-]+)", text)
                )

            self.assertTrue(referenced, skill_name)
            for relative in sorted(referenced):
                with self.subTest(skill=skill_name, relative=relative):
                    self.assertTrue((root / relative).exists(), relative)

    def test_each_skill_runs_without_repository_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for skill_name in PUBLIC_SKILLS:
                installed = root / skill_name
                shutil.copytree(SKILLS_ROOT / skill_name, installed)
                commands = [
                    [
                        sys.executable,
                        "-m",
                        "compileall",
                        "-q",
                        str(installed),
                    ],
                    [
                        sys.executable,
                        str(
                            installed
                            / "scripts"
                            / "shared"
                            / "machine_config.py"
                        ),
                        "--help",
                    ],
                ]
                if skill_name == "daily-papers":
                    commands.extend(
                        [
                            [
                                sys.executable,
                                str(
                                    installed
                                    / "scripts"
                                    / "daily"
                                    / "fetch_and_score.py"
                                ),
                                "--help",
                            ],
                            [
                                sys.executable,
                                str(
                                    installed
                                    / "scripts"
                                    / "shared"
                                    / "run_coordinator.py"
                                ),
                                "--help",
                            ],
                        ]
                    )
                if skill_name == "paper-reader":
                    commands.extend(
                        [
                            [
                                sys.executable,
                                str(
                                    installed
                                    / "scripts"
                                    / "paper-reader"
                                    / "validate_paper_note.py"
                                ),
                                "--help",
                            ],
                            [
                            sys.executable,
                            str(
                                installed
                                / "scripts"
                                / "paper-reader"
                                / "zotero_helper.py"
                            ),
                            "--help",
                            ],
                        ]
                    )
                if (
                    installed / "scripts" / "configure" / "config_manager.py"
                ).exists():
                    commands.append(
                        [
                            sys.executable,
                            str(
                                installed
                                / "scripts"
                                / "configure"
                                / "config_manager.py"
                            ),
                            "--help",
                        ]
                    )
                if (installed / "scripts" / "shared" / "refresh_mocs.py").exists():
                    commands.append(
                        [
                            sys.executable,
                            str(
                                installed
                                / "scripts"
                                / "shared"
                                / "refresh_mocs.py"
                            ),
                            "--help",
                        ]
                    )
                if (
                    installed / "scripts" / "shared" / "runtime_context.py"
                ).exists():
                    commands.append(
                        [
                            sys.executable,
                            str(
                                installed
                                / "scripts"
                                / "shared"
                                / "runtime_context.py"
                            ),
                            "--help",
                        ]
                    )
                if (
                    installed / "scripts" / "shared" / "paper_identity.py"
                ).exists():
                    commands.append(
                        [
                            sys.executable,
                            str(
                                installed
                                / "scripts"
                                / "shared"
                                / "paper_identity.py"
                            ),
                            "--help",
                        ]
                    )
                if skill_name == "configure-dailypaper":
                    commands.extend(
                        (
                            [
                                sys.executable,
                                str(
                                    installed
                                    / "scripts"
                                    / "configure"
                                    / "onboard.py"
                                ),
                                "--help",
                            ],
                            [
                                sys.executable,
                                str(
                                    installed
                                    / "scripts"
                                    / "shared"
                                    / "vault_coordination.py"
                                ),
                                "--help",
                            ],
                        )
                    )

                for command in commands:
                    with self.subTest(skill=skill_name, command=command):
                        result = subprocess.run(
                            command,
                            cwd=temp_dir,
                            env={
                                **{
                                    key: value
                                    for key, value in os.environ.items()
                                    if key
                                    not in {
                                        "DAILYPAPER_CONFIG",
                                        "DAILYPAPER_MACHINE_CONFIG",
                                        "DAILYPAPER_VAULT",
                                        "DAILYPAPER_WORKSPACE",
                                    }
                                },
                                "DAILYPAPER_MACHINE_CONFIG": str(
                                    Path(temp_dir) / "missing-machine.json"
                                ),
                            },
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        self.assertEqual(
                            result.returncode,
                            0,
                            result.stdout + result.stderr,
                        )


if __name__ == "__main__":
    unittest.main()
