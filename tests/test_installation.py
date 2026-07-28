import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_ROOT = REPO_ROOT / "skills" / "daily-papers"


class InstallationTests(unittest.TestCase):
    def test_every_documented_skill_root_dependency_is_bundled(self) -> None:
        referenced: set[str] = set()
        for document in [SUITE_ROOT / "SKILL.md", *SUITE_ROOT.rglob("*.md")]:
            text = document.read_text(encoding="utf-8")
            referenced.update(
                re.findall(r"\{SKILL_ROOT\}/([A-Za-z0-9_./-]+)", text)
            )

        self.assertTrue(referenced)
        for relative in sorted(referenced):
            with self.subTest(relative=relative):
                if relative == "scripts/shared/user-config.local.json":
                    self.assertTrue((SUITE_ROOT / relative).parent.is_dir())
                    continue
                self.assertTrue((SUITE_ROOT / relative).exists(), relative)

    def test_copied_skill_runs_without_repository_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            installed = Path(temp_dir) / ".agents" / "skills" / "daily-papers"
            shutil.copytree(SUITE_ROOT, installed)

            commands = (
                [
                    sys.executable,
                    str(installed / "scripts" / "daily" / "fetch_and_score.py"),
                    "--help",
                ],
                [
                    sys.executable,
                    str(
                        installed
                        / "scripts"
                        / "configure"
                        / "config_manager.py"
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
                [
                    sys.executable,
                    "-m",
                    "compileall",
                    "-q",
                    str(installed),
                ],
            )
            for command in commands:
                with self.subTest(command=command):
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
