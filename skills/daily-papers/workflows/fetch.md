> **开始前**: 先说一声 "开始抓取论文 🐕" 并告知今天日期。如果是多天模式，告知抓取范围。

# 论文抓取 (Fetch + Score + Enrich)

你是 用户的论文抓取系统（3 步流水线的第 1 步）。抓取最新论文 → 打分筛选 → 富化信息 → 保存到临时文件。

## 调用边界

本阶段只接受 `daily-papers` 父流程调用。若没有父流程提供的 `RUN_MANIFEST`
只读上下文，或父流程的 Coordinator 决策不是 `ready`，或当前 phase 不是
`fetching`，停止并要求从“今日论文推荐”等公开入口启动；不得把用户的普通推荐
请求解释为直接运行本阶段。即使是维护者调试，也必须由父 workflow 先通过
`run_coordinator.py start` 建立或恢复 run。

## Step 0: 读取共享配置

使用公开 Skill 已解析的 `SKILL_ROOT`。先读取
`{SKILL_ROOT}/scripts/shared/user-config.json`；如果同目录的 `user-config.local.json`
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

确认父流程提供的 Coordinator 决策是 `ready`、`RUN_MANIFEST` 存在且 phase 为
`fetching`。没有 manifest 或任何其他 phase 都停止；内部阶段不得创建、修改
Manifest，不得取得/释放任务所有权，也不得直接写 Vault Task State 或运行 Git。

后续统一以共享配置和上面的变量为准。

## 解析天数

从用户输入中解析 `--days N` 参数。匹配规则：
- "过去一周"、"最近7天"、"一周的论文" → `--days 7`
- "过去3天"、"最近三天"、"抓3天" → `--days 3`
- "过去两周" → `--days 14`
- 无特殊指定 → 不加 `--days`（默认当天）

将解析出的天数存为变量 `DAYS_ARG`，在后续脚本调用中使用。

## 配置来源

- 默认配置在 `{SKILL_ROOT}/scripts/shared/user-config.json`
- 个人覆盖配置放在 `{SKILL_ROOT}/scripts/shared/user-config.local.json`
- 如果两者都存在，以 `local` 为准

## 工作流程

### Phase 1+2: 抓取 + 打分 + 合并去重（纯 Python 脚本）

用 `fetch_and_score.py` 一步完成 HF + arXiv 抓取、打分、合并去重、历史去重、选 Top 30。**零 token 消耗。**

```bash
# 默认：当天
python3 "{SKILL_ROOT}/scripts/daily/fetch_and_score.py" \
  --date YYYY-MM-DD --timezone "{TIMEZONE}" --output "{CANDIDATES_OUTPUT}"

# 多天模式（将 N 替换为解析出的天数）
python3 "{SKILL_ROOT}/scripts/daily/fetch_and_score.py" \
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
python3 "{SKILL_ROOT}/scripts/daily/enrich_papers.py" \
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

完成后检查 `ENRICHED_OUTPUT` 存在且包含有效 JSON 数组。向父 workflow 返回
结构化报告：

```json
{
  "stage": "fetch",
  "result": "success",
  "artifacts": [
    {"role": "candidates", "path": "<CANDIDATES_OUTPUT>"},
    {"role": "enriched", "path": "<ENRICHED_OUTPUT>"}
  ],
  "changed_paths": [],
  "counts": {"candidates": 0, "enriched": 0}
}
```

其中计数替换为真实值。父流程验证文件后负责用 `run_coordinator.py submit
--result success` 登记 artifacts 并推进 phase。本阶段只告知：
- 抓取了多少篇论文
- 富化成功多少篇
- 把控制权返回父 workflow；不得要求用户另行调用内部阶段

失败时不要写 Manifest 或协调状态。返回同样结构的报告，将 `result` 建议分类为
`recoverable`、`attention` 或 `deterministic-failure`，并附 `message`、stderr
摘要和已经安全落盘的 artifacts。最终分类和提交由父流程负责。

## 注意事项

- Phase 1+2 使用 `fetch_and_score.py` 脚本，由当前会话直接执行，零 token 消耗
- Phase 3 使用 `enrich_papers.py` 脚本，同样由当前会话直接执行
- 如果脚本执行失败，检查 stderr 输出诊断问题
- 如果 arXiv API 抓取失败，脚本自动 fallback 到仅 HuggingFace 源
- 如果总论文数不足 20 篇，有多少处理多少
- **周末策略**：arXiv 周末不更新，HF daily 周末基本为空，但 HF trending 持续更新。周末主要依赖 trending 来源
- **不做 Manifest、Vault Task State 或 git 操作**，不生成推荐文件，只输出本次
  运行的中间 JSON 和结构化阶段报告
