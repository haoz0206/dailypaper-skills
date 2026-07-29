# 论文笔记（Daily Notes Adapter）

你是每日论文流水线的第 3 步。本 Adapter 负责选择必读论文、串行调用共享阅读
核心、保存 checkpoint、回填链接和刷新目录；论文阅读与概念创建只由
`{SKILL_ROOT}/references/paper-reader/reading-core.md` 实现。

## 调用边界

本阶段只接受 `daily-papers` 父流程调用。没有父流程提供的 `RUN_MANIFEST`
只读上下文、Coordinator 决策不是 `ready`，或当前 phase 不是
`writing-notes` 时立即停止，并引导用户使用公开入口。父流程保持任务所有权；
本阶段和 Subagent 都不得修改 Manifest、Vault Task State 或 Git。

## Step 0: 使用父流程的运行时上下文

父流程必须传入同一个只读 `RUNTIME_CONTEXT`，且其 `status=ready`、
`paths.vault` 与 Manifest 中的 Vault 完全一致、
`configuration_fingerprint` 与 Manifest 完全一致。缺失或不一致时停止；本阶段
不得重新运行预检、读取配置文件或自行合并覆盖层。

显式生成并在后续统一使用这些变量：

- `VAULT_PATH`
- `NOTES_PATH`
- `CONCEPTS_PATH`
- `DAILY_PAPERS_PATH`
- `AUTO_REFRESH_INDEXES`
- `RUN_MANIFEST`
- `ENRICHED_INPUT = RUN_MANIFEST.paths.enriched`
- `NOTE_MATCHES = RUN_MANIFEST 所在目录/note-matches.json`
- `CHANGED_PATHS`

所有值只从 `RUNTIME_CONTEXT` 或 `RUN_MANIFEST.paths` 取得，其中：

- `VAULT_PATH = RUNTIME_CONTEXT.paths.vault`
- `NOTES_PATH = RUNTIME_CONTEXT.paths.paper_notes`
- `CONCEPTS_PATH = RUNTIME_CONTEXT.paths.concepts`
- `DAILY_PAPERS_PATH = RUNTIME_CONTEXT.paths.daily_papers`
- 自动化开关来自 `RUNTIME_CONTEXT.automation`

后续步骤统一使用上面的已验证值。

## 前置检查

1. 检查 `RUN_MANIFEST` 和其中声明的 `ENRICHED_INPUT` 是否存在
2. 确认父流程传入的 Coordinator 决策是 `ready`，当前 phase 是
   `writing-notes`
3. 检查已在 review checkpoint 登记的 `NOTE_MATCHES` 及今天的推荐文件
   `{DAILY_PAPERS_PATH}/YYYY-MM-DD-论文推荐.md` 是否存在
4. 如果任一不存在、决策不是 `ready` 或 phase 不匹配，告知用户需要从每日入口
   启动，然后停止
5. 全部检查通过后，才说一声“开始整理笔记 📝”并告知今天日期

## 工作流程

### Step 1: 论文笔记生成

为推荐论文生成完整论文笔记：

1. 从今天的推荐文件中，读取分流表，筛选出标记为"必读"的论文（"值得看"和"可跳过"的不生成笔记）
2. **质量检查已有笔记**（不是只看文件是否存在）：
   - 对已有 `📒 **笔记**` 标记的论文，使用确定性匹配报告中的 `note.path`
   - 从 `RUN_MANIFEST` 同目录的 `note-matches.json` 取得该论文的稳定
     `paper_id` 和匹配结果；`ambiguous` 不得自动选择候选文件
   - 对 exact/fallback 候选文件运行
     `python3 "{SKILL_ROOT}/scripts/paper-reader/validate_paper_note.py" "{笔记路径}" --expected-paper-id "{paper_id}"`
   - 只有退出码为 0 且 JSON 中 `valid=true` 时才算合格，可以跳过
   - 退出码为 1 的笔记视为不完整（包括旧笔记缺少稳定身份），但必须保留原文件，
     再调用阅读核心补全
3. 按推荐文件顺序**逐篇**处理需要生成/重新生成的论文，禁止多个写入者并行修改
   同一 Vault。先完整读取一次
   `{SKILL_ROOT}/references/paper-reader/reading-core.md`，然后对每篇传入：
   arXiv 链接、review 阶段确定的稳定 `paper_id`、`READING_MODE=full`、
   `OUTPUT_MODE=note`、同一个冻结 `RUNTIME_CONTEXT`，以及可选的已验证已有笔记。
   本阶段直接调用阅读核心，不得加载公共 standalone Adapter。
   - 当前 Harness 支持 Subagent 时，为当前论文启动恰好一个 Subagent，等待其
     读取并执行 `reading-core.md`，完成写入和自检后再处理下一篇；不支持时在
     当前上下文内执行相同核心
   - 向 Subagent 传递同一个 `RUN_MANIFEST` 仅供读取上下文，明确禁止它修改
     manifest、取得或释放 Vault 锁，或读取 standalone 会话契约
   - Subagent 必须返回阅读核心接口定义的 `paper_id`、实际笔记路径、
     概念/资源候选路径和质量结果；父阶段逐项验证后才能继续
   - 每完成并验证一篇，把唯一的 `notes-progress-<序号>.json` 写入
     `RUN_MANIFEST` 同目录，包含该论文的 artifacts、所有实际 Vault 相对
     changed paths，并把验证器 JSON 放入 `metadata.quality`。父 workflow 用
     `run_coordinator.py submit --report` 保存 checkpoint；`progress` 不推进
     phase。本阶段与 Subagent 不得自行调用该命令
     ```json
     {
       "version": 1,
       "stage": "notes",
       "result": "progress",
       "artifacts": [
         {
           "role": "paper-note",
           "scope": "vault",
           "path": "论文笔记/<topic>/<MethodName>.md"
         }
       ],
       "changed_paths": ["论文笔记/<topic>/<MethodName>.md"],
       "metadata": {"quality": [{"valid": true, "failures": []}]}
     }
     ```
   - **不要指定固定的输出路径**，让阅读核心按身份和主题决定文件名及分类目录
   - 阅读核心会用方法名缩写作为文件名（如 `DAPL.md`），并自动分类到正确子目录
   - 完成后以 Subagent 返回的路径为准，并验证该路径位于 `NOTES_PATH`、文件中的
     `paper_id` 与当前论文相同；不要重新扫描整个笔记库猜测生成文件
4. 阅读核心根据最终笔记中的概念链接创建缺失概念。本 Adapter 不得提前从推荐页
   或 `method_names` 批量创建概念，也不得在核心完成后重复创建。

> **铁律**：不论论文数量多少，"必读"的论文**全部**生成笔记，一篇不能少。
> 耗时长是正常的，不是偷懒的理由。如果 context 接近上限，先把已完成内容落盘；
> 向父流程返回 progress 报告并由父流程保存 checkpoint，然后告知用户剩余论文
> 需要继续处理，**绝对不能默默跳过，也不能提交半成品**。

#### ⚠️ 笔记质量硬性要求

**绝对禁止自己手写简化版笔记。每篇论文必须通过共享阅读核心生成。**
不要因为"怕 context overflow"或"论文太多"就自己写个 70 行的骨架糊弄过去。
如果当前会话上下文接近上限，优先按上面的 Subagent 约定隔离逐篇阅读；Subagent
不可用且本次上下文无法继续时，退出并让用户重新调用公开 daily 入口，由
Coordinator 恢复同一 `run_id` 后继续剩余论文；不得另建 Run。不能跳过任何一篇
必读论文。

笔记质量规则只在 `reading-core.md` 中定义；本 Adapter 只负责父级二次验证。

#### 🔍 生成后结构验证（每篇必须执行）

每篇生成后立即运行：

```bash
python3 "{SKILL_ROOT}/scripts/paper-reader/validate_paper_note.py" \
  "{笔记路径}" --expected-paper-id "{PAPER_ID}"
```

这个确定性验证器统一检查行数、公式、图片和必需 section。退出码为 1 时保留
当前文件和资源，根据 JSON `failures` 再次调用同一个阅读核心补全；禁止
删除已有成果。再次失败则返回 `attention`，让父流程保存 checkpoint 并请用户
决定，而不是把骨架笔记发布为成功结果。

### Step 2: 笔记链接回填

论文笔记全部生成完成后，将笔记链接回填到当天的推荐文件中。

只运行确定性脚本：

```bash
python3 "{SKILL_ROOT}/scripts/notes/backfill_links.py" \
  --recommendation "{DAILY_PAPERS_PATH}/YYYY-MM-DD-论文推荐.md" \
  --vault "{VAULT_PATH}" \
  --notes-dir "{NOTES_PATH}" \
  --concepts-dir "{CONCEPTS_PATH}"
```

脚本拥有扫描、身份匹配、插入笔记链接和修正分流表 wikilink 的完整实现。本
Adapter 不得再按标题猜方法名、扫描后手工匹配或直接编辑推荐文件。

- 退出码 0 时读取 stdout 的 `Added N note links to recommendation file`；`N>0`
  表示推荐文件是本次 changed path，`N=0` 不因本步骤新增路径。
- 非零退出码或输出契约缺失时停止，保留现有文件并返回 `attention`；禁止用手工
  3a–3d 流程兜底。
- 本步骤唯一允许修改的文件是传入的推荐文件。若出现其他 dirty path，立即停止并
  交给父流程检查。

### Step 3: 刷新 MOC 索引

只有在 `AUTO_REFRESH_INDEXES=true` 时才执行：

```bash
python3 "{SKILL_ROOT}/scripts/shared/refresh_mocs.py" \
  --scope all \
  --vault-root "{VAULT_PATH}" \
  --notes-root "{NOTES_PATH}" \
  --concepts-root "{CONCEPTS_PATH}"
```

默认配置下这个开关是开启的，所以新增的概念和论文笔记通常会自动反映到各分类目录页中。

### Step 4: 最终验证与返回发布报告

1. 确认所有必读论文笔记、概念、链接和 MOC 均已通过检查。
2. 收集本次实际创建或修改的 Vault 相对路径到 `CHANGED_PATHS`，去重并确认没有
   路径逃出 `VAULT_PATH`。不得直接写入 Manifest。
3. 读取 `{SKILL_ROOT}/references/stage-report.md`，把最终报告写到
   `RUN_MANIFEST` 同目录的 `notes-result.json`。列出推荐文件、history、每篇
   论文笔记、新增概念、资源文件和 MOC 等所有实际 artifacts 与 changed paths：

   ```json
   {
     "version": 1,
     "stage": "notes",
     "result": "success",
     "artifacts": [
       {
         "role": "daily-note",
         "scope": "vault",
         "path": "DailyPapers/YYYY-MM-DD-论文推荐.md"
       },
       {
         "role": "history",
         "scope": "vault",
         "path": "DailyPapers/.history.json"
       },
       {
         "role": "paper-note",
         "scope": "vault",
         "path": "论文笔记/<topic>/<MethodName>.md"
       }
     ],
     "changed_paths": ["<Vault 相对路径>"],
     "metadata": {
       "counts": {"concepts": 0, "paper_notes": 0, "backfilled_links": 0},
       "quality": [{"path": "<Vault 相对路径>", "valid": true, "failures": []}]
     }
   }
   ```

4. 父流程逐项验证后调用 `run_coordinator.py submit --report`。该调用登记最终
   路径、推进 `validated`/`publishing` 并完成唯一内容 commit/push。本阶段绝不
   直接发布。
5. 禁止手工执行 `git add -A`、commit、rebase 或 force push。Run Coordinator 会：
   - 验证远程仍停留在本次抢锁 commit；
   - 验证配置指纹和远程任务 `run_id` 没有变化；
   - 拒绝 manifest 之外的工作树修改；
   - 将任务状态与稳定输出放入同一个内容 commit；
   - 普通 push 失败时保留本地提交并停止，不得重新生成内容。

## 输出

完成后把结构化报告交给父 workflow，并告知用户：
- 创建了多少个新概念
- 生成了多少篇论文笔记
- 回填了多少个笔记链接
- 流水线全部完成

若未完成，返回 `recoverable`、`attention` 或 `deterministic-failure` 分类建议，
把原因、已通过质量验证的 artifacts 和 changed paths 写入 Stage Report；最终
提交由父流程执行。发生异常中断时保留已经登记的 progress checkpoints，下一次
`start` 从原 run 恢复。

## 注意事项

- 如果前置文件不存在，必须先运行前面的步骤
- 阅读核心会处理概念库补充，不要提前或重复创建
- 仅为"必读"论文生成笔记，"值得看"不生成，耗时正常，**不是跳过的理由**
- 默认自动刷新目录页，并由父流程按远程 Vault 协调契约发布
- **绝对禁止**以下偷懒行为：
  - 自己手写 70 行骨架笔记代替阅读核心输出
  - 以"context overflow"为由跳过论文不生成笔记
  - 看到文件已存在就跳过，不检查质量
  - 生成笔记后不做质量验证
- 如果 context 真的接近上限：先落盘并向父流程报告已完成的笔记，由父流程登记
  progress checkpoint，但不要 commit。然后**明确告知用户**还有 N 篇未完成，
  需要继续运行同一个 manifest。绝不能默默跳过
