import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"


class HarnessContractTests(unittest.TestCase):
    def test_skill_metadata_is_compatible_with_both_harnesses(self) -> None:
        common_fields = {"name", "description"}
        for skill_path in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
            text = skill_path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), skill_path)
            frontmatter = text.split("---", 2)[1]
            keys = {
                line.split(":", 1)[0]
                for line in frontmatter.splitlines()
                if line and not line.startswith((" ", "\t")) and ":" in line
            }
            self.assertIn("name", keys, skill_path)
            self.assertIn("description", keys, skill_path)
            self.assertFalse(keys - common_fields, skill_path)

            codex_metadata = skill_path.parent / "agents" / "openai.yaml"
            self.assertTrue(codex_metadata.exists(), codex_metadata)
            self.assertIn(
                "allow_implicit_invocation:",
                codex_metadata.read_text(encoding="utf-8"),
            )

        for public_skill in ("daily-papers", "paper-reader", "generate-mocs"):
            metadata = (
                SKILLS_ROOT / public_skill / "agents" / "openai.yaml"
            ).read_text(encoding="utf-8")
            self.assertIn("allow_implicit_invocation: true", metadata)

    def test_canonical_daily_inputs_are_harness_neutral(self) -> None:
        daily_skill = (SKILLS_ROOT / "daily-papers" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        for prompt in (
            "今日论文推荐",
            "过去3天论文推荐",
            "过去一周论文推荐",
        ):
            self.assertIn(prompt, daily_skill)

        paper_reader = (
            SKILLS_ROOT / "paper-reader" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("context: fork", paper_reader)
        self.assertNotIn("allowed-tools:", paper_reader)

    def test_default_business_configuration_matches_unified_contract(self) -> None:
        config = json.loads(
            (SKILLS_ROOT / "_shared" / "user-config.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            config["paths"],
            {
                "obsidian_vault": ".",
                "paper_notes_folder": "论文笔记",
                "daily_papers_folder": "DailyPapers",
                "concepts_folder": "_概念",
                "inbox_folder": "_待整理",
                "zotero_db": "~/Zotero/zotero.sqlite",
                "zotero_storage": "~/Zotero/storage",
            },
        )
        self.assertEqual(
            config["daily_papers"]["keywords"],
            [
                "world model",
                "diffusion model",
                "embodied ai",
                "3d gaussian splatting",
                "4d gaussian splatting",
                "sim-to-real",
                "sim2real",
                "robot simulation",
            ],
        )
        self.assertEqual(
            config["daily_papers"]["arxiv_categories"],
            ["cs.RO", "cs.CV", "cs.AI", "cs.LG"],
        )
        self.assertEqual(
            config["daily_papers"]["domain_boost_keywords"],
            [
                "robot",
                "manipulation",
                "grasping",
                "locomotion",
                "navigation",
                "planning",
                "reinforcement learning",
                "policy learning",
                "visuomotor",
                "action prediction",
            ],
        )

    def test_contract_documents_stable_vault_outputs(self) -> None:
        contract = (REPO_ROOT / "HARNESS_CONTRACT.md").read_text(
            encoding="utf-8"
        )
        for output in (
            "DailyPapers/",
            "YYYY-MM-DD-论文推荐.md",
            ".history.json",
            "论文笔记/",
            "_概念/",
            "_待整理/",
            "<topic>/<MethodName>.md",
        ):
            self.assertIn(output, contract)

        for schema_item in (
            "# 🔪 今日锐评",
            "## 分流表",
            "🔥 必读",
            "👀 值得看",
            "💤 可跳过",
            ".dailypaper/tasks/daily-papers.json",
        ):
            self.assertIn(schema_item, contract)

    def test_daily_entry_uses_deterministic_vault_coordination(self) -> None:
        daily_skill = (SKILLS_ROOT / "daily-papers" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        notes_skill = (
            SKILLS_ROOT / "daily-papers-notes" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("vault_coordination.py\" acquire", daily_skill)
        self.assertIn('--harness "{HARNESS_ID}"', daily_skill)
        self.assertIn("Claude Code 使用 `claude-code`", daily_skill)
        self.assertIn("Codex 使用 `codex`", daily_skill)
        self.assertIn("vault_coordination.py\" complete", notes_skill)
        self.assertIn("不得自动 rebase", daily_skill)

    def test_repository_contract_is_fixed(self) -> None:
        config = json.loads(
            (SKILLS_ROOT / "_shared" / "user-config.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            config["repository"]["url"],
            "git@github.com:haoz0206/dailypaper-vault.git",
        )
        self.assertEqual(config["repository"]["branch"], "main")
        self.assertEqual(
            config["repository"]["task_state_file"],
            ".dailypaper/tasks/daily-papers.json",
        )
        self.assertFalse(config["automation"]["git_commit"])
        self.assertFalse(config["automation"]["git_push"])

    def test_paper_note_template_has_stable_sections(self) -> None:
        template = (
            SKILLS_ROOT / "paper-reader" / "assets" / "paper-note-template.md"
        ).read_text(encoding="utf-8")
        for section in (
            "## 元信息",
            "## 一句话总结",
            "## 核心贡献",
            "## 问题背景",
            "## 方法详解",
            "## 关键公式",
            "## 关键图表",
            "## 实验结果",
            "## 批判性思考",
            "## 关联笔记",
            "## 速查卡片",
        ):
            self.assertIn(section, template)


if __name__ == "__main__":
    unittest.main()
