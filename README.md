# DailyPaper Skills

一套可由 Claude Code、Codex 和兼容 Agent Skills 安装器共同使用的论文发现、筛选、
阅读与 Obsidian 知识库工作流。

DailyPaper Skills 将每日论文任务收敛为几条稳定的自然语言入口：

```text
今日论文推荐
过去3天论文推荐
读一下这篇论文 https://arxiv.org/abs/2509.24527
更新索引
配置每日论文
```

同一套 Skill 会完成论文抓取、相关性评分、分级推荐、深度阅读、概念链接和 Obsidian
索引维护。Claude Code 与 Codex 共享相同的输入、输出和恢复协议，不需要切换 Git
分支或维护两份配置。

> [!IMPORTANT]
> 当前发行版是面向维护者个人 DailyPaper Vault 的 opinionated deployment：
> 协调远程固定为 `git@github.com:haoz0206/dailypaper-vault.git`，分支固定为
> `main`。这项约束用于跨机器原子协调，不是一个可在配置里覆盖的示例值。
> 非该 Vault 的使用者应先 fork 本仓库、替换固定仓库策略并运行完整测试，不能直接
> 对自己的 Vault 执行写入流程。

## 功能

- 从 Hugging Face Daily、Trending 和 arXiv 获取候选论文。
- 按可配置关键词、arXiv 分类和阈值评分，生成“必读 / 值得看 / 可跳过”推荐。
- 阅读 arXiv、DOI、本地 PDF 或显式 Zotero 输入，生成结构化论文笔记。
- 提取公式、Figure、Table、实验、方法局限和可复用概念。
- 使用稳定 `paper_id` 去重；歧义匹配不会选择或覆盖已有笔记。
- 自动维护论文与概念 MOC（Map of Content）。
- 使用远程 Task State、本地 Manifest 和精确 Git change set 支持异常恢复。
- Claude Code、Codex 和通用 Agent Skills 安装器共用四个可移植 Skill。

## 公共 Skills

| Skill | 用户入口 | 作用 |
| --- | --- | --- |
| `configure-dailypaper` | `配置每日论文` | 首次 onboarding、机器路径和共享研究配置 |
| `daily-papers` | `今日论文推荐` | 完整的抓取、点评和笔记流水线 |
| `paper-reader` | `读一下这篇论文 ...` | 独立阅读 arXiv、PDF、DOI 或 Zotero 论文 |
| `generate-mocs` | `更新索引` | 重新生成 Obsidian 论文与概念目录 |

`fetch`、`review` 和 `notes` 是 `daily-papers` 的内部阶段，不是可单独安装或直接调用
的 Skills。

## 环境要求

- Claude Code 或 Codex
- Python 3.10+
- Git 与可访问固定 Vault 远程的 SSH key
- Obsidian（输出本质是 Markdown，但使用了 wikilink 和 MOC）
- `poppler-utils`（PDF 文本与图片回退）
- Zotero（可选；普通 arXiv 和本地 PDF 输入不需要）

安装 Poppler：

```bash
# Debian / Ubuntu
sudo apt-get install poppler-utils

# macOS
brew install poppler
```

## 安装

首个正式版本使用固定 tag 安装，避免后续分支变更悄悄改变已经部署的工作流：

```bash
npx skills add \
  "https://github.com/haoz0206/dailypaper-skills.git#v1.0.0" \
  --skill configure-dailypaper daily-papers paper-reader generate-mocs \
  --agent claude-code codex \
  --global --yes
```

安装器会在 `.agents/skills` 保存通用 Skill，并为支持的 harness 创建对应入口。
如果文件系统不支持符号链接，可以追加 `--copy`；之后升级时应重新执行完整安装命令，
以免多个物理副本发生漂移。

开发 checkout 可以直接作为安装源：

```bash
npx skills add /workspace/dailypaper-skills \
  --skill configure-dailypaper daily-papers paper-reader generate-mocs \
  --agent claude-code codex \
  --yes
```

## 首次配置

安装后首先运行 `configure-dailypaper`，不要直接执行日报：

```text
配置 DailyPaper。本机 Vault 路径是 /workspace/dailypaper-vault
```

onboarding 会按顺序：

1. clone 或验证固定 Vault 仓库与 `main` 分支；
2. 完成可恢复的 Vault bootstrap；
3. 原子写入并回读本机配置；
4. 准备共享研究配置。

两层配置分别是：

```text
~/.config/dailypaper/config.json       # 本机：Vault 与可选 Zotero 绝对路径
<Vault>/.dailypaper/config.json        # 共享：研究范围、输出路径和自动化策略
```

Linux 服务器推荐使用 `/workspace/dailypaper-vault`。Mac 可以配置不同的本地 clone
路径；机器路径不进入共享配置，也不影响跨 harness 的配置指纹。

查看或修改研究范围：

```text
查看当前每日论文配置
把研究方向改成 VLA、robot learning 和 diffusion policy
只抓取 cs.RO、cs.CV 和 cs.AI，每天推荐 15 篇
```

主要共享配置项：

| 配置项 | 说明 |
| --- | --- |
| `daily_papers.arxiv_categories` | arXiv API 的硬分类范围 |
| `daily_papers.keywords` | 相关方向与加分关键词 |
| `daily_papers.negative_keywords` | 命中后排除的关键词 |
| `daily_papers.domain_boost_keywords` | 额外领域加分 |
| `daily_papers.min_score` | 候选最低分 |
| `daily_papers.top_n` | 单日推荐数量 |
| `automation.auto_refresh_indexes` | 写入后是否刷新 MOC |
| `automation.git_commit` / `git_push` | 独立 Skill 的可选发布策略，必须同步开关 |

Vault、Zotero、SSH key 和 harness 安装路径是机器配置，不允许写入共享 Vault 配置。
不存在运行时消费者的字段会被拒绝，而不是被静默保存。

## 使用

### 每日推荐

```text
今日论文推荐
过去3天论文推荐
过去一周论文推荐
```

抓取窗口是不可变的 Run intent。同一天重复相同窗口可以幂等恢复；同一天改用不同
窗口会返回 `intent-conflict`，不会复用另一份日报或自动取消现有任务。

### 阅读论文

```text
读一下这篇论文 https://arxiv.org/abs/2509.24527
快速看一下这篇论文 ~/Downloads/paper.pdf
批判性分析这篇论文 10.48550/arXiv.2509.24527
读一下 Zotero 里的 Diffusion Policy
```

Zotero 只在输入明确指向 Zotero 时启用，并且只查询临时只读 SQLite 快照。

### 刷新索引

```text
更新索引
```

`generate-mocs` 会保护运行前已经 dirty 的 MOC 和用户维护的 Markdown，不会用生成
内容覆盖它们。

## Vault 输出

稳定输出结构：

```text
<Vault>/
├── .dailypaper/
│   ├── config.json
│   ├── tasks/daily-papers.json
│   └── runs/                         # 本机恢复数据，不提交
├── DailyPapers/
│   ├── YYYY-MM-DD-论文推荐.md
│   └── .history.json
└── 论文笔记/
    ├── <topic>/<MethodName>.md
    ├── _概念/
    └── _待整理/
```

论文笔记模板见
[obsidian-templates/论文笔记模板.md](obsidian-templates/论文笔记模板.md)。

## 调度

本仓库提供可恢复的任务执行单元，不绑定某一种 scheduler。定时任务应让 Claude Code
或 Codex 调用同一个自然语言入口，并保证：

- 运行机器已经完成 onboarding；
- GitHub SSH key 在非交互环境可用；
- 每次调用都从公共 `daily-papers` 入口开始；
- 不在 scheduler 外部手写 `git pull`、锁、Manifest 或发布逻辑。

协调器会自行检查远程 Task State、同步 Vault、取得 writer lock，并在失败后从原
`run_id` 恢复。

## 安全与恢复

- 远程 Vault Task State 是跨机器所有权依据；本地 Manifest 只用于同机恢复。
- 运行中的任务不会因为 lease 或 idle 时间到期而被自动抢占。
- 存活的 guardian 只有在用户确认精确 `run_id` / `session_id` 后才能停止并恢复。
- 远程运行缺少本地恢复目录时，AI 必须展示精确 ID 并询问是否执行 CAS 取消。
- 未登记 dirty path、变化后的 artifact、意外远程 HEAD 或第三个 staged 版本都会
  阻止发布。
- Git 只暂存当前 Run/Session 登记的精确路径；不自动 rebase、不 force push。
- 网络、JSON、路径、Git blob、PDF 工具和生成文件都有大小、时间与 symlink 边界。

详细协议见 [HARNESS_CONTRACT.md](HARNESS_CONTRACT.md)，实现边界见
[ARCHITECTURE.md](ARCHITECTURE.md)。

## 架构

```text
configure-dailypaper ── onboarding / shared configuration transaction

daily-papers ── Run Coordinator
  ├── fetch
  ├── review
  └── notes ─────────────┐
                         ├── shared paper-reading core
paper-reader ─ Standalone┘

generate-mocs ─ Standalone Coordinator ─ deterministic MOC builder
```

源实现位于 `skills/daily-papers`。另外三个公共 Skill 由
`tools/sync_public_skills.py` 根据显式资源清单物化成可独立安装的自包含包。生成副本
的重复文件是发行隔离成本，不是第二套实现；不要直接编辑生成文件。

## 开发与验证

```bash
python3 tools/sync_public_skills.py --check
python3 -m compileall -q skills
python3 -m unittest discover -s tests -v
git diff --check
```

修改公共资源后先同步：

```bash
python3 tools/sync_public_skills.py
```

仓库 CI 在 Python 3.10 和 3.12 上执行相同门禁。维护者发布流程见
[RELEASING.md](RELEASING.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 项目来源与归属

本项目源自
[huangkiki/dailypaper-skills](https://github.com/huangkiki/dailypaper-skills)，
并保留原项目 Git 历史与贡献者记录。当前维护分支在原始 Claude Code 工作流之上
进行了大规模重构，包括：

- Claude Code / Codex / 通用 Agent Skills 的统一发行；
- 可恢复的跨机器 Vault 协调和精确 Git 发布；
- 独立安装包、配置 onboarding 和确定性状态协议；
- 统一论文身份、阅读核心、MOC、I/O、HTTP、进程和路径安全边界；
- 面向异常中断、并发、脏工作树和不可信输入的完整回归测试。

这不是原项目的官方发行版。原始创意、早期工作流和历史贡献归原作者及贡献者所有；
本 fork 的后续设计与实现由本仓库维护者负责。更多归属信息见 [NOTICE](NOTICE)。

## License

本项目继续使用 [Apache License 2.0](LICENSE)。使用 AI 生成的论文摘要、公式解释
和研究判断时，请始终回到原论文核验。
