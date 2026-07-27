---
name: daily-papers-fetch
description: |
  论文抓取（3 步流水线的第 1 步）。抓取 arXiv + HuggingFace 最新论文，打分筛选，富化信息，
  输出到本次运行 manifest 指定的隔离目录，供后续阶段使用。

  触发词："论文抓取"、"跑一下论文抓取"
  支持多天模式："过去3天论文推荐"、"过去一周论文推荐"、"过去一周的论文"、"抓 3 天的论文"、"最近5天"
---

> **开始前**: 先说一声 "开始抓取论文 🐕" 并告知今天日期。如果是多天模式，告知抓取范围。

# 论文抓取 (Fetch + Score + Enrich)

你是 用户的论文抓取系统（3 步流水线的第 1 步）。抓取最新论文 → 打分筛选 → 富化信息 → 保存到临时文件。

## Step 0: 读取共享配置

将本 `SKILL.md` 所在目录的父目录解析为绝对路径 `SKILLS_ROOT`。先读取
`{SKILLS_ROOT}/_shared/user-config.json`；如果同目录的 `user-config.local.json`
存在，再用它覆盖默认值。也允许 `DAILYPAPER_CONFIG` 指向外部配置。

显式生成并在后续统一使用这些变量：

- `VAULT_PATH`
- `DAILY_PAPERS_PATH`
- `KEYWORDS`
- `NEGATIVE_KEYWORDS`
- `DOMAIN_BOOST_KEYWORDS`
- `ARXIV_CATEGORIES`
- `MIN_SCORE`
- `TOP_N`
- `TIMEZONE`
- `RUN_MANIFEST`
- `CANDIDATES_OUTPUT`
- `ENRICHED_OUTPUT`

其中：

- `DAILY_PAPERS_PATH = {VAULT_PATH}/{daily_papers_folder}`
- `TIMEZONE = runtime.timezone`
- 所有关键词、分类、阈值都以共享配置为准
- `CANDIDATES_OUTPUT` 和 `ENRICHED_OUTPUT` 必须从 `RUN_MANIFEST.paths` 读取

如果父流程没有提供 `RUN_MANIFEST`，先根据当前宿主设置 `HARNESS_ID`（Claude Code
使用 `claude-code`，Codex 使用 `codex`），再运行：

```bash
python3 "{SKILLS_ROOT}/_shared/run_context.py" create \
  --date YYYY-MM-DD --timezone "{TIMEZONE}"
```

记住返回的 manifest 绝对路径；禁止复用其他运行的 manifest。

随后必须取得 Vault 任务所有权：

```bash
python3 "{SKILLS_ROOT}/_shared/vault_coordination.py" acquire \
  "{RUN_MANIFEST}" --harness "{HARNESS_ID}"
```

如果父流程已经提供 manifest，则确认 `RUN_MANIFEST.coordination.status` 是
`acquired`。任何其他状态都停止；内部阶段不得绕过协调器直接写 Vault。

后续统一以共享配置和上面的变量为准。

开始抓取前更新运行状态：

```bash
python3 "{SKILLS_ROOT}/_shared/run_context.py" update "{RUN_MANIFEST}" \
  --status fetching
```

## 解析天数

从用户输入中解析 `--days N` 参数。匹配规则：
- "过去一周"、"最近7天"、"一周的论文" → `--days 7`
- "过去3天"、"最近三天"、"抓3天" → `--days 3`
- "过去两周" → `--days 14`
- 无特殊指定 / "跑一下论文抓取" → 不加 `--days`（默认当天）

将解析出的天数存为变量 `DAYS_ARG`，在后续脚本调用中使用。

## 配置来源

- 默认配置在 `{SKILLS_ROOT}/_shared/user-config.json`
- 个人覆盖配置放在 `{SKILLS_ROOT}/_shared/user-config.local.json`
- 如果两者都存在，以 `local` 为准

## 工作流程

### Phase 1+2: 抓取 + 打分 + 合并去重（纯 Python 脚本）

用 `fetch_and_score.py` 一步完成 HF + arXiv 抓取、打分、合并去重、历史去重、选 Top 30。**零 token 消耗。**

```bash
# 默认：当天
python3 "{SKILLS_ROOT}/daily-papers/fetch_and_score.py" \
  --date YYYY-MM-DD --timezone "{TIMEZONE}" --output "{CANDIDATES_OUTPUT}"

# 多天模式（将 N 替换为解析出的天数）
python3 "{SKILLS_ROOT}/daily-papers/fetch_and_score.py" \
  --date YYYY-MM-DD --timezone "{TIMEZONE}" --days N --output "{CANDIDATES_OUTPUT}"
```

根据前面解析的 `DAYS_ARG`，如果用户指定了天数就加 `--days N`，否则不加。

脚本自动完成：
- 并行抓取 HuggingFace Daily + Trending API 和 arXiv API
- 关键词打分（正向/负向/领域加分/trending 加分）
- 按 arXiv ID 合并去重
- 读取 `.history.json` 跨天去重（含周末模式放宽规则）
- 不足 20 篇时从历史回填
- 按 score 降序取 Top 30

进度日志输出到 stderr，JSON 结果写入 manifest 指定的候选文件。

**检查输出**：确认 `CANDIDATES_OUTPUT` 存在且包含有效 JSON 数组。如果为空数组或文件不存在，检查 stderr 诊断问题。

### Phase 3: 批量富化（enrich_papers.py 脚本）

用 `enrich_papers.py` 脚本一次性富化所有论文。脚本使用 `asyncio` + `curl`
子进程并发请求，纯 regex 解析 HTML，不依赖宿主专用网页工具。

```bash
python3 "{SKILLS_ROOT}/daily-papers/enrich_papers.py" \
  --input "{CANDIDATES_OUTPUT}" --output "{ENRICHED_OUTPUT}"
```

使用显式输入输出参数，避免管道、stdout/stderr 混淆和跨运行文件冲突。

脚本自动完成以下工作（Semaphore(10) 限制并发，单篇超时 30 秒）：
- 并行抓取 HTML 页面 + PDF 页面
- 从 HTML 提取：figure_url、authors、affiliations、section_headers、captions、has_real_world、method_names、method_summary
- 从 PDF 提取：affiliations（通过 `pdftotext | extract_affiliations.py`）
- 如果 HTML authors 为空，fallback 到 abs 页面 `<meta>` 标签提取 authors/affiliations
- 合并优先级（脚本内部处理）：
  - figure_url: HTML curl
  - affiliations: PDF > HTML > abs fallback > Phase 1 data
  - authors: HTML > abs fallback > Phase 1 data
  - 其他字段: HTML regex 提取

**输出格式**：与输入相同的 JSON 数组，每篇论文增加以下字段：
- `figure_url` (string): 首图 URL
- `affiliations` (string): 机构列表，逗号分隔
- `authors` (string): 作者列表（可能被更完整的来源覆盖）
- `section_headers` (array): 章节标题
- `captions` (array): 图表标题
- `has_real_world` (bool): 是否包含真实实验
- `method_names` (array): 方法名列表
- `method_summary` (string): 方法描述（300-500 字）

## 输出

完成后检查 `ENRICHED_OUTPUT` 存在且包含有效 JSON 数组。告知用户：
- 抓取了多少篇论文
- 富化成功多少篇
- 提示运行下一步：`跑一下论文点评`

## 注意事项

- Phase 1+2 使用 `fetch_and_score.py` 脚本，由当前会话直接执行，零 token 消耗
- Phase 3 使用 `enrich_papers.py` 脚本，同样由当前会话直接执行
- 如果脚本执行失败，检查 stderr 输出诊断问题
- 如果 arXiv API 抓取失败，脚本自动 fallback 到仅 HuggingFace 源
- 如果总论文数不足 20 篇，有多少处理多少
- **周末策略**：arXiv 周末不更新，HF daily 周末基本为空，但 HF trending 持续更新。周末主要依赖 trending 来源
- **不做 git 操作**，不生成推荐文件，只输出本次运行的中间 JSON
