---
name: daily-papers
description: |
  DailyPaper suite for academic-paper discovery, daily recommendations, deep
  reading, Obsidian notes and MOCs, and shared configuration. Use for “今日论文推荐”,
  “过去3天论文推荐”, “过去一周论文推荐”, “读一下这篇论文（arXiv 或 PDF）”,
  “更新索引”, “查看当前每日论文配置”, “配置每日论文”, or equivalent requests.
---

# DailyPaper Suite

这是 Claude Code、Codex 和通用 Agent Skills 安装器共享的唯一公开 Skill。所有运行
依赖、内部 workflow、脚本、模板和参考资料都位于本目录，禁止依赖兄弟 Skill。

## 定位

将本 `SKILL.md` 所在目录解析为绝对路径 `SKILL_ROOT`。后续所有 workflow 和脚本
都从 `SKILL_ROOT` 解析，不依赖调用者当前目录：

- workflows：`{SKILL_ROOT}/workflows/`
- scripts：`{SKILL_ROOT}/scripts/`
- shared runtime：`{SKILL_ROOT}/scripts/shared/`
- references：`{SKILL_ROOT}/references/`
- assets：`{SKILL_ROOT}/assets/`

如果任何被引用文件缺失，停止并报告安装不完整；不得从仓库其他位置临时复制。

## 路由

根据用户请求读取并完整执行恰好一个公开 workflow：

- 日报、今日/多日论文推荐：
  `{SKILL_ROOT}/workflows/daily.md`
- 单篇论文、本地 PDF、arXiv 或显式 Zotero 阅读：
  `{SKILL_ROOT}/workflows/paper-reader.md`
- 更新 Obsidian 索引/MOC：
  `{SKILL_ROOT}/workflows/generate-mocs.md`
- 查看、初始化或修改 DailyPaper 配置：
  `{SKILL_ROOT}/workflows/configure.md`

日报 workflow 会自行按顺序读取内部 `fetch.md`、`review.md`、`notes.md`。这些内部
文件不是独立 Skill，不响应用户直接调用，也不自行取得任务所有权。

## 跨 Harness 契约

- Canonical 自然语言输入、Vault 路径、Markdown schema 和 Git 协调结果保持一致。
- Harness 身份只作为运行时协调字段；不得切换 Skills 分支或输出目录。
- 支持 Subagent 时按 workflow 明确委派；不支持时执行相同的 inline 流程。
- Vault 锁、run manifest、最终验证和 Git 发布始终由公开父 workflow 所有。
- 不访问与当前输入无关的 Zotero 数据库，不写 Harness 专属 sidecar。
