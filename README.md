# dailypaper-skills 🗞️

一套面向 Codex 的每日论文筛选、精读和 Obsidian 笔记工作流。

它会抓取 HuggingFace Daily、Trending 和 arXiv 的新论文，按研究方向筛选并生成每日
推荐；对重点论文生成结构化笔记、概念链接和目录页。Zotero 是可选集成，不是每日报告
的依赖。

## 常用入口

Claude Code 和 Codex 共用同一组日常输入：

```text
今日论文推荐
过去3天论文推荐
过去一周论文推荐
```

读单篇论文和刷新索引：

```text
读一下这篇论文 https://arxiv.org/abs/2509.24527
快速看一下这篇论文 ~/Downloads/paper.pdf
批判性分析这篇论文 ~/Downloads/paper.pdf
更新索引
```

Codex 仍支持 `$daily-papers`、`$paper-reader`、`$generate-mocs` 作为显式适配器，
但它们不是用户侧的必要输入。完整的跨 harness 契约见
[HARNESS_CONTRACT.md](HARNESS_CONTRACT.md)。

## 输出

默认目录结构：

```text
ObsidianVault/
├── .agents/skills/                 # Codex 项目 Skill
├── .dailypaper/
│   ├── config.json                 # 可选的仓库级配置
│   └── runs/                       # 运行状态，已忽略
├── DailyPapers/
│   ├── YYYY-MM-DD-论文推荐.md
│   └── .history.json
└── 论文笔记/
    ├── _概念/
    ├── _待整理/
    └── .../*.md
```

论文笔记模板见 [obsidian-templates/论文笔记模板.md](obsidian-templates/论文笔记模板.md)。

## 安装

依赖：

- Codex CLI
- Python 3.10+
- `curl`
- `poppler-utils`（`apt install poppler-utils` / `brew install poppler`）
- Obsidian
- Zotero（可选）

### 项目级安装（推荐）

把 Skill 放进 Vault 仓库的 `.agents/skills`：

```bash
VAULT=~/ObsidianVault
mkdir -p "$VAULT/.agents/skills"
cp -R ./skills/. "$VAULT/.agents/skills/"

mkdir -p "$VAULT/DailyPapers" \
  "$VAULT/论文笔记/_概念/0-待分类" \
  "$VAULT/论文笔记/_待整理"
```

Codex 从当前目录向仓库根扫描 `.agents/skills`。因此从 Vault 内启动 Codex 或让
Codex Cloud checkout 该 Vault 时，都可以发现这些 Skill。

### 用户级安装

如果希望所有本地仓库都能使用：

```bash
mkdir -p ~/.agents/skills
cp -R ./skills/. ~/.agents/skills/
```

## 配置

默认配置在 Skill 根目录的 `_shared/user-config.json`：

- `paths.obsidian_vault` 默认是 `"."`；
- 相对 Vault 路径固定解析到当前 Git 仓库根目录；
- 默认时区是 `Asia/Shanghai`；
- Git commit/push 默认关闭；
- Zotero 路径仅在明确使用 Zotero 功能时访问。

本地个人配置可写入 `_shared/user-config.local.json`。也可以使用环境变量：

```bash
export DAILYPAPER_VAULT=/absolute/path/to/ObsidianVault
export DAILYPAPER_CONFIG=/absolute/path/to/dailypaper-config.json
```

Cloud 推荐把配置提交为 Vault 内的 `.dailypaper/config.json`，并在 environment 中设置：

```bash
export DAILYPAPER_CONFIG="$PWD/.dailypaper/config.json"
```

主要配置字段：

| 配置项 | 说明 |
| --- | --- |
| `paths.obsidian_vault` | Obsidian Vault 根目录 |
| `paths.paper_notes_folder` | 论文笔记目录 |
| `paths.daily_papers_folder` | 每日推荐目录 |
| `paths.concepts_folder` | 概念笔记目录 |
| `paths.inbox_folder` | 无法自动分类的论文目录 |
| `paths.zotero_db` | 可选 Zotero SQLite 路径 |
| `daily_papers.keywords` | 正向研究关键词 |
| `daily_papers.negative_keywords` | 排除关键词 |
| `daily_papers.domain_boost_keywords` | 领域加分词 |
| `runtime.timezone` | 日报日期使用的 IANA 时区 |

## 工作流

`今日论文推荐` 入口内部依次执行三个阶段：

1. **抓取与富化**：抓取 HF/arXiv，打分、去重并补充作者、机构、方法和图片信息。
2. **点评**：生成“必读 / 值得看 / 可跳过”的推荐页并更新 history。
3. **笔记与索引**：精读必读论文，补概念、回填链接并刷新 MOC。

入口会先创建唯一的 run manifest。候选数据和富化数据写入该 run 的隔离目录，不再共享
固定 `/tmp/daily_papers_*.json`，因此并发和失败重跑不会读取其他任务的中间文件。

三个内部阶段仍可用于调试：

- `$daily-papers-fetch`
- `$daily-papers-review`
- `$daily-papers-notes`

它们默认关闭隐式调用，正常使用只需说 `今日论文推荐`。需要排查 Codex Skill
路由时，才使用 `$daily-papers` 或内部阶段的显式名称。

## Git 与多设备同步

默认设置：

- `auto_refresh_indexes = true`
- `git_commit = false`
- `git_push = false`

打开 Git 自动化后，完整日报只在所有阶段验证成功时提交一次，只暂存本次运行记录的
文件，不使用 `git add -A`。建议开始自动任务前保持 Vault 工作树干净。

Mac 端阅读时：

```bash
git pull --ff-only
```

避免在 Mac 和服务器上同时修改 agent 管理的推荐页或论文笔记；个人批注最好放在独立
笔记中。

## Codex Cloud 与定时任务

Cloud environment 至少需要：

- checkout 包含 `.agents/skills` 的 Vault 仓库；
- setup 安装 Python 3.10+、`curl`、`poppler-utils`；
- agent 网络允许 `arxiv.org`、`export.arxiv.org`、`huggingface.co`；
- 配置 `DAILYPAPER_CONFIG` 和所需时区。

Cloud 默认交付完整 diff/PR，不应假定它能无人值守直推 `main`。需要完全自动化时，把
scheduler 作为外部 adapter：可使用 Codex Desktop Scheduled，或用 GitHub Actions
cron 触发并在质量检查后创建 PR。

Claude Code 与 Codex 的定时 prompt 都只需要：

```text
今日论文推荐
```

## Zotero

每日推荐不读取 Zotero。`$paper-reader` 接收 arXiv URL 或本地 PDF 时，也不会检查
Zotero SQLite；无法分类的非 Zotero 论文保存到 `论文笔记/_待整理/`。

只有明确搜索 Zotero、批量处理 Zotero 分类或运行 `paper_daemon.py` 时才需要 Zotero。
守护进程使用当前 Codex 非交互参数，并允许通过以下环境变量调整：

- `PAPER_DAEMON_CODEX_BIN`
- `PAPER_DAEMON_CODEX_MODEL`
- `PAPER_DAEMON_CODEX_ARGS`
- `PAPER_DAEMON_CODEX_SEARCH`
- `PAPER_DAEMON_CODEX_WORKDIR`
- `PAPER_DAEMON_STATE_DIR`（默认遵循 XDG，写入
  `~/.local/state/dailypaper`）

## 仓库结构

```text
skills/
├── daily-papers/
├── paper-reader/
├── generate-mocs/
├── daily-papers-fetch/
├── daily-papers-review/
├── daily-papers-notes/
└── _shared/

tests/
└── test_*.py
```

更多实现细节见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q skills tests
```

## License

Apache-2.0，见 [LICENSE](LICENSE)。
