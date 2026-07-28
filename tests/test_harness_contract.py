import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
SUITE_ROOT = SKILLS_ROOT / "daily-papers"


class HarnessContractTests(unittest.TestCase):
    def test_only_public_skill_has_portable_metadata(self) -> None:
        skill_files = sorted(SKILLS_ROOT.glob("*/SKILL.md"))
        self.assertEqual(skill_files, [SUITE_ROOT / "SKILL.md"])

        text = skill_files[0].read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        frontmatter = text.split("---", 2)[1]
        keys = {
            line.split(":", 1)[0]
            for line in frontmatter.splitlines()
            if line and not line.startswith((" ", "\t")) and ":" in line
        }
        self.assertEqual(keys, {"name", "description"})
        self.assertFalse(
            any(SKILLS_ROOT.rglob("openai.yaml")),
            "portable skills must not depend on vendor sidecar metadata",
        )

    def test_canonical_inputs_route_through_one_public_skill(self) -> None:
        skill = (SUITE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        for prompt in (
            "今日论文推荐",
            "过去3天论文推荐",
            "过去一周论文推荐",
            "读一下这篇论文",
            "更新索引",
            "查看当前每日论文配置",
            "配置每日论文",
        ):
            self.assertIn(prompt, skill)
        for workflow in (
            "daily.md",
            "paper-reader.md",
            "generate-mocs.md",
            "configure.md",
        ):
            self.assertIn(f"workflows/{workflow}", skill)

    def test_internal_workflows_are_not_discoverable_skills(self) -> None:
        for workflow in ("fetch.md", "review.md", "notes.md"):
            path = SUITE_ROOT / "workflows" / workflow
            text = path.read_text(encoding="utf-8")
            self.assertFalse(text.startswith("---\n"), path)
            self.assertIn("只接受 `daily-papers` 父流程调用", text)

        fetch = (SUITE_ROOT / "workflows" / "fetch.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("不得创建 manifest、调用 acquire", fetch)
        notes = (SUITE_ROOT / "workflows" / "notes.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("禁止多个写入者并行修改", notes)
        self.assertIn("等待其", notes)

    def test_subagent_behavior_degrades_to_inline_execution(self) -> None:
        paper_reader = (SUITE_ROOT / "workflows" / "paper-reader.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("context: fork", paper_reader)
        self.assertNotIn("allowed-tools:", paper_reader)
        self.assertIn("恰好一个", paper_reader)
        self.assertIn("不支持 Subagent", paper_reader)
        self.assertIn("不得执行 Git add、commit 或 push", paper_reader)

    def test_configuration_workflow_is_state_safe(self) -> None:
        config_workflow = (SUITE_ROOT / "workflows" / "configure.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("config_manager.py", config_workflow)
        self.assertIn("状态是 `running`", config_workflow)
        self.assertIn("不得自动 rebase", config_workflow)
        self.assertIn(".dailypaper/config.json", config_workflow)
        self.assertIn(".dailypaper/tasks/daily-papers.json", config_workflow)

    def test_default_business_configuration_matches_unified_contract(self) -> None:
        config = json.loads(
            (SUITE_ROOT / "scripts" / "shared" / "user-config.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(config["paths"]["obsidian_vault"], ".")
        self.assertEqual(config["paths"]["paper_notes_folder"], "论文笔记")
        self.assertEqual(config["paths"]["daily_papers_folder"], "DailyPapers")
        self.assertEqual(
            config["daily_papers"]["arxiv_categories"],
            ["cs.RO", "cs.CV", "cs.AI", "cs.LG"],
        )
        self.assertEqual(config["daily_papers"]["top_n"], 30)

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
            ".dailypaper/tasks/daily-papers.json",
        ):
            self.assertIn(output, contract)
        for schema_item in ("# 🔪 今日锐评", "## 分流表", "🔥 必读"):
            self.assertIn(schema_item, contract)

    def test_daily_entry_owns_deterministic_vault_coordination(self) -> None:
        daily = (SUITE_ROOT / "workflows" / "daily.md").read_text(
            encoding="utf-8"
        )
        notes = (SUITE_ROOT / "workflows" / "notes.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("vault_coordination.py\" acquire", daily)
        self.assertIn('--harness "{HARNESS_ID}"', daily)
        self.assertIn("Claude Code 使用 `claude-code`", daily)
        self.assertIn("Codex 使用 `codex`", daily)
        self.assertIn("vault_coordination.py\" complete", notes)
        self.assertIn("不得自动 rebase", daily)

    def test_repository_contract_is_fixed(self) -> None:
        config = json.loads(
            (SUITE_ROOT / "scripts" / "shared" / "user-config.json").read_text(
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
        template = (SUITE_ROOT / "assets" / "paper-note-template.md").read_text(
            encoding="utf-8"
        )
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
