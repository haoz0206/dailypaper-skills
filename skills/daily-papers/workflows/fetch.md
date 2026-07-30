> **开始前**: 先说一声 "开始抓取论文 🐕" 并告知今天日期。如果是多天模式，告知抓取范围。

# 论文抓取与审批 (Acquire + Approve + Enrich)

你是用户的论文抓取系统（3 步流水线的第 1 步）。完整抓取分类元数据 → 逐篇语义
审批 → 富化入选论文 → 保存到 Run 临时文件。

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
- `RUN_DIR = RUN_MANIFEST` 的父目录
- `ACQUIRED_OUTPUT = RUN_DIR/acquired-papers.json`
- `ACQUISITION_SUMMARY = RUN_DIR/acquisition-summary.json`
- `CANDIDATE_DOCS_DIR = RUN_DIR/candidate-docs`
- `EVALUATIONS_DIR = RUN_DIR/relevance-evaluations`
- `CANDIDATE_INDEX = RUN_DIR/candidate-index.json`
- `APPROVAL_SUMMARY = RUN_DIR/approval-summary.json`
- `APPROVAL_LIMIT = TOP_N * WINDOW_DAYS`
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

### Phase 1：恢复优先的完整元数据抓取

进入网络抓取前先检查 Run artifact：

1. 若 `CANDIDATE_INDEX` 已存在，直接运行 Phase 2 的 `pending`。它会验证 acquisition
   summary、原始 metadata、candidate Markdown 和已有 Evaluation 的完整绑定；验证成功
   时不得重抓或重新 `prepare`。
2. 若 index 不存在，但 `ACQUIRED_OUTPUT` 与 `ACQUISITION_SUMMARY` 都存在，跳过网络
   抓取，直接运行下面带 `--summary` 的 `prepare`。summary 中的 SHA-256 和完整
   arXiv snapshot 必须通过脚本验证。
3. 只有上述可恢复 artifact 不完整时才执行新抓取。存在 index 但验证失败属于
   `attention`，不得以重抓覆盖已有审批。

需要新抓取时运行确定性抓取器。它对 `ARXIV_CATEGORIES` 和冻结日期窗口使用 arXiv 分页，
根据 `totalResults` 证明 bounded snapshot 完整；关键词只产生辅助信号，不删除论文。

```bash
python3 "{SKILL_ROOT}/scripts/daily/fetch_and_score.py" \
  --runtime-context "{RUNTIME_CONTEXT_FILE}" \
  --date YYYY-MM-DD --days "{WINDOW_DAYS}" \
  --output "{ACQUIRED_OUTPUT}" --summary "{ACQUISITION_SUMMARY}"
```

脚本自动完成：
- 分页抓取选定 arXiv 分类中日期窗口内的全部标题和摘要
- 抓取 HuggingFace Daily/Trending 辅助信号；arXiv 非空时只叠加匹配 ID 的信号，
  arXiv 完整快照为空时才作为周末/节假日 fallback
- 按稳定 arXiv ID 合并；选定分类的 arXiv 论文一篇不少地进入 metadata pool
- 读取 `.history.json`，只标记后置入选资格；历史论文仍生成 Markdown 并接受审批
- 新论文不足 20 篇时，只把得分最高的历史论文标为可回填，不从审批池删除其他论文
- 计算正向、负向、领域和 trending 信号，但不做相关性硬过滤

如果分页失败、`totalResults` 变化、结果超过安全上限或无法证明完整，脚本以
`incomplete-arxiv-snapshot` 失败。不得退化为仅关键词或仅 HuggingFace 后继续发布。

### Phase 2：单篇 Markdown + Subagent 相关度审批

仅在 `CANDIDATE_INDEX` 不存在时，使用已验证或刚生成的 acquisition artifact 为
每篇论文生成一个不可变 Markdown 以及审批索引：

```bash
python3 "{SKILL_ROOT}/scripts/daily/candidate_approval.py" prepare \
  --input "{ACQUIRED_OUTPUT}" \
  --summary "{ACQUISITION_SUMMARY}" \
  --candidates-dir "{CANDIDATE_DOCS_DIR}" \
  --evaluations-dir "{EVALUATIONS_DIR}" \
  --index "{CANDIDATE_INDEX}"
```

完整读取 `{SKILL_ROOT}/references/relevance-approval.md`，然后运行：

```bash
python3 "{SKILL_ROOT}/scripts/daily/candidate_approval.py" pending \
  --index "{CANDIDATE_INDEX}"
```

只对返回的 `pending` 项逐篇审批。支持 Subagent 时使用最多 8 个并发 worker，
优先选择宿主可用的低成本、快速模型；每个任务只读取自己的候选 Markdown、同一个
只读研究配置和审批契约，并只写自己的 `evaluation_path`。父阶段等待全部任务。
不支持 Subagent 时由当前会话逐篇执行同一契约，不得改回关键词硬过滤。

恢复时再次运行 `pending`，只补做缺失项。已有结果必须通过 Evaluation v1、
`paper_id` 和候选 SHA-256 校验；无效结果停止并报告，不得静默覆盖。全部完成后
汇总 `approve`、`uncertain` 和关键词救回项，再按语义相关度选择每天最多
`TOP_N` 篇：

```bash
python3 "{SKILL_ROOT}/scripts/daily/candidate_approval.py" collect \
  --index "{CANDIDATE_INDEX}" \
  --output "{CANDIDATES_OUTPUT}" \
  --summary "{APPROVAL_SUMMARY}" \
  --top-n "{APPROVAL_LIMIT}" \
  --min-score "{MIN_SCORE}"
```

`reject` 不进入富化，除非确定性 `score >= MIN_SCORE`，此时作为
`keyword-rescue` 保留以降低模型假阴性；`uncertain` 始终进入候选池，直到
`TOP_N` 上限。

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
    {"role": "acquisition", "scope": "run", "path": "acquired-papers.json"},
    {"role": "acquisition-summary", "scope": "run", "path": "acquisition-summary.json"},
    {"role": "candidate-index", "scope": "run", "path": "candidate-index.json"},
    {"role": "approval-summary", "scope": "run", "path": "approval-summary.json"},
    {"role": "candidates", "scope": "run", "path": "candidates.json"},
    {"role": "enriched", "scope": "run", "path": "enriched.json"}
  ],
  "changed_paths": [],
  "metadata": {
    "counts": {
      "acquired": 0,
      "approve": 0,
      "uncertain": 0,
      "reject": 0,
      "candidates": 0,
      "enriched": 0
    }
  }
}
```

其中路径使用真实 Run 相对路径，计数从审批汇总和输出中取得。
`candidate-index.json` 固定 acquisition summary、原始 metadata 与每篇候选
Markdown 的 SHA-256；每个 evaluation 再通过自身的 `input_sha256` 绑定对应候选。
这些逐篇文件不塞入 Manifest。父流程负责用
`run_coordinator.py submit --report` 登记并推进 phase。本阶段只告知：
- 完整抓取了多少篇论文
- approve / uncertain / reject 各多少篇
- 富化成功多少篇
- 把控制权返回父 workflow；不得要求用户另行调用内部阶段

失败时不要写 Manifest 或协调状态。把同样结构的报告写到 `fetch-result.json`，
将 `result` 分类为 `recoverable`、`attention` 或 `deterministic-failure`，
附非空 `message`，并把 stderr 摘要放入 `metadata`。最终提交由父流程负责。

## 注意事项

- Phase 1 使用脚本完整抓取；Phase 2 才消耗低成本模型 token
- 关键词、负向关键词和领域词只作审批提示、排序及救回信号
- Phase 3 使用 `enrich_papers.py` 脚本，同样由当前会话直接执行
- 如果脚本执行失败，检查 stderr 输出诊断问题
- arXiv snapshot 无法证明完整时停止，不得静默降级
- 如果总论文数不足 20 篇，有多少处理多少
- **周末策略**：只有 arXiv 完整快照证明为空时，才使用 HF Daily/Trending 的
  bounded fallback；因此不牺牲工作日选定分类的完整性
- **不做 Manifest、Vault Task State 或 git 操作**，不生成推荐文件，只输出本次
  运行的中间 JSON 和结构化阶段报告
