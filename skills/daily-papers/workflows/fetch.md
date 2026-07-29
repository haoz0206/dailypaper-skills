> **开始前**: 先说一声 "开始抓取论文 🐕" 并告知今天日期。如果是多天模式，告知抓取范围。

# 论文抓取 (Fetch + Score + Enrich)

你是 用户的论文抓取系统（3 步流水线的第 1 步）。抓取最新论文 → 打分筛选 → 富化信息 → 保存到临时文件。

## 调用边界

本阶段只接受 `daily-papers` 父流程调用。若没有父流程提供的 `RUN_MANIFEST`
只读上下文，或父流程的 Coordinator 决策不是 `ready`，或当前 phase 不是
`fetching`，停止并要求从“今日论文推荐”等公开入口启动；不得把用户的普通推荐
请求解释为直接运行本阶段。即使是维护者调试，也必须由父 workflow 先通过
`run_coordinator.py start` 建立或恢复 run。

## Step 0: 使用父流程的运行时上下文

父流程必须同时传入同一个只读 `RUNTIME_CONTEXT`，且其 `status=ready`、
`paths.vault` 与 Manifest 中的 Vault 完全一致、
`configuration_fingerprint` 与 Manifest 完全一致。缺失或不一致时停止；本阶段
不得重新运行预检、读取配置文件或自行合并覆盖层。

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
- `RUNTIME_CONTEXT_FILE`
- `CANDIDATES_OUTPUT`
- `ENRICHED_OUTPUT`

所有路径和配置值只从 `RUNTIME_CONTEXT` 或 `RUN_MANIFEST.paths` 取得，其中：

- `VAULT_PATH = RUNTIME_CONTEXT.paths.vault`
- `DAILY_PAPERS_PATH = RUNTIME_CONTEXT.paths.daily_papers`
- `TIMEZONE = RUNTIME_CONTEXT.runtime.timezone`
- 所有关键词、分类、阈值来自 `RUNTIME_CONTEXT.daily_papers`
- `CANDIDATES_OUTPUT` 和 `ENRICHED_OUTPUT` 必须从 `RUN_MANIFEST.paths` 读取
- `RUNTIME_CONTEXT_FILE` 必须直接使用 Coordinator 返回的绝对
  `runtime_context_file`

确认父流程提供的 Coordinator 决策是 `ready`、`RUN_MANIFEST` 存在且 phase 为
`fetching`。没有 manifest 或任何其他 phase 都停止；内部阶段不得创建、修改
Manifest，不得取得/释放任务所有权，也不得直接写 Vault Task State 或运行 Git。

后续统一以上面的已验证值为准。

## 冻结的抓取窗口

只从 `RUN_MANIFEST.window_days` 读取 `WINDOW_DAYS`。它由父流程在首次 start 前解析，
并已绑定到远程 Task State；本阶段不得从当前 prompt 重解析、采用默认值或修改它。
再次验证它是 1–31 的整数，不满足时停止并报告 Manifest 错误。

## 工作流程

### Phase 1+2: 抓取 + 打分 + 合并去重（纯 Python 脚本）

用 `fetch_and_score.py` 一步完成 HF + arXiv 抓取、打分、合并去重、历史去重、选 Top 30。**零 token 消耗。**

```bash
python3 "{SKILL_ROOT}/scripts/daily/fetch_and_score.py" \
  --runtime-context "{RUNTIME_CONTEXT_FILE}" \
  --date YYYY-MM-DD --days "{WINDOW_DAYS}" --output "{CANDIDATES_OUTPUT}"
```

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

用 `enrich_papers.py` 脚本一次性富化所有论文。脚本使用 `asyncio` 和共享的
有界 HTTP 客户端并发请求，纯 regex 解析 HTML，不依赖宿主专用网页工具。

```bash
python3 "{SKILL_ROOT}/scripts/daily/enrich_papers.py" \
  --input "{CANDIDATES_OUTPUT}" --output "{ENRICHED_OUTPUT}"
```

使用显式输入输出参数，避免管道、stdout/stderr 混淆和跨运行文件冲突。

脚本自动完成以下工作（Semaphore(10) 限制并发，单篇超时 30 秒）：
- 并行抓取 HTML 页面 + PDF 页面
- 从 HTML 提取：figure_url、authors、affiliations、section_headers、captions、has_real_world、method_names、method_summary
- 从 PDF 提取：先通过共享 HTTP 边界下载到隔离临时目录，再用受限的
  `pdftotext` 读取前两页，并在进程内提取 affiliations
- 如果 HTML authors 为空，fallback 到 abs 页面 `<meta>` 标签提取 authors/affiliations
- 合并优先级（脚本内部处理）：
  - figure_url: HTML
  - affiliations: HTML > abs fallback > PDF > Phase 1 data
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

完成后检查 `ENRICHED_OUTPUT` 存在且包含有效 JSON 数组。读取
`{SKILL_ROOT}/references/stage-report.md`，把以下内容写到
`RUN_MANIFEST` 同目录的 `fetch-result.json`：

```json
{
  "version": 1,
  "stage": "fetch",
  "result": "success",
  "artifacts": [
    {"role": "candidates", "scope": "run", "path": "candidates.json"},
    {"role": "enriched", "scope": "run", "path": "enriched.json"}
  ],
  "changed_paths": [],
  "metadata": {"counts": {"candidates": 0, "enriched": 0}}
}
```

其中路径使用 Manifest 中的真实 Run 相对路径，计数替换为真实值。父流程负责用
`run_coordinator.py submit --report` 登记并推进 phase。本阶段只告知：
- 抓取了多少篇论文
- 富化成功多少篇
- 把控制权返回父 workflow；不得要求用户另行调用内部阶段

失败时不要写 Manifest 或协调状态。把同样结构的报告写到 `fetch-result.json`，
将 `result` 分类为 `recoverable`、`attention` 或 `deterministic-failure`，
附非空 `message`，并把 stderr 摘要放入 `metadata`。最终提交由父流程负责。

## 注意事项

- Phase 1+2 使用 `fetch_and_score.py` 脚本，由当前会话直接执行，零 token 消耗
- Phase 3 使用 `enrich_papers.py` 脚本，同样由当前会话直接执行
- 如果脚本执行失败，检查 stderr 输出诊断问题
- 如果 arXiv API 抓取失败，脚本自动 fallback 到仅 HuggingFace 源
- 如果总论文数不足 20 篇，有多少处理多少
- **周末策略**：arXiv 周末不更新，HF daily 周末基本为空，但 HF trending 持续更新。周末主要依赖 trending 来源
- **不做 Manifest、Vault Task State 或 git 操作**，不生成推荐文件，只输出本次
  运行的中间 JSON 和结构化阶段报告
