# 论文笔记 (Concepts + Notes + Backfill)

你是 用户的论文笔记系统（3 步流水线的第 3 步）。补充概念库 → 生成论文笔记 → 链接回填 → 刷新目录页。

## 调用边界

本阶段只接受 `daily-papers` 父流程调用。没有父流程提供的 `RUN_MANIFEST`
只读上下文、Coordinator 决策不是 `ready`，或当前 phase 不是
`writing-notes` 时立即停止，并引导用户使用公开入口。父流程保持任务所有权；
本阶段和 Subagent 都不得修改 Manifest、Vault Task State 或 Git。

## Step 0: 读取共享配置

使用公开 Skill 已解析的 `SKILL_ROOT`。读取
`{SKILL_ROOT}/scripts/shared/user-config.json` 和可选的 `user-config.local.json`。

显式生成并在后续统一使用这些变量：

- `VAULT_PATH`
- `NOTES_PATH`
- `CONCEPTS_PATH`
- `DAILY_PAPERS_PATH`
- `AUTO_REFRESH_INDEXES`
- `GIT_COMMIT_ENABLED`
- `GIT_PUSH_ENABLED`
- `RUN_MANIFEST`
- `ENRICHED_INPUT = RUN_MANIFEST.paths.enriched`
- `CHANGED_PATHS`

其中：

- `NOTES_PATH = {VAULT_PATH}/{paper_notes_folder}`
- `CONCEPTS_PATH = {NOTES_PATH}/{concepts_folder}`
- `DAILY_PAPERS_PATH = {VAULT_PATH}/{daily_papers_folder}`
- `GIT_PUSH_ENABLED` 只有在 `GIT_COMMIT_ENABLED=true` 时才可能为真
- `GIT_COMMIT_ENABLED` 和 `GIT_PUSH_ENABLED` 只控制 `paper-reader` 等独立
  调用；本协调流水线始终由父流程调用 Run Coordinator 按原子发布契约提交并
  push，内部阶段不得用这两个开关绕过或重复发布

后续步骤统一使用上面的变量。

## 前置检查

1. 检查 `RUN_MANIFEST` 和其中声明的 `ENRICHED_INPUT` 是否存在
2. 确认父流程传入的 Coordinator 决策是 `ready`，当前 phase 是
   `writing-notes`
3. 检查今天的推荐文件 `{DAILY_PAPERS_PATH}/YYYY-MM-DD-论文推荐.md` 是否存在
4. 如果任一不存在、决策不是 `ready` 或 phase 不匹配，告知用户需要从每日入口
   启动，然后停止
5. 全部检查通过后，才说一声“开始整理笔记 📝”并告知今天日期

## 工作流程

### Step 1: 概念库补充

**1a: 提取概念列表**
1. 扫描今天的推荐文件，提取所有 `[[...]]` 链接
2. 额外从 `ENRICHED_INPUT` 的 `method_names` 列表中提取所有方法名
3. 合并去重

**1b: 过滤**
只保留以下类型的术语（跳过通用词、论文自身名称、公司名、人名）：
- 方法/模型名（如 Q-Former, Parseval Regularization, CVAE, PCM）
- 数据集名（如 AMASS, LaFan1, MotionX, AndroidCode）
- 仿真器/框架名（如 OmniGibson, IsaacLab, Acados）
- 技术概念名（如 System Level Synthesis, Consistency Model）

**1c: 创建缺失的概念笔记（自动归类）**
检查 `{CONCEPTS_PATH}/` 下是否已存在（搜索所有子目录）。对于缺失的概念，**根据概念类型自动归类到对应子目录**，不要全扔 `0-待分类/`。

分类规则见 `{SKILL_ROOT}/references/paper-reader/concept-categories.md`

概念笔记模板见 `{SKILL_ROOT}/references/paper-reader/concept-categories.md`

### Step 2: 论文笔记生成

为推荐论文生成完整论文笔记：

1. 从今天的推荐文件中，读取分流表，筛选出标记为"必读"的论文（"值得看"和"可跳过"的不生成笔记）
2. **质量检查已有笔记**（不是只看文件是否存在）：
   - 对已有 `📒 **笔记**` 标记的论文，扫描笔记目录找到对应文件并检查行数
   - 使用与“生成后质量验证”完全相同的标准：行数、公式、图片和必需 section
     任一不合格都视为骨架笔记，必须重新调用 paper-reader 生成
   - 只有同时满足下方全部硬性验证条件时才算合格，可以跳过
3. 按推荐文件顺序**逐篇**处理需要生成/重新生成的论文，禁止多个写入者并行修改
   同一 Vault。读取并执行 `{SKILL_ROOT}/workflows/paper-reader.md`，传入 arXiv
   链接，并明确设置 `DAILYPAPER_PARENT_RUN=true`，禁止它独立提交 Git。
   - 当前 Harness 支持 Subagent 时，为当前论文启动恰好一个 Subagent，等待其
     写入和自检完成后再处理下一篇；不支持时在当前上下文内执行相同步骤
   - 向 Subagent 传递同一个 `RUN_MANIFEST` 仅供读取上下文，明确禁止它修改
     manifest、取得或释放 Vault 锁、运行 Git add/commit/push
   - Subagent 必须返回实际笔记路径、概念/资源变更路径和质量检查结果；父阶段
     逐项验证后才能继续
   - 每完成并验证一篇，立即向父 workflow 返回一条结构化 `progress` 报告，包含
     该论文的 artifacts、所有实际 Vault 相对 changed paths 和质量检查结果。父
     workflow 应调用 `run_coordinator.py submit --result progress` 保存详细
     checkpoint；`progress` 不推进 phase。本阶段与 Subagent 不得自行调用该命令
   - **不要指定固定的输出路径**，让 paper-reader 自行决定文件名和分类目录
   - paper-reader 会用方法名缩写作为文件名（如 `DAPL.md`），并自动分类到正确子目录
   - 完成后扫描笔记目录，找到实际生成的文件路径和文件名，记录下来供 Step 3 回填
4. 笔记生成后，paper-reader 会自动补充概念库，无需重复

> **铁律**：不论论文数量多少，"必读"的论文**全部**生成笔记，一篇不能少。
> 耗时长是正常的，不是偷懒的理由。如果 context 接近上限，先把已完成内容落盘；
> 向父流程返回 progress 报告并由父流程保存 checkpoint，然后告知用户剩余论文
> 需要继续处理，**绝对不能默默跳过，也不能提交半成品**。

#### ⚠️ 笔记质量硬性要求

**绝对禁止自己手写简化版笔记。每篇论文必须通过 `paper-reader` skill 生成。**
不要因为"怕 context overflow"或"论文太多"就自己写个 70 行的骨架糊弄过去。
如果当前会话上下文接近上限，优先按上面的 Subagent 约定隔离逐篇阅读；Subagent
不可用时才开启同一 Harness 的新会话继续剩余论文。不能跳过任何一篇必读论文。

笔记质量由 paper-reader skill 自身保证（模板、公式、图片、概念链接等规则均在 paper-reader 中定义）。

#### 🔍 生成后质量验证（每篇必须执行）

每篇笔记生成后，立即验证：
1. 文件行数 >= 120（低于此值说明内容不完整）
2. 包含 `$$` 或 `$` LaTeX 公式（至少 2 处）
3. 包含 `![` 图片引用（至少 1 张）
4. 包含 `## 关键公式`、`## 关键图表` 和 `## 实验结果` section header
5. 如果任一条件不满足，**删除文件并重新生成**

### Step 3: 笔记链接回填

论文笔记全部生成完成后，将笔记链接回填到当天的推荐文件中。

优先运行确定性脚本：

```bash
python3 "{SKILL_ROOT}/scripts/notes/backfill_links.py" \
  --recommendation "{DAILY_PAPERS_PATH}/YYYY-MM-DD-论文推荐.md" \
  --notes-dir "{NOTES_PATH}"
```

**3a: 收集已有笔记**

扫描 `{NOTES_PATH}/` 下所有子目录（跳过 `{CONCEPTS_PATH}`），获取所有 `.md` 文件列表，建立 `{文件名(不含.md): 相对路径}` 的索引。

**3b: 匹配论文与笔记**

读取当天推荐文件 `{DAILY_PAPERS_PATH}/YYYY-MM-DD-论文推荐.md`，对每篇论文（`### N.` 开头的段落）：

1. 从论文标题中提取方法名/模型名（通常是标题冒号前的缩写，如 "DM0"、"BPP"、"PA3FF"）
2. 与 3a 的笔记索引匹配（不区分大小写）
3. 也检查富化数据的 `method_names`（如果有残留数据）

**3c: 插入笔记链接 + 修正分流表**

对匹配到笔记的论文，在 `- **来源**:` 行之后插入一行：

```markdown
- 📒 **笔记**: [[笔记名]]
```

其中 `笔记名` 是不含 `.md` 后缀的文件名（Obsidian 会自动解析到正确路径）。

- 如果该论文已有 `📒 **已有笔记**` 或 `📒 **笔记**` 行，跳过不重复添加
- 逐篇插入，确保不破坏文件其他内容

**3d: 同步修正分流表 wikilink**

paper-reader 生成笔记时会自行决定文件名（通常用方法名缩写，如 `DAPL`），可能与分流表中的 `[[wikilink]]` 不一致（如分流表写了 `[[Emerging Extrinsic Dexterity]]`）。因此回填时必须检查并修正：

1. 对每篇已生成笔记的论文，拿到实际笔记文件名（如 `DAPL`）
2. 在分流表（`## 分流表` 区域）中查找该论文的 `[[...]]` 链接
3. 如果 wikilink 文本与实际笔记文件名不一致，替换为 `[[实际文件名]]`
4. 同样检查论文详评标题下方是否有不一致的 wikilink，一并修正

### Step 4: 刷新 MOC 索引

只有在 `AUTO_REFRESH_INDEXES=true` 时才执行：

```bash
python3 "{SKILL_ROOT}/scripts/shared/generate_concept_mocs.py"
python3 "{SKILL_ROOT}/scripts/shared/generate_paper_mocs.py"
```

默认配置下这个开关是开启的，所以新增的概念和论文笔记通常会自动反映到各分类目录页中。

### Step 5: 最终验证与返回发布报告

1. 确认所有必读论文笔记、概念、链接和 MOC 均已通过检查。
2. 收集本次实际创建或修改的 Vault 相对路径到 `CHANGED_PATHS`，去重并确认没有
   路径逃出 `VAULT_PATH`。不得直接写入 Manifest。
3. 向父 workflow 返回最终结构化报告，列出推荐文件、history、每篇论文笔记、
   新增概念、资源文件和 MOC 等所有实际 artifacts 与 changed paths：

   ```json
   {
     "stage": "notes",
     "result": "success",
     "artifacts": [
       {"role": "paper-note", "path": "<实际笔记绝对路径>"}
     ],
     "changed_paths": ["<Vault 相对路径>"],
     "counts": {"concepts": 0, "paper_notes": 0, "backfilled_links": 0}
   }
   ```

4. 父流程逐项验证后才调用 `run_coordinator.py submit --result success`。该调用
   登记最终路径、推进 `validated`/`publishing` 并完成唯一内容 commit/push。
   本阶段绝不直接发布。
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
附原因、已通过质量验证的 artifacts 和 changed paths；最终分类与
`run_coordinator.py submit` 由父流程执行。发生异常中断时保留已经登记的
progress checkpoints，下一次 `start` 从原 run 恢复。

## 注意事项

- 如果前置文件不存在，必须先运行前面的步骤
- `paper-reader` skill 会自动处理概念库补充，不要重复创建
- 仅为"必读"论文生成笔记，"值得看"不生成，耗时正常，**不是跳过的理由**
- 默认自动刷新目录页，并由父流程按远程 Vault 协调契约发布
- **绝对禁止**以下偷懒行为：
  - 自己手写 70 行骨架笔记代替 paper-reader 输出
  - 以"context overflow"为由跳过论文不生成笔记
  - 看到文件已存在就跳过，不检查质量
  - 生成笔记后不做质量验证
- 如果 context 真的接近上限：先落盘并向父流程报告已完成的笔记，由父流程登记
  progress checkpoint，但不要 commit。然后**明确告知用户**还有 N 篇未完成，
  需要继续运行同一个 manifest。绝不能默默跳过
