# Architecture

这份文档记录各模块的实现逻辑，方便想改代码或理解内部机制的人参考。

## 整体架构

```
用户说一句话
    │
    ├─ configure-dailypaper（安装后首个入口；本机路径 + 共享研究配置）
    ├─ daily-papers（每日推荐）
    │    └─ workflows/daily.md
    │         ├─ fetch.md（Python，零 token）
    │         ├─ review.md（当前 agent 点评）
    │         └─ notes.md（当前 agent + paper-reader workflow）
    ├─ paper-reader（手动读单篇论文 / Zotero）
    └─ generate-mocs（手动刷新 Obsidian 索引）
```

`skills/daily-papers/` 是完整日报能力的自包含深模块。`paper-reader`、
`generate-mocs` 和 `configure-dailypaper` 是公开、可独立安装的聚焦入口；
它们由 `tools/sync_public_skills.py` 从日报模块中的规范 workflow 和最小运行资源
生成，避免复制实现发生漂移。安装器发现四个公共 `SKILL.md`，而 fetch、review、
notes 三个流水线阶段仍然只作为 `daily-papers` 的内部资源。

三步流水线的设计主要是为了控制单次上下文长度。入口首先创建独立
`RunManifest`，三个阶段通过 manifest 中的运行级 JSON 路径传递数据，避免并发和失败
重跑读到其他任务的文件。

安装后必须先运行 `configure-dailypaper`。服务器推荐使用
`/workspace/dailypaper-vault`；该绝对路径默认写入本机
`~/.config/dailypaper/config.json`，不会进入 Git。四个公共 Skill 启动时都会验证
机器配置；缺失或无效时停止并要求配置，不会把当前工作目录当成 Vault。
`DAILYPAPER_VAULT` 只作为临时显式覆盖。

Vault 内跟踪的
`.dailypaper/config.json` 使用相对值 `"."`，从而允许 Mac、服务器和其他 harness
在不同 clone 路径上共享同一配置指纹。

首次运行前，`vault_coordination.py bootstrap` 会处理空远程：验证固定
`origin`、`main` 和干净工作树，创建可移植配置及忽略 `.dailypaper/runs/` 的
`.gitignore`，生成首个初始化提交并普通 push。已有提交时该命令先 fast-forward
pull，并且只有缺失 bootstrap 文件时才再提交，所以可幂等调用。

---

## Step 1: fetch workflow

**纯 Python，不消耗模型 token。**

### 1.1 抓取 + 打分（fetch_and_score.py）

数据源：
- HuggingFace Daily Papers API：`https://huggingface.co/api/daily_papers?date=YYYY-MM-DD`
- HuggingFace Trending API：`https://huggingface.co/api/daily_papers?sort=trending`
- arXiv API：`https://export.arxiv.org/api/query`，分类来自
  `daily_papers.arxiv_categories` 配置

打分规则：
- 命中 `negative_keywords` → 直接 -999 排除
- 命中 `keywords`：标题 +3，摘要 +1
- 命中 `domain_boost_keywords`：+1~2
- Trending 加分：根据 upvotes 分档（5 / 10 / 20），相关论文 +1~3，不相关的只有 20+ upvotes 才加分

去重：
- 按 arXiv ID 合并，保留高分
- 单天模式：跟 `.history.json` 交叉去重
- 周末模式：保留 5+ upvotes 的热门论文作为"再推荐"
- 多天模式（days > 1）：跳过历史去重
- 候选不足 20 篇时从历史回补

输出：`{RUN_DIR}/candidates.json`

### 1.2 元数据富化（enrich_papers.py）

用 `asyncio + curl` 并发（Semaphore=10）请求 arXiv 页面，每个请求 30s 超时 + 指数退避重试。

提取内容：
- 从 arXiv HTML 提取：首图 URL、作者、机构、章节标题、图表标题、方法名、是否有真机实验
- HTML 取不到时 fallback 到 `pdftotext` 提取机构
- 再 fallback 到 arXiv abs 页面的 `<meta>` 标签

输出：`{RUN_DIR}/enriched.json`

---

## Step 2: review workflow

**当前 agent 主导，读候选列表写点评。**

### 2.1 扫描已有笔记

扫描 Obsidian 的论文笔记和概念库目录，把候选论文跟已有笔记做匹配（方法名 / 标题模糊匹配），标记 `has_existing_note`。

### 2.2 写锐评

当前 agent 以"毒舌但有料的资深研究员"角色点评每篇论文：
- 分流表：🔥 必读 / 👀 值得看 / 💤 可跳过
- 每篇包含：作者、机构、链接、来源、核心方法（带 `[[概念]]` 链接）、对比方法、借鉴意义、锐评
- 已有笔记的论文走简化格式
- 跟用户方向完全无关的论文可以跳过，列出跳过原因

硬性约束：
- 不能凭空说"只有仿真"——必须检查 `has_real_world` 字段
- 不能说某篇是"山寨"——除非有具体方法论证据
- 不确定的信息必须注明"摘要未提及"

### 2.3 保存

- 写入 `{DAILY_PAPERS_PATH}/YYYY-MM-DD-论文推荐.md`
- 更新 `.history.json`：追加今日推荐的 arXiv ID + 标题，只保留最近 30 天
- 将变更路径加入当前 run；不在本阶段提交

---

## Step 3: notes workflow

**当前 agent 编排 + 多次执行 paper-reader workflow。**

### 3.1 概念库补充

1. 扫描推荐文件里所有 `[[概念]]` 链接 + enriched JSON 的 `method_names`
2. 过滤：只保留方法 / 模型 / 数据集 / 仿真器 / 技术概念名，排除通用词、论文标题、人名
3. 自动分类到 16 个概念子目录（生成模型 / 强化学习 / 机器人策略 / 3D 视觉 / 仿真器 / 数据集等）
4. 创建概念笔记：定义 + 数学形式 + 核心要点 + 代表工作 + 相关概念

### 3.2 论文笔记生成

- 只为"🔥 必读"论文生成完整笔记
- 已有笔记如果 < 100 行或缺少关键 section → 删除重新生成
- 逐篇执行 paper-reader workflow

质量校验（每篇）：
- 文件 ≥ 120 行
- 包含 LaTeX 公式（≥ 2 处）
- 包含图片引用（≥ 1 处）
- 包含 `## 关键公式` 和 `## 实验结果` section
- 不达标 → 删了重来

### 3.3 链接回填

在推荐文件中，给已有笔记的论文插入 `📒 **笔记**: [[NoteName]]` 链接。

### 3.4 刷新目录页 + git

- 调用 `generate_concept_mocs.py` 和 `generate_paper_mocs.py`
- 全部验证成功后，由 `vault_coordination.py` 创建一次精确暂存的内容 commit/push
- acquisition commit 在抓取开始前已经取得跨机器任务所有权
- acquisition 前的 pull 完成后会清除配置缓存并重新读取 Vault 配置，避免锁指纹
  使用 pull 前的旧值

---

## paper-reader workflow

**既是独立公共 Skill，也由 notes workflow 作为内部能力调用。**

### 输入源

| 来源 | 处理方式 |
|------|----------|
| arXiv 链接 | 优先获取 arXiv HTML，必要时下载 PDF |
| 本地 PDF | 直接读取 |
| Zotero 搜索 | 查 DB → 定位 PDF / 在线源 |
| Zotero 分类批量 | 递归子分类 → 去重 → 逐篇处理 |

找不到 PDF 时的 fallback 顺序：
1. arXiv URL 直接获取 HTML 版本（优先，能拿图），不访问 Zotero
2. 明确的 Zotero 输入才用 `zotero_helper.py info` 定位元数据和本地 PDF
3. Fallback：arXiv PDF / DOI 页面
4. 最后：通过 arXiv API 或可用搜索能力检索论文标题
5. 都不行 → 跳过

### 阅读模式

| 模式 | 触发词 | 输出 |
|------|--------|------|
| 快速摘要 | "快速看一下" | 3-5 句核心贡献 |
| 完整解析 | 默认 | 结构化笔记（模板） |
| 批判性分析 | "批判性分析" | 优缺点评估 |
| 知识提取 | "提取公式" | 公式 + 算法伪代码 |

### 图片获取（多路 fallback）

1. arXiv HTML：提取 `<figure>` 标签的图片 URL（优先）
2. 项目主页：从摘要 / HTML 找项目链接，抓 teaser 图
3. PDF 提取：`pdfimages -png`，过滤 > 10KB 的
4. 写完后跑 `download_note_images.py` 做可达性检查，不可达的自动下载到本地

### 笔记生成

严格按 `paper-note-template.md` 模板：
- 所有 Figure、所有公式、所有 Table 都必须出现
- 技术术语首次出现必须用 `[[概念]]` 链接
- 每个公式需要：名称、LaTeX、含义、符号说明
- 文件名只用方法 / 模型名（如 `Pi05.md`），不加年份前缀

### 存储

- Zotero 输入：`{NOTES_PATH}/{zotero_collection_path}/{MethodName}.md`
- 非 Zotero 输入：根据主题分类，无法判断时写入 `{INBOX_PATH}/`
- 默认未分类目录为 `_待整理/`
- YAML frontmatter：title / method_name / authors / year / venue / tags / 可选 zotero_collection / image_source / created

### 概念库维护

每篇论文读完后：
1. 扫描笔记中所有 `[[概念]]` 链接
2. 检查概念笔记是否存在
3. 不存在的按 16 类自动分类并创建

### 批量处理（paper_daemon.py）

```bash
python3 paper_daemon.py -c "VLA"     # 处理 VLA 分类
python3 paper_daemon.py --status     # 查看进度
python3 paper_daemon.py --list       # 列出所有分类
```

- 使用 `PAPER_DAEMON_HARNESS=claude-code|codex` 选择 CLI；只安装一个 CLI 时可自动
  识别，同时安装两个时拒绝猜测
- Claude Code 使用非交互 `--print`，Codex 使用非交互 `exec --ephemeral`；默认都
  不启用危险的权限绕过参数
- API 限流：指数退避（60s → 最大 6h）
- 配额限制：优先解析当前 harness 返回的重置时间；无法解析时默认等待 30 分钟
- 断点续跑：checkpoint 持久化
- 进程锁：防止并发
- 自动跳过已有笔记（> 100 行）

---

## generate-mocs workflow

**纯 Python，递归扫目录生成索引页。**

核心函数 `build_tree_mocs()`：
- 递归遍历目录
- 每个目录生成一个 `目录名.md` 索引文件
- 包含：子目录链接（带笔记数统计）+ 当前目录笔记列表
- 幂等：内容没变的文件不重写
- 用 wikilink 格式

分两个入口：
- `generate_concept_mocs.py`：扫描配置中的概念库目录（默认 `_概念/`）
- `generate_paper_mocs.py`：扫描论文笔记（排除概念目录）

---

## scripts/shared 公共模块

### user-config.json

所有路径、关键词、打分规则、自动化开关的集中配置：

```json
{
  "paths": {
    "obsidian_vault": ".",
    "paper_notes_folder": "论文笔记",
    "daily_papers_folder": "DailyPapers",
    "concepts_folder": "_概念",
    "inbox_folder": "_待整理",
    "zotero_db": "~/Zotero/zotero.sqlite",
    "zotero_storage": "~/Zotero/storage"
  },
  "daily_papers": {
    "keywords": ["world model", "diffusion model", "embodied ai", ...],
    "negative_keywords": ["medical imaging", "weather forecast", ...],
    "domain_boost_keywords": ["robot", "manipulation", ...],
    "arxiv_categories": ["cs.RO", "cs.CV", "cs.AI", "cs.LG"],
    "min_score": 2,
    "top_n": 30
  },
  "runtime": {
    "timezone": "Asia/Shanghai"
  },
  "repository": {
    "url": "git@github.com:haoz0206/dailypaper-vault.git",
    "remote": "origin",
    "branch": "main",
    "task_state_file": ".dailypaper/tasks/daily-papers.json",
    "pull_before_run": true,
    "require_clean": true,
    "coordination_enabled": true,
    "lease_hours": 24,
    "same_day_policy": "skip"
  },
  "automation": {
    "auto_refresh_indexes": true,
    "git_commit": false,
    "git_push": false
  }
}
```

### user_config.py

Python 配置加载器，带缓存。提供 `load_user_config()` / `paths_config()` / `daily_papers_config()` / `automation_config()` 等便捷函数。会校验 `git_push` 不能在 `git_commit` 关闭时开启。

### run_context.py

为每次运行创建隔离 manifest，记录日期、时区、中间文件、Vault 相对变更路径和远程
协调结果。Claude Code 与 Codex 传递同一种 manifest。

### vault_coordination.py

每日任务的确定性 Git 协调器。运行前验证固定远程和分支、执行 fast-forward pull，
并通过 `.dailypaper/tasks/daily-papers.json` 的 acquisition commit/push 原子抢占
任务。运行后验证远程 HEAD、`run_id` 和配置指纹，只发布 manifest 登记的稳定输出。
协调失败时禁止自动 rebase、force push 或锁抢占。

### moc_builder.py

MOC 生成引擎，被 `generate_concept_mocs.py` 和 `generate_paper_mocs.py` 调用。

---

## Obsidian 目录结构

```
<Obsidian Vault>/
├── .dailypaper/
│   ├── tasks/
│   │   └── daily-papers.json        # 跨机器/跨 harness 任务状态
│   └── runs/                        # 本地忽略的中间 manifest
├── DailyPapers/
│   ├── YYYY-MM-DD-论文推荐.md      # 每日推荐
│   └── .history.json                # 跨天去重索引
├── 论文笔记/
│   ├── 3-Robotics/
│   │   ├── 1-VLX/VLA/
│   │   │   ├── VLA.md               # 目录页（自动生成）
│   │   │   ├── OpenVLA.md
│   │   │   └── Pi05.md
│   │   └── ...
│   ├── _概念/
│   │   ├── 1-生成模型/
│   │   │   ├── DiT.md
│   │   │   └── Flow Matching.md
│   │   ├── 3-机器人策略/
│   │   │   └── Diffusion Policy.md
│   │   ├── ... (共 16 个分类)
│   │   └── 0-待分类/
│   └── _待整理/                    # 无法自动归类的论文
└── ...
```
