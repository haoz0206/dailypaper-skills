from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_readme_uses_versioned_install_and_preserves_attribution(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("dailypaper-skills.git#v1.0.0", readme)
        self.assertNotIn("#codex%2Funified-harness", readme)
        self.assertIn("huangkiki/dailypaper-skills", readme)
        self.assertIn("[NOTICE](NOTICE)", readme)
        self.assertIn("Apache License 2.0", readme)

    def test_notice_identifies_the_upstream_derivative(self) -> None:
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")

        self.assertIn("https://github.com/huangkiki/dailypaper-skills", notice)
        self.assertIn("not an official release", notice)
        self.assertIn("Apache", notice)

    def test_first_release_has_a_changelog_entry(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("## [Unreleased]", changelog)
        self.assertIn("## [1.0.0] - 2026-07-29", changelog)
        self.assertIn("[1.0.0]:", changelog)

    def test_release_workflow_is_tagged_main_only_and_regenerates_notes(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('tags:\n      - "v*"', workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("merge-base --is-ancestor", workflow)
        self.assertIn("tools/sync_public_skills.py --check", workflow)
        self.assertIn("python3 -m unittest discover -s tests -v", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("--generate-notes", workflow)

    def test_release_notes_have_a_catch_all_category(self) -> None:
        release_config = (
            ROOT / ".github" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("title: Breaking changes", release_config)
        self.assertIn("title: Security and safety", release_config)
        self.assertIn('        - "*"', release_config)


if __name__ == "__main__":
    unittest.main()
