import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"


class HarnessContractTests(unittest.TestCase):
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

        codex_metadata = (
            SKILLS_ROOT / "daily-papers" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("allow_implicit_invocation: true", codex_metadata)

    def test_default_business_configuration_matches_main_contract(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
