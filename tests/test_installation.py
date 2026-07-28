import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
PUBLIC_SKILLS = (
    "daily-papers",
    "paper-reader",
    "generate-mocs",
    "configure-dailypaper",
)


class InstallationTests(unittest.TestCase):
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
                    if relative == "scripts/shared/user-config.local.json":
                        self.assertTrue((root / relative).parent.is_dir())
                        continue
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
                    commands.append(
                        [
                            sys.executable,
                            str(
                                installed
                                / "scripts"
                                / "daily"
                                / "fetch_and_score.py"
                            ),
                            "--help",
                        ]
                    )
                if skill_name == "paper-reader":
                    commands.append(
                        [
                            sys.executable,
                            str(
                                installed
                                / "scripts"
                                / "paper-reader"
                                / "zotero_helper.py"
                            ),
                            "--help",
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
                if skill_name == "configure-dailypaper":
                    commands.append(
                        [
                            sys.executable,
                            str(
                                installed
                                / "scripts"
                                / "shared"
                                / "vault_coordination.py"
                            ),
                            "--help",
                        ]
                    )

                for command in commands:
                    with self.subTest(skill=skill_name, command=command):
                        result = subprocess.run(
                            command,
                            cwd=temp_dir,
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
