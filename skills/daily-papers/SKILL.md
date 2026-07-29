---
name: daily-papers
description: |
  Discover and review recent academic papers, generate a daily Obsidian
  recommendation, and create notes for must-read papers. Use for “今日论文推荐”,
  “过去3天论文推荐”, “过去一周论文推荐”, or equivalent daily-paper requests.
---

# DailyPaper Suite

这是 Claude Code、Codex 和通用 Agent Skills 安装器共享的日报公共 Skill。所有
运行依赖、内部 workflow、脚本、模板和参考资料都位于本目录，禁止依赖兄弟 Skill。

## 定位

将本 `SKILL.md` 所在目录解析为绝对路径 `SKILL_ROOT`。后续所有 workflow 和脚本
都从 `SKILL_ROOT` 解析，不依赖调用者当前目录：

- workflows：`{SKILL_ROOT}/workflows/`
- scripts：`{SKILL_ROOT}/scripts/`
- shared runtime：`{SKILL_ROOT}/scripts/shared/`
- references：`{SKILL_ROOT}/references/`
- assets：`{SKILL_ROOT}/assets/`

如果任何被引用文件缺失，停止并报告安装不完整；不得从仓库其他位置临时复制。

## 执行

读取并完整执行 `{SKILL_ROOT}/workflows/daily.md`。该 workflow 会自行按顺序读取
内部 `fetch.md`、`review.md` 和 `notes.md`；`notes.md` 直接调用非 discoverable
的 `references/paper-reader/reading-core.md`。内部 workflow 与 reference 不响应
用户直接调用，也不自行取得任务所有权。

手动论文阅读、MOC 刷新和配置请求分别由公共 `paper-reader`、`generate-mocs` 和
`configure-dailypaper` Skill 响应；不要从本 Skill 的 metadata 抢占这些请求。

## 跨 Harness 契约

- Canonical 自然语言输入、Vault 路径、Markdown schema 和 Git 协调结果保持一致。
- Harness 身份只作为运行时协调字段；不得切换 Skills 分支或输出目录。
- 支持 Subagent 时按 workflow 明确委派；不支持时执行相同的 inline 流程。
- Vault 锁、run manifest、最终验证和 Git 发布始终由公开父 workflow 所有。
- 不访问与当前输入无关的 Zotero 数据库，不写 Harness 专属 sidecar。
