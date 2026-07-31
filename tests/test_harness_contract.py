import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
SUITE_ROOT = SKILLS_ROOT / "daily-papers"
PUBLIC_SKILLS = {
    "daily-papers",
    "paper-reader",
    "generate-mocs",
    "configure-dailypaper",
}


class HarnessContractTests(unittest.TestCase):
    def test_public_skills_have_portable_metadata(self) -> None:
        skill_files = sorted(SKILLS_ROOT.glob("*/SKILL.md"))
        self.assertEqual(
            sorted(SKILLS_ROOT.rglob("SKILL.md")),
            skill_files,
            "internal workflows must not become installer-visible skills",
        )
        self.assertEqual(
            {path.parent.name for path in skill_files},
            PUBLIC_SKILLS,
        )
        for skill_path in skill_files:
            text = skill_path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"))
            frontmatter = text.split("---", 2)[1]
            keys = {
                line.split(":", 1)[0]
                for line in frontmatter.splitlines()
                if line and not line.startswith((" ", "\t")) and ":" in line
            }
            self.assertEqual(keys, {"name", "description"}, skill_path)

            name_match = re.search(r"(?m)^name:\s*(\S+)\s*$", frontmatter)
            self.assertIsNotNone(name_match, skill_path)
            name = name_match.group(1)
            self.assertEqual(name, skill_path.parent.name, skill_path)
            self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertLessEqual(len(name), 64, skill_path)

            description_match = re.search(
                r"(?ms)^description:\s*[|>]?\s*\n(?P<value>(?:[ \t]+.*\n?)+)",
                frontmatter,
            )
            self.assertIsNotNone(description_match, skill_path)
            description = " ".join(
                line.strip()
                for line in description_match.group("value").splitlines()
            ).strip()
            self.assertGreaterEqual(len(description), 1, skill_path)
            self.assertLessEqual(len(description), 1024, skill_path)

            self.assertLessEqual(
                len(text.splitlines()),
                500,
                f"{skill_path} should use progressive disclosure",
            )
        self.assertFalse(
            any(SKILLS_ROOT.rglob("openai.yaml")),
            "portable skills must not depend on vendor sidecar metadata",
        )

    def test_public_skill_prompts_have_explicit_context_budgets(self) -> None:
        max_bytes = {
            "daily-papers": 2500,
            "paper-reader": 8000,
            "generate-mocs": 3000,
            "configure-dailypaper": 10500,
        }
        for skill_name, limit in max_bytes.items():
            payload = (SKILLS_ROOT / skill_name / "SKILL.md").read_bytes()
            self.assertLessEqual(
                len(payload),
                limit,
                f"{skill_name} should use progressive disclosure",
            )

    def test_canonical_inputs_route_to_distinct_public_skills(self) -> None:
        daily = (SUITE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        for prompt in (
            "今日论文推荐",
            "过去3天论文推荐",
            "过去一周论文推荐",
        ):
            self.assertIn(prompt, daily)
        for prompt in (
            "读一下这篇论文",
            "更新索引",
            "查看当前每日论文配置",
            "配置每日论文",
        ):
            self.assertNotIn(prompt, daily)

        self.assertIn(
            "读一下这篇论文",
            (SKILLS_ROOT / "paper-reader" / "SKILL.md").read_text(
                encoding="utf-8"
            ),
        )
        self.assertIn(
            "更新索引",
            (SKILLS_ROOT / "generate-mocs" / "SKILL.md").read_text(
                encoding="utf-8"
            ),
        )
        configuration = (
            SKILLS_ROOT / "configure-dailypaper" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("配置每日论文", configuration)
        self.assertIn("查看当前每日论文配置", configuration)

    def test_internal_workflows_are_not_discoverable_skills(self) -> None:
        for workflow in ("fetch.md", "review.md", "notes.md"):
            path = SUITE_ROOT / "workflows" / workflow
            text = path.read_text(encoding="utf-8")
            self.assertFalse(text.startswith("---\n"), path)
            self.assertIn("只接受 `daily-papers` 父流程调用", text)

        fetch = (SUITE_ROOT / "workflows" / "fetch.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("不得创建、修改", fetch)
        self.assertIn("Vault Task State", fetch)
        notes = (SUITE_ROOT / "workflows" / "notes.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("禁止多个写入者并行修改", notes)
        self.assertIn("等待其", notes)

    def test_subagent_behavior_degrades_to_inline_execution(self) -> None:
        fetch = (SUITE_ROOT / "workflows" / "fetch.md").read_text(
            encoding="utf-8"
        )
        relevance_approval = (
            SUITE_ROOT / "references" / "relevance-approval.md"
        ).read_text(encoding="utf-8")
        paper_reader = (SUITE_ROOT / "workflows" / "paper-reader.md").read_text(
            encoding="utf-8"
        )
        reading_core = (
            SUITE_ROOT
            / "references"
            / "paper-reader"
            / "reading-core.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("context: fork", paper_reader)
        self.assertNotIn("allowed-tools:", paper_reader)
        self.assertIn("恰好一个", paper_reader)
        self.assertIn("不支持 Subagent", paper_reader)
        self.assertIn("只让它读取并执行 `reading-core.md`", paper_reader)
        self.assertNotIn("Subagent", reading_core)
        self.assertIn("candidate_approval.py", fetch)
        self.assertIn("references/relevance-approval.md", fetch)
        self.assertIn("不支持 Subagent", fetch)
        self.assertIn("最多同时运行 8 个", relevance_approval)
        self.assertIn("低成本、快速模型", relevance_approval)
        self.assertIn("approve", relevance_approval)
        self.assertIn("uncertain", relevance_approval)
        self.assertIn("reject", relevance_approval)
        self.assertIn("不得退回关键词硬过滤", relevance_approval)

    def test_paper_reading_core_has_two_real_adapters(self) -> None:
        standalone = (
            SUITE_ROOT / "workflows" / "paper-reader.md"
        ).read_text(encoding="utf-8")
        daily_notes = (
            SUITE_ROOT / "workflows" / "notes.md"
        ).read_text(encoding="utf-8")
        core_relative = "references/paper-reader/reading-core.md"

        self.assertIn(core_relative, standalone)
        self.assertIn(core_relative, daily_notes)
        self.assertNotIn("workflows/paper-reader.md", daily_notes)
        self.assertIn("standalone-session.md", standalone)
        self.assertIn("RUN_MANIFEST", daily_notes)

        core = (SUITE_ROOT / core_relative).read_text(encoding="utf-8")
        for contract_item in (
            "PAPER_INPUT",
            "READING_MODE",
            "OUTPUT_MODE",
            '"note_path"',
            '"concept_paths"',
            '"resource_paths"',
            '"quality"',
            "validate_paper_note.py",
        ):
            self.assertIn(contract_item, core)
        for lifecycle_term in (
            "standalone-session",
            "RUN_MANIFEST",
            "SESSION_ID",
            "standalone_coordinator.py",
            "run_coordinator.py",
            "refresh_mocs.py",
            "git add",
            "git commit",
            "git push",
        ):
            self.assertNotIn(lifecycle_term, core)

    def test_notes_delegate_concepts_and_backfill_to_single_owners(self) -> None:
        notes = (SUITE_ROOT / "workflows" / "notes.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("不得提前从推荐页", notes)
        self.assertNotIn("Step 1: 概念库补充", notes)
        self.assertNotIn("3a: 收集已有笔记", notes)
        self.assertNotIn("3b: 匹配论文与笔记", notes)
        self.assertNotIn("3c: 插入笔记链接", notes)
        self.assertNotIn("3d: 同步修正", notes)
        self.assertIn("backfill_links.py", notes)
        self.assertIn("唯一允许修改的文件是传入的推荐文件", notes)
        self.assertIn("父级二次验证", notes)

    def test_configuration_workflow_is_state_safe(self) -> None:
        config_workflow = (
            SKILLS_ROOT / "configure-dailypaper" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("config_manager.py", config_workflow)
        self.assertIn("machine_config.py", config_workflow)
        self.assertIn("scripts/configure/onboard.py", config_workflow)
        self.assertIn("安装后的首个入口", config_workflow)
        self.assertIn("远程日报是\n`running`", config_workflow)
        self.assertIn("不得自动 rebase", config_workflow)
        self.assertIn(".dailypaper/config.json", config_workflow)
        self.assertIn(".dailypaper/tasks/daily-papers.json", config_workflow)
        self.assertIn("config_manager.py\" \\\n  --vault \"{VAULT_PATH}\" prepare", config_workflow)
        self.assertIn("完整的可恢复发布事务", config_workflow)
        self.assertIn('--vault "{VAULT_PATH}" resume', config_workflow)
        self.assertIn("Harness 不得自行运行 `git add`", config_workflow)
        self.assertNotIn("git pull --ff-only", config_workflow)
        self.assertNotIn("git clone --branch", config_workflow)
        self.assertNotIn(
            'scripts/shared/vault_coordination.py" bootstrap',
            config_workflow,
        )

    def test_every_runtime_skill_requires_machine_onboarding(self) -> None:
        for skill_name in ("daily-papers", "paper-reader", "generate-mocs"):
            text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(
                encoding="utf-8"
            )
            if skill_name == "daily-papers":
                text += (SUITE_ROOT / "workflows" / "daily.md").read_text(
                    encoding="utf-8"
                )
            else:
                text += (
                    SKILLS_ROOT
                    / skill_name
                    / "references"
                    / "standalone-session.md"
                ).read_text(encoding="utf-8")
            self.assertIn("configure-dailypaper", text, skill_name)
            if skill_name == "daily-papers":
                self.assertIn("runtime_context.py", text, skill_name)
                self.assertIn("run_coordinator.py", text, skill_name)
            else:
                self.assertIn("standalone_coordinator.py", text, skill_name)
                self.assertIn("standalone-session.md", text, skill_name)

    def test_runtime_context_is_resolved_once_and_reused_by_daily_stages(self) -> None:
        daily = (SUITE_ROOT / "workflows" / "daily.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("RUNTIME_CONTEXT", daily)
        self.assertIn("RUNTIME_CONTEXT_FILE", daily)
        self.assertIn("返回的 `runtime_context`", daily)
        self.assertIn("不得再次读取或手工合并配置文件", daily)
        self.assertNotIn(
            'python3 "{SKILL_ROOT}/scripts/shared/runtime_context.py"',
            daily,
        )
        self.assertNotIn(
            'python3 "{SKILL_ROOT}/scripts/shared/vault_coordination.py" bootstrap',
            daily,
        )
        for workflow in ("fetch.md", "review.md", "notes.md"):
            text = (SUITE_ROOT / "workflows" / workflow).read_text(
                encoding="utf-8"
            )
            self.assertIn("RUNTIME_CONTEXT", text, workflow)
            self.assertIn("configuration_fingerprint", text, workflow)
            self.assertNotIn("user-config.local.json", text, workflow)
        fetch = (SUITE_ROOT / "workflows" / "fetch.md").read_text(
            encoding="utf-8"
        )
        self.assertIn('--runtime-context "{RUNTIME_CONTEXT_FILE}"', fetch)
        self.assertNotIn("--timezone", fetch)

    def test_standalone_writers_guard_fresh_remote_task_state(self) -> None:
        contract = (
            SUITE_ROOT / "references" / "standalone-session.md"
        ).read_text(encoding="utf-8")
        self.assertIn("fast-forward", contract)
        self.assertIn("--result success", contract)
        self.assertIn("--path", contract)
        for workflow in ("paper-reader.md", "generate-mocs.md"):
            text = (SUITE_ROOT / "workflows" / workflow).read_text(
                encoding="utf-8"
            )
            self.assertIn("standalone_coordinator.py", text, workflow)
            self.assertIn("standalone-session.md", text, workflow)
            self.assertIn("decision=ready", text, workflow)
            self.assertIn("完整读取", text, workflow)
            self.assertNotIn(
                'standalone_coordinator.py" start',
                text,
                workflow,
            )
            self.assertNotIn("逐一计算 SHA-256", text, workflow)
            self.assertNotIn("--prepare-standalone", text, workflow)
            self.assertNotIn("config_manager.py", text, workflow)

    def test_default_business_configuration_matches_unified_contract(self) -> None:
        config = json.loads(
            (SUITE_ROOT / "scripts" / "shared" / "defaults.json").read_text(
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
        self.assertIn("run_coordinator.py\" start", daily)
        self.assertIn('--harness "{HARNESS_ID}"', daily)
        self.assertIn("Claude Code 使用 `claude-code`", daily)
        self.assertIn("Codex 使用 `codex`", daily)
        self.assertIn('submit \\\n     "{RUN_MANIFEST}" --report', daily)
        self.assertIn("notes-progress-<序号>.json", notes)
        self.assertIn("validate_paper_note.py", notes)
        self.assertNotIn("删除文件并重新生成", notes)
        self.assertIn("cancel-confirmation-required", daily)
        self.assertIn("exact `run_id`", daily)
        self.assertIn("不得自动 rebase", daily)

    def test_internal_stages_share_one_scoped_report_contract(self) -> None:
        contract = (SUITE_ROOT / "references" / "stage-report.md").read_text(
            encoding="utf-8"
        )
        self.assertIn('"version": 1', contract)
        self.assertIn('"scope": "run"', contract)
        self.assertIn("changed_paths", contract)
        self.assertIn("--report", contract)
        for workflow in ("fetch.md", "review.md", "notes.md"):
            text = (SUITE_ROOT / "workflows" / workflow).read_text(
                encoding="utf-8"
            )
            self.assertIn("stage-report.md", text)
            self.assertIn('"version": 1', text)

    def test_repository_contract_is_fixed(self) -> None:
        config = json.loads(
            (SUITE_ROOT / "scripts" / "shared" / "defaults.json").read_text(
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

    def test_documented_obsidian_template_matches_canonical_asset(self) -> None:
        self.assertEqual(
            (
                REPO_ROOT / "obsidian-templates" / "论文笔记模板.md"
            ).read_bytes(),
            (
                SUITE_ROOT / "assets" / "paper-note-template.md"
            ).read_bytes(),
        )

    def test_deprecated_local_overlays_are_not_ignored(self) -> None:
        ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        for deprecated in (
            "collection_mapping.json",
            "user-config.local.json",
            ".agents/skills/daily-papers",
            ".claude/skills/daily-papers",
        ):
            self.assertNotIn(deprecated, ignore)


if __name__ == "__main__":
    unittest.main()
