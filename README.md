# dailypaper-skills 🗞️

这是一套可由 Claude Code 和 Codex 共用的每日论文 skills。

简单说，就是跟当前 harness 说一句话，它会帮你从每天的新论文里筛一轮，挑出值得看的，再把重点论文读完、写成 Obsidian 笔记。日常不用记一堆命令，基本就是：

```text
今日论文推荐
读一下这篇论文 https://arxiv.org/abs/2509.24527
```

如果你也有“每天想看看新论文，但不想每天从一堆页面里手动捞”的痛苦，这个仓库大概就是为这种场景准备的。

> **统一 Harness 分支**
> 当前分支只使用 Agent Skills 的可移植 `SKILL.md` 接口。Claude Code、Codex
> 和兼容的第三方安装器读取同一套 workflow，日常不需要切换 Git 分支。

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
| Skill 安装 | `npx skills add ... -a claude-code` | `npx skills add ... -a codex` |
| 显式调用 | `/daily-papers` | `$daily-papers` |
| Skill 元数据 | 标准 `SKILL.md` | 同一份标准 `SKILL.md` |
| 协调身份 | `claude-code` | `codex` |
| Vault 默认根目录 | 当前 Git 仓库根目录 `"."` | 相同 |
| 中间数据 | `.dailypaper/runs/<run-id>/` 隔离 manifest | 相同 |
| 日报 Git 行为 | acquisition commit + 一个精确内容 commit | 相同 |
| 非 Zotero 单篇输入 | 不访问 Zotero，无法分类时写 `_待整理/` | 相同 |

两个 harness 从同一 checkout 读取 workflow，都会验证固定 Vault 远程，并通过任务
状态文档原子取得同日写入权。应统一使用自然语言入口；显式调用、安装目标和 CLI
参数仍属于运行时 adapter 差异。完整契约见
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

查看或调整整套 workflow 的共享配置：

```text
查看当前每日论文配置
把研究方向改成 VLA、robot learning 和 diffusion policy
只抓取 cs.RO、cs.CV 和 cs.AI，每天推荐 15 篇
```

这些请求由 `daily-papers` 内部的配置 workflow 处理。它会先同步 Vault、检查是否
有正在运行的日报，再预览和校验 `.dailypaper/config.json`；配置更新只提交这个
文件。

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

首次部署时先准备持久化 Vault，再用通用安装器把唯一公开的 `daily-papers` Skill
安装到两个 harness：

```bash
git clone \
  git@github.com:haoz0206/dailypaper-vault.git \
  /workspace/dailypaper-vault

export DAILYPAPER_VAULT=/workspace/dailypaper-vault
export DAILYPAPER_CONFIG=/workspace/dailypaper-vault/.dailypaper/config.json

npx skills add \
  "https://github.com/haoz0206/dailypaper-skills.git#codex%2Funified-harness" \
  --skill daily-papers \
  --agent claude-code codex \
  --global --copy --yes
```

这里显式使用 `--copy`，让每个 harness 得到完整、自包含的目录，不依赖仓库外部
兄弟 Skill 或跨目录符号链接。首次执行 `今日论文推荐` 时会自动、幂等地 bootstrap
Vault。空远程会创建并推送首个 `main` 提交，其中只有可移植配置和忽略本地 run
manifest 的 `.gitignore`；已有提交时会先 `pull --ff-only`。

如果是在开发 checkout 中验证尚未发布的改动，可以把安装源换成本地路径：

```bash
npx skills add /workspace/dailypaper-skills \
  --skill daily-papers \
  --agent claude-code codex \
  --copy --yes
```

`/workspace/dailypaper-vault` 是 per-machine 配置：把这两个环境变量写进定时任务或
服务环境，不要把绝对路径提交到 Vault。已跟踪配置保持
`paths.obsidian_vault = "."`，Mac 可以 clone 到别的目录而不改变协调指纹。
非交互式任务还需要能使用服务器上的 GitHub SSH key。

从 `/workspace/dailypaper-vault` 内启动 Claude Code 或 Codex，即可让相同自然语言
输入写入同一个 Vault。不要为切换 harness 而切换 skills 仓库分支。

## 配置

安装后，完整默认配置位于各自安装的 suite 内，例如：

```text
~/.claude/skills/daily-papers/scripts/shared/user-config.json
~/.agents/skills/daily-papers/scripts/shared/user-config.json
```

不要直接把个人设置写进安装包默认值。推荐把共享配置提交到 Vault 的
`.dailypaper/config.json`，并在定时环境中设置：

```bash
export DAILYPAPER_VAULT=/workspace/dailypaper-vault
export DAILYPAPER_CONFIG="$DAILYPAPER_VAULT/.dailypaper/config.json"
```

研究范围等共享设置通过 `daily-papers` 的配置入口修改，例如：

```text
查看当前每日论文配置
把研究方向改成 robot learning、VLA、diffusion policy
只检索 cs.RO、cs.CV 和 cs.AI，每天推荐 15 篇
```

配置 workflow 只管理当前实现真正支持的共享字段，并在原子替换文件前再次检查
远程任务状态。主要字段是：

| 配置项 | 说明 |
| --- | --- |
| `daily_papers.arxiv_categories` | arXiv API 的硬分类范围，分类之间使用 OR |
| `daily_papers.keywords` | 你关心的研究方向，用来给论文加分 |
| `daily_papers.negative_keywords` | 标题或摘要命中后直接排除 |
| `daily_papers.domain_boost_keywords` | 额外加分的领域词 |
| `daily_papers.min_score` | 进入最终候选列表的最低分 |
| `daily_papers.top_n` | 每天保留数量，多日调用当前会乘以天数 |
| `automation.auto_refresh_indexes` | 生成内容后是否刷新 Obsidian MOC |

`DAILYPAPER_VAULT`、Zotero 数据库位置、SSH key 和 Harness 安装路径都是
per-machine 设置，不由共享配置 workflow 写入 Vault。共享配置中的
`paths.obsidian_vault` 始终保持 `"."`。

严格只抓某个 calendar day、自定义 arXiv API query 或 `max_results` 当前还不是
受支持配置；配置 workflow 会报告需要修改实现，而不会写入一个无效字段。

Zotero 分类批量阅读不需要另外写映射文件。需要时，在当前 harness 安装目录的
`scripts/shared/user-config.local.json` 中配置 `paths.zotero_db` 和
`paths.zotero_storage`；不要把这两个机器路径写进 Vault 的共享配置。不使用
Zotero 时无需创建该文件，日报和普通 arXiv/本地 PDF 阅读都不会打开 SQLite。

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
3. **笔记**：对“必读”论文逐篇执行内部 paper-reader workflow，生成完整论文笔记，补概念库，再刷新目录页。

三个阶段是 suite 内部实现，不是独立安装或用户调用入口。维护者单阶段调试时也
必须提供已经取得任务所有权的 run manifest。

`读一下这篇论文 ...` 走内部 paper-reader workflow，支持 arXiv 链接、本地 PDF、
显式 Zotero 搜索和 Zotero 分类；普通 arXiv/PDF 输入不会访问 Zotero SQLite。
`更新索引` 走内部 MOC workflow。`查看/修改每日论文配置` 走内部配置 workflow，
字段白名单、关键词规范化、冲突检查、arXiv 分类、阈值和原子写入均由确定性脚本
校验；存在 `running` 日报时拒绝修改。

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
└── daily-papers/
    ├── SKILL.md            # 唯一公开、可安装入口
    ├── workflows/          # 日报、阅读、MOC、配置及三个内部阶段
    ├── scripts/            # 确定性实现与 shared runtime
    ├── assets/             # 论文笔记模板
    └── references/         # 阅读质量与 Zotero 参考

obsidian-templates/
└── 论文笔记模板.md
```

安装器只会发现 `daily-papers` 一个 Skill。它根据自然语言路由到日报、单篇阅读、
索引或配置 workflow；抓取、点评和笔记阶段要求父流程提供已取得任务所有权的
`RUN_MANIFEST`。

`paper-reader` 不依赖厂商专属的 fork 配置：当前 Harness 支持 Subagent 时，prompt
会要求把每篇论文委派给恰好一个 Subagent 并等待完成；不支持时执行相同的 inline
流程。Vault 锁、manifest 和 Git 发布始终由父流程负责。

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
