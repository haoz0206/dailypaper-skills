# dailypaper-skills 🗞️

这是一套可由 Claude Code 和 Codex 共用的每日论文 skills。

简单说，就是跟当前 harness 说一句话，它会帮你从每天的新论文里筛一轮，挑出值得看的，再把重点论文读完、写成 Obsidian 笔记。日常不用记一堆命令，基本就是：

```text
今日论文推荐
读一下这篇论文 https://arxiv.org/abs/2509.24527
```

如果你也有“每天想看看新论文，但不想每天从一堆页面里手动捞”的痛苦，这个仓库大概就是为这种场景准备的。

> **统一 harness 分支**
> 当前分支同时包含 Claude Code 可读取的 `SKILL.md` 和 Codex 使用的
> `agents/openai.yaml`，日常不需要为 harness 切换 Git 分支。

> **🧩 顺手推荐**
> 如果你主要在 Zotero 里读 PDF，可以搭配我另一个插件 [Zotero AI Sidebar](https://github.com/huangkiki/zotero-ai-sidebar)。这个插件是在 Zotero 右侧加一个 AI 侧栏，适合边读边问、点译、全文翻译、截图追问、写回 Zotero 笔记。
>
> ![Zotero AI Sidebar 阅读侧栏](https://raw.githubusercontent.com/huangkiki/zotero-ai-sidebar/master/docs/assets/zotero-real-overview.png)
>
> 我的习惯是：用这个仓库做每日筛选和 Obsidian 深度笔记；真正在 Zotero 里打开 PDF 精读时，用 Zotero AI Sidebar 做即时问答和点译。

> **🎬 视频演示**：[用 Claude Code 打造我的论文流水线](http://xhslink.com/o/1dhQCn40EWY)

## Claude Code / Codex 相同点与不同点

同一分支中的两个 harness 共享用户可见接口：

| 项目 | Claude Code | Codex |
| --- | --- | --- |
| 每日调用 | `今日论文推荐` | `今日论文推荐` |
| 多日调用 | `过去3天论文推荐` / `过去一周论文推荐` | 相同 |
| 单篇输入 | arXiv URL、本地 PDF、显式 Zotero 输入 | 相同 |
| 推荐文件 | `DailyPapers/YYYY-MM-DD-论文推荐.md` | 相同 |
| 论文笔记 | `论文笔记/<分类>/<MethodName>.md` | 相同 |
| 概念与待整理目录 | `_概念/`、`_待整理/` | 相同 |
| 默认研究配置 | embodied AI、world model、diffusion model | 相同 |
| Markdown 模板 | 推荐页、论文笔记、wikilink、MOC | 相同 |

仍然存在但不再需要分支隔离的 adapter 差异：

| 项目 | Claude Code | Codex |
| --- | --- | --- |
| Skill 安装 | `~/.claude/skills` | `~/.agents/skills` 或项目级 `.agents/skills` |
| 显式调用 | `/daily-papers` | `$daily-papers` |
| Skill 元数据 | `SKILL.md` 的共同 frontmatter | 同一目录下的 `agents/openai.yaml` |
| 协调身份 | `claude-code` | `codex` |
| Vault 默认根目录 | 当前 Git 仓库根目录 `"."` | 相同 |
| 中间数据 | `.dailypaper/runs/<run-id>/` 隔离 manifest | 相同 |
| 日报 Git 行为 | acquisition commit + 一个精确内容 commit | 相同 |
| 非 Zotero 单篇输入 | 不访问 Zotero，无法分类时写 `_待整理/` | 相同 |

两个 harness 从同一 checkout 读取 workflow，都会验证固定 Vault 远程，并通过任务
状态文档原子取得同日写入权。应统一使用自然语言入口；显式调用、安装路径和元数据仍
属于运行时 adapter 差异。完整契约见
[HARNESS_CONTRACT.md](HARNESS_CONTRACT.md)。

## ✨ 它会帮你做什么

- 抓 HuggingFace Daily、Trending 和 arXiv 上的新论文。
- 按你关心的方向打分，先筛掉明显不相关的。
- 生成每日推荐页，分成“必读 / 值得看 / 可跳过”。
- 对重点论文生成结构化笔记，包括方法、实验、公式、图表、局限和后续可追的问题。
- 自动写进 Obsidian，并维护论文目录页和概念索引。
- 如果你用 Zotero，也可以直接按标题搜索，或者按分类批量读论文。

最后在 Obsidian 里大概会长这样：

```text
/workspace/dailypaper-vault/        # 服务器上的本机固定路径示例
├── .dailypaper/
│   ├── config.json
│   ├── tasks/
│   │   └── daily-papers.json
│   └── runs/
├── DailyPapers/
│   └── YYYY-MM-DD-论文推荐.md
├── 论文笔记/
│   ├── 具体分类/
│   │   └── MethodName.md
│   ├── _概念/
│   │   └── ...概念笔记.md
│   └── _待整理/
└── ...
```

笔记模板可以看这里：[obsidian-templates/论文笔记模板.md](obsidian-templates/论文笔记模板.md)

## 🧭 怎么用

最常用的就是这几句：

```text
今日论文推荐
过去3天论文推荐
过去一周论文推荐
```

读单篇论文：

```text
读一下这篇论文 https://arxiv.org/abs/2509.24527
快速看一下这篇论文 ~/Downloads/paper.pdf
批判性分析这篇论文 ~/Downloads/paper.pdf
```

如果你配好了 Zotero，也可以这样：

```text
读一下 Zotero 里的 Diffusion Policy
批量读一下 Zotero 里 VLA 分类下的论文
```

目录页一般会自动刷新。如果你手动移动过笔记，或者觉得目录没同步，再补一句：

```text
更新索引
```

## ⚙️ 安装

需要这些东西：

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- Codex CLI
- [Obsidian](https://obsidian.md/)
- [Python 3.10+](https://www.python.org/)
- [`poppler-utils`](https://poppler.freedesktop.org/)，macOS 可以 `brew install poppler`
- [Zotero](https://www.zotero.org/)，可选，但如果你已经用 Zotero 管论文会很方便

服务器上的本机约定是：

```text
/workspace/dailypaper-skills
/workspace/dailypaper-vault
```

首次部署并把同一份 skills 安装到两个 harness 的用户级目录：

```bash
git clone --branch codex/unified-harness \
  git@github.com:haoz0206/dailypaper-skills.git \
  /workspace/dailypaper-skills
git clone \
  git@github.com:haoz0206/dailypaper-vault.git \
  /workspace/dailypaper-vault

export DAILYPAPER_VAULT=/workspace/dailypaper-vault
export DAILYPAPER_CONFIG=/workspace/dailypaper-vault/.dailypaper/config.json

python3 /workspace/dailypaper-skills/skills/_shared/vault_coordination.py \
  bootstrap --vault /workspace/dailypaper-vault

mkdir -p ~/.claude/skills
cp -R /workspace/dailypaper-skills/skills/. ~/.claude/skills/

mkdir -p ~/.agents/skills
cp -R /workspace/dailypaper-skills/skills/. ~/.agents/skills/
```

`bootstrap` 可以安全重复执行。对于当前这样的空 Vault 远程，它会创建并推送首个
`main` 提交，其中只有可移植的 `.dailypaper/config.json` 和忽略本地 run manifest
的 `.gitignore`；已有初始化提交时，它先 `pull --ff-only`，再只补齐缺失文件。

`/workspace/dailypaper-vault` 是 per-machine 配置：把这两个环境变量写进定时任务或
服务环境，不要把绝对路径提交到 Vault。已跟踪配置保持
`paths.obsidian_vault = "."`，Mac 可以 clone 到别的目录而不改变协调指纹。
非交互式任务还需要能使用服务器上的 GitHub SSH key。

从 `/workspace/dailypaper-vault` 内启动 Claude Code 或 Codex，即可让相同自然语言
输入写入同一个 Vault。不要为切换 harness 而切换 skills 仓库分支。

## 配置

安装后，两套 harness 各有一份相同的默认配置：

```text
~/.claude/skills/_shared/user-config.json
~/.agents/skills/_shared/user-config.json
```

推荐把共享配置提交到 Vault 的 `.dailypaper/config.json`，并在定时环境中设置：

```bash
export DAILYPAPER_VAULT=/workspace/dailypaper-vault
export DAILYPAPER_CONFIG="$DAILYPAPER_VAULT/.dailypaper/config.json"
```

你可以自己改，也可以直接让当前 harness 帮你改，比如：

```text
帮我配置 dailypaper-skills。我的 Obsidian 库在 XXX，研究方向是 robot learning、VLA、diffusion policy。
```

主要会改这几项：

| 配置项 | 说明 |
| --- | --- |
| `paths.obsidian_vault` | 你的 Obsidian 库路径 |
| `paths.zotero_db` | Zotero 数据库路径，不用 Zotero 可以留空 |
| `paths.zotero_storage` | Zotero 附件存储路径 |
| `daily_papers.keywords` | 你关心的研究方向，用来给论文加分 |
| `daily_papers.negative_keywords` | 你不想看的方向 |
| `daily_papers.domain_boost_keywords` | 额外加分的领域词 |
| `runtime.timezone` | 固定使用 `Asia/Shanghai` 判断日报日期 |
| `repository.url` | 固定 Vault 远程仓库 |
| `repository.task_state_file` | 跨机器/跨 harness 任务状态文档 |

Zotero 分类批量阅读不需要你另外写映射文件。只要 `paths.zotero_db` 和 `paths.zotero_storage` 配好，脚本会直接从 Zotero 分类树里查。

批量阅读守护进程在同时安装两个 CLI 的机器上必须显式指定 harness：

```bash
export PAPER_DAEMON_HARNESS=claude-code  # 或 codex
```

这样不会因为 `PATH` 顺序不同而调用错误的 CLI。

## 我一般怎么搭配 Zotero AI Sidebar

这个仓库和 [Zotero AI Sidebar](https://github.com/huangkiki/zotero-ai-sidebar) 不是替代关系，更像是两个不同位置的工具。

`dailypaper-skills` 更适合做这些事：

- 每天批量筛新论文。
- 把一篇论文完整读完，沉淀成 Obsidian 笔记。
- 顺手维护概念库和目录页。
- 对 Zotero 里某个分类的论文做批量整理。

Zotero AI Sidebar 更适合在读 PDF 的时候用：

- 看到一段看不顺，直接点译。
- 围绕当前论文提问，不用手动复制标题、摘要、选区。
- 截图问图表、公式或实验结果。
- 把回答写回 Zotero 子笔记。

所以我自己的工作流通常是：

1. 早上跑 `今日论文推荐`，先知道今天有没有值得看的。
2. 对特别重要的论文跑 `读一下这篇论文 ...`，生成 Obsidian 深度笔记。
3. 真正在 Zotero 里打开 PDF 细读时，用 Zotero AI Sidebar 做临场问答、点译和截图追问。
4. 一段时间后，对 Zotero 某个分类跑批量阅读，把已有文献库再整理进 Obsidian。

## 它内部大概怎么跑

`今日论文推荐` 其实会拆成三步：

1. **抓取**：从 HuggingFace Daily、Trending 和 arXiv API 抓候选论文，按你的关键词打分去重。
2. **点评**：当前 harness 读候选列表，分成“必读 / 值得看 / 可跳过”，写到 Obsidian 的 `DailyPapers/` 目录。
3. **笔记**：对“必读”论文逐篇调用 `paper-reader`，生成完整论文笔记，补概念库，再刷新目录页。

正常不用手动跑这三步。如果你只是想调试某一步，也可以说：

```text
跑一下论文抓取
跑一下论文点评
跑一下论文笔记
```

`读一下这篇论文 ...` 走的是 `paper-reader`。它支持 arXiv 链接、本地 PDF、Zotero 搜索和 Zotero 分类。生成笔记时会尽量从 arXiv HTML、项目主页和 PDF 里把图表找出来，写完后还会检查图片链接，坏掉的外链会尽量下载到本地。

`更新索引` 走的是 `generate-mocs`，会递归扫描论文笔记和概念库，生成 Obsidian 可用的目录页。

更多实现细节见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 🔒 Vault 远程协调

完整日报运行前会：

1. 幂等执行一次 bootstrap；空远程会建立 `main`、可移植配置和 run ignore。
2. 验证 Vault 的 `origin` 是
   `git@github.com:haoz0206/dailypaper-vault.git`，当前分支是 `main`。
3. 要求工作树干净并执行 `git pull --ff-only origin main`，随后重新加载配置。
4. 检查当天输出和 `.dailypaper/tasks/daily-papers.json`。
5. 用独立 acquisition commit/push 原子取得任务所有权。

只有抢锁 push 成功的 harness 才会继续。另一个 Claude Code/Codex 任务如果同时启动，
普通 push 会被拒绝并立即停止，不会 rebase、force push 或覆盖同日内容。

全部输出验证成功后，协调器只暂存 manifest 登记的路径以及任务状态文档，创建一个
内容 commit 并 push。`automation.git_commit` / `git_push` 仍默认关闭，它们只控制
独立调用 `paper-reader` 或 `generate-mocs` 时的 Git 行为，不控制完整日报协调协议。

## 仓库里有什么

```text
skills/
├── daily-papers/          # 每日推荐总入口
├── paper-reader/          # 单篇论文阅读与笔记生成
├── generate-mocs/         # Obsidian 目录页生成
├── daily-papers-fetch/    # 内部：抓取候选论文
├── daily-papers-review/   # 内部：生成推荐点评
├── daily-papers-notes/    # 内部：生成重点论文笔记
└── _shared/               # 共享配置和索引脚本

obsidian-templates/
└── 论文笔记模板.md
```

日常真正会直接用到的，基本就是：

- `daily-papers`
- `paper-reader`
- `generate-mocs`

另外几个是流水线内部拆出来的步骤，主要方便调试和重跑。

## FAQ

**可以一步跑完整流程吗？**

可以。直接说 `今日论文推荐`。

**不用 Zotero 可以吗？**

可以。每日推荐不依赖 Zotero；单篇阅读也支持 arXiv 链接和本地 PDF。Zotero 主要是用来搜索已有文献库、读取分类和批量处理。

**不用 Obsidian 可以吗？**

也可以。输出本质上就是 Markdown 文件。不过如果你想用 `[[双向链接]]`、图谱、概念库和目录页，Obsidian 会更顺手。

**能每天自动跑吗？**

可以。你可以让任一 harness 按系统环境配置定时任务，比如 macOS 的 `launchd` 或
Linux 的 `cron`。定时任务建议只触发 `今日论文推荐`，不要手写三条内部命令。

**生成的笔记能直接放进论文写作里吗？**

建议把它当成 related work 整理、阅读记录和追问提纲。AI 生成内容可能会有误，正式写作前还是要回到原文核验。

## 免责声明

这是我个人研究工作流的开源整理，不是一个保证完全稳定的产品。AI 生成的推荐、点评和笔记可能有事实错误、遗漏或误读，更适合作为辅助工具，而不是替代自己的研究判断。

如果你遇到问题，欢迎提 issue、PR，或者直接让 AI 和你一起改。

## 支持这个项目

如果这套 workflow 对你有帮助，欢迎点 Star、提 PR，或者分享你的适配版本。

[![Star History Chart](https://api.star-history.com/svg?repos=haoz0206/dailypaper-skills&type=Date)](https://www.star-history.com/#haoz0206/dailypaper-skills&Date)

## License

Apache-2.0. See [LICENSE](LICENSE).
