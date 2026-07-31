from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_readme_uses_versioned_install_and_preserves_attribution(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("dailypaper-skills.git#v1.2.0", readme)
        self.assertIn("npx --yes skills@1.5.20 add", readme)
        self.assertIn("Node.js | 22.20.0+", readme)
        self.assertIn("GitHub Releases", readme)
        self.assertIn("tag 尚未发布时", readme)
        self.assertNotIn("#codex%2Funified-harness", readme)
        self.assertIn("huangkiki/dailypaper-skills", readme)
        self.assertIn("[NOTICE](NOTICE)", readme)
        self.assertIn("Apache License 2.0", readme)

    def test_readme_documents_daily_semantics_and_safe_scheduling(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("arXiv `submittedDate` 窗口", readme)
        self.assertIn("根据 `totalResults` 完成分页", readme)
        self.assertIn("不会把截断结果当成完整", readme)
        self.assertIn("Codex Scheduled tasks", readme)
        self.assertIn("Claude Code Routines", readme)
        self.assertIn("关闭 isolated worktree", readme)
        self.assertIn("不是本 opinionated deployment 的开箱即用入口", readme)
        self.assertIn("不要在 Skill 外部执行 git pull/add/commit/push", readme)
        self.assertIn("远程显示 running 但本机没有对应 run 目录", readme)

    def test_readme_has_acceptance_recovery_and_lifecycle_guidance(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("### 安装后验收", readme)
        self.assertIn("## 常见问题", readme)
        self.assertIn("## 升级、回滚与卸载", readme)
        self.assertIn("git ls-remote", readme)
        self.assertIn("attention-required", readme)
        self.assertIn("npx --yes skills@1.5.20 remove", readme)

    def test_notice_identifies_the_upstream_derivative(self) -> None:
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")

        self.assertIn("https://github.com/huangkiki/dailypaper-skills", notice)
        self.assertIn("not an official release", notice)
        self.assertIn("Apache", notice)

    def test_release_changelog_has_current_and_first_release_entries(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("## [Unreleased]", changelog)
        self.assertIn("## [1.2.0] - 2026-07-31", changelog)
        self.assertIn("## [1.1.0] - 2026-07-30", changelog)
        self.assertIn("## [1.0.0] - 2026-07-30", changelog)
        self.assertIn("installation", changelog)
        self.assertIn("acceptance, arXiv window semantics", changelog)
        self.assertIn("[1.0.0]:", changelog)
        self.assertIn("[1.1.0]:", changelog)
        self.assertIn("[1.2.0]:", changelog)

    def test_release_workflow_is_tagged_main_only_and_regenerates_notes(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        ci_workflow = (
            ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('tags:\n      - "v*"', workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("merge-base --is-ancestor", workflow)
        for source in (workflow, ci_workflow):
            self.assertIn("-r requirements-dev.txt", source)
            self.assertIn("python3 tools/release_gate.py", source)
            self.assertIn("python3 tools/installer_smoke.py", source)
        self.assertIn("gh release create", workflow)
        self.assertIn("--generate-notes", workflow)

    def test_release_gate_contains_static_compile_test_and_drift_checks(self) -> None:
        gate = (ROOT / "tools" / "release_gate.py").read_text(encoding="utf-8")
        ruff_config = (ROOT / "ruff.toml").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements-dev.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("tools/sync_public_skills.py", gate)
        self.assertIn('"ruff"', gate)
        self.assertIn('"compileall"', gate)
        self.assertIn('"unittest"', gate)
        self.assertIn('("git", "diff", "--check")', gate)
        for rule in (
            "B023",
            "F821",
            "F841",
            "PLW1509",
            "S101",
            "S314",
            "S608",
        ):
            self.assertIn(f'"{rule}"', ruff_config)
        self.assertRegex(requirements, r"(?m)^ruff==\d+\.\d+\.\d+$")

        installer = (ROOT / "tools" / "installer_smoke.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('SKILLS_CLI = "skills@1.5.20"', installer)
        self.assertIn('"claude-code"', installer)
        self.assertIn('"codex"', installer)
        self.assertIn('"--copy"', installer)

    def test_release_notes_have_a_catch_all_category(self) -> None:
        release_config = (
            ROOT / ".github" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("title: Breaking changes", release_config)
        self.assertIn("title: Security and safety", release_config)
        self.assertIn('        - "*"', release_config)


if __name__ == "__main__":
    unittest.main()
