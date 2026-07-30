# 每日论文推荐

这是面向用户的一句话入口。对用户来说，正常只需要说一次：

- `今日论文推荐`
- `过去3天论文推荐`
- `过去一周论文推荐`

## 执行原则

1. 先识别时间范围：
   - `今日论文推荐`、`每日推荐`、`今日论文` -> 当天
   - `过去3天论文推荐`、`最近3天论文` -> 3 天
   - `过去一周论文推荐`、`看看这周有啥论文` -> 7 天
   把结果规范化为整数 `WINDOW_DAYS`；其值必须在 1–31 之间，否则停止并要求用户
   缩小窗口。`WINDOW_DAYS` 是本次请求 intent，只在第一次调用 Coordinator 前
   解析一次。
2. 使用公开 `SKILL.md` 已解析的绝对路径 `SKILL_ROOT`。所有脚本和内部 workflow
   都从 `SKILL_ROOT` 解析，禁止依赖当前工作目录。
3. 根据当前宿主设置 `HARNESS_ID`：Claude Code 使用 `claude-code`，Codex 使用 `codex`；
   不得根据 Vault 分支或输出路径猜测。
4. 唯一允许的运行入口是 Run Coordinator 的 `start-or-resume` 决策。它在一个
   接口内完成本机 onboarding 验证、首次 Vault bootstrap、正常运行前的
   fast-forward 同步、Runtime Context 解析、远程任务检查和本地恢复判断：

   ```bash
   python3 "{SKILL_ROOT}/scripts/shared/run_coordinator.py" start \
     --harness "{HARNESS_ID}" --date YYYY-MM-DD --window-days "{WINDOW_DAYS}"
   ```

   不得在此前单独运行 `machine_config.py validate`、`vault_coordination.py
   bootstrap` 或 `runtime_context.py`；重复预检会增加远程请求，并会在异常恢复
   时错误拒绝当前 run 自己尚未提交的合法 artifact。

   完整保留 Coordinator 的 JSON 返回。若 onboarding 不存在或无效，停止并要求
   用户先运行公共 `configure-dailypaper` Skill；不得退回当前工作目录、猜测 Vault，
   或在 Skills 仓库中创建输出。只有 `decision=ready` 时：

   - 从 `manifest` 取得绝对 `RUN_MANIFEST`。
   - 把返回的 `runtime_context` 完整保存为只读 `RUNTIME_CONTEXT`。后续阶段只从
     这个对象读取 `paths`、`runtime`、`repository`、`daily_papers`、
     `automation` 和 `configuration_fingerprint`，不得再次读取或手工合并配置文件。
   - 从 `runtime_context_file` 取得 Coordinator 冻结的绝对
     `RUNTIME_CONTEXT_FILE`；需要启动 Python 子进程时传入该文件，不得把配置重新
     展开成易漂移的另一套命令行参数。
   - 保留 `vault_preparation` 供最终报告使用。`bootstrapped` /
     `already-bootstrapped` 表示已安全同步；`preserved-for-recovery` 表示为了保留
     active run 的本地 artifact 而有意跳过工作树 pull。

   必须逐项处理决定：

   - `ready`：`mode=started` 表示新运行，`mode=resumed` 表示在本机校验后恢复原
     run；两者都从返回的当前 `phase` 继续，已经 checkpoint 成功的阶段不得重做。
     `mode=resumed` 时必须沿用返回值及 Manifest 中冻结的 `window_days`；不得再从
     当前 prompt 解析时间范围。
     当 `mode=resumed` 且当前 phase 的规范 Stage Report 已存在时，必须先验证并
     `submit` 该报告，不能重新执行阶段。Coordinator 在 `start` 时只会把由现存
     Vault artifact 支撑的报告路径临时纳入 guardian 恢复检查；在 `submit` 成功
     前，它们仍未成为 Manifest checkpoint。
   - `already-published`：同日结果已发布，向用户报告现有输出并结束。
   - `intent-conflict`：同日已有 run，但其冻结的 `window_days` 与当前请求不同。
     展示 existing/requested intent 并停止；不得静默复用、恢复、覆盖或取消。
   - `still-running`：相同 run 的 guardian 仍活跃。报告返回的 exact
     `confirmation_run_id` 并询问用户是否确认原执行者已经中断、要恢复这个同一
     run。没有 exact-ID 明确确认时结束，不得启动第二个写入者，也不得根据运行
     时长抢占。用户确认后，只能用原窗口重新调用 `start` 并增加
     `--confirm-running-run-id "<exact run_id>"`；这只停止旧 guardian 后对同一
     Manifest 执行完整恢复校验，不取消远程所有权，也不删除 artifact。
   - `cancel-confirmation-required`：远程存在 `running` run，但本机没有对应 run
     目录，或对应目录存在但 Manifest 缺失、无法安全恢复。向用户展示返回的 exact
     `run.run_id`、owner、harness、started_at 和 `problem`（如有），并明确询问
     是否取消这个旧 run。**没有用户对该 exact run_id 的明确确认时必须停止，
     绝不允许取消或继续。**
   - `attention-required`：保留现有所有权和产物，向用户展示原因和
     `confirmation_run_id`，询问是否重试这个 exact run。不得自动恢复、取消或
     创建新 run。只有用户明确确认重试该 run_id 后，才重新调用 `start` 并增加
     `--confirm-attention-run-id "<exact run_id>"`。
   - `blocked`：展示 `code` 和 `message` 后停止。不得绕过工作树、配置指纹、
     workflow contract、远程分支或所有权检查。

5. 只有用户明确确认取消 `cancel-confirmation-required` 中展示的 exact
   `run_id` 后，才把 **该次 `start` 返回的原始 `proposal` JSON 对象原样**交回
   Coordinator；不得重新构造、删字段或用后续 `start` 的输出替换：

   ```bash
   python3 "{SKILL_ROOT}/scripts/shared/run_coordinator.py" cancel \
     '<原始 proposal JSON>'
   ```

   可用 `@<独立临时文件>` 传递完全相同的 JSON，但不得使用共享固定临时文件。
   `cancel` 通过 compare-and-set 再次核对 exact `run_id` 和远程 head；返回
   `cancelled` 后重新执行第 4 步 `start`。若返回 `blocked`，说明 proposal 已过期，
   必须停止并展示原因，不能取消变化后的 run。跨机器 run 的安全性始终由用户人工
   确认。
6. `ready` 后按返回的 `phase`，顺序读取并执行以下内部阶段文件，把同一个
   `RUN_MANIFEST` 和第 4 步返回的同一个 `RUNTIME_CONTEXT` 作为只读上下文传给
   每个阶段，并把同一个 `RUNTIME_CONTEXT_FILE` 传给需要配置的脚本：
   - `{SKILL_ROOT}/workflows/fetch.md`
   - `{SKILL_ROOT}/workflows/review.md`
   - `{SKILL_ROOT}/workflows/notes.md`

   `fetching` 从 fetch 开始，`reviewing` 跳过已 checkpoint 的 fetch，
   `writing-notes` 跳过 fetch/review 并从 notes 的详细 checkpoint 继续。
   `validated` 或 `publishing` 不重做内容，只向 Coordinator 提交/恢复发布。

   进入首个阶段前先读取
   `{SKILL_ROOT}/references/stage-report.md`。每个内部阶段把 Stage Report v1
   写入 `RUN_MANIFEST` 的父目录，不得修改 Manifest、Git 或 Vault Task State。
   父流程只把报告交给 Coordinator：

   - fetch 写 `fetch-result.json`，包含完整 acquisition、candidate index、
     approval summary、`candidates` 和 `enriched`。
   - review 写 `review-result.json`，包含推荐页、history 和 Vault changed paths。
   - notes 每完成并验证一篇论文，写唯一的 `notes-progress-<序号>.json`；
     `progress` 只保存详细 checkpoint，不推进 phase。
   - notes 全部成功后写 `notes-result.json`，包含所有实际变更。Coordinator 完成
     `validated`/`publishing` 校验和唯一内容 commit/push；阶段本身不得发布。

   唯一提交形式：

   ```bash
   python3 "{SKILL_ROOT}/scripts/shared/run_coordinator.py" submit \
     "{RUN_MANIFEST}" --report "{STAGE_REPORT}"
   ```

   恢复时必须先检查当前 phase 对应的规范结果报告，以及 notes 的连续 progress
   报告。已存在且有效的报告优先提交，不得重复生成其 artifact；报告格式无效、
   声明路径没有对应现存 artifact，或出现未声明的脏路径时停止并报告，不得据此
   放宽 guardian 或 resume 校验。

7. 任一阶段未成功时，父流程必须先分类，再把结果提交给同一个 Coordinator：

   - 临时网络错误、限流、宿主崩溃等使用 `recoverable`。
   - 重试预算耗尽、需要用户裁决或存在未知文件修改使用 `attention`。
   - 无效配置、不可兼容输入等确定性永久失败使用 `deterministic-failure`。

   把分类、非空 `message`、可选 `retry_at`、证据和已安全落盘的路径写入同一个
   Stage Report，再用 `--report` 提交。Coordinator 会在中断或记录失败 Outcome
   前保存可验证的 artifact、changed paths 和报告证据，包括
   `deterministic-failure`。内部 stage 不得自行提交；未经用户明确要求，不得取消
   active run。
8. 全部完成后，用一句话告诉用户：
   - 推荐文件已生成
   - 重点论文笔记已生成多少篇
   - 目录页是否已自动刷新
   - Git 是否提交/推送

## 重要约束

- 当前入口始终负责 Vault bootstrap、Run Coordinator、`RUN_MANIFEST`、最终验证和 Git
  发布；不得把整个日报编排委派给 Subagent。只有逐篇相关度审批和逐篇论文阅读
  可以按各自 reference 的约定委派，父流程必须等待并逐篇验证。
- 不要先要求用户手动跑 `跑一下论文抓取 / 点评 / 笔记`。
- 三个内部阶段不是用户入口，不得把直接用户请求转交给它们。维护者需要单阶段
  调试时，也必须显式提供已取得所有权的 `RUN_MANIFEST`。
- 不要通过自然语言假设内部阶段已被调用；必须实际读取对应 workflow 文件。
- 不要使用共享固定 `/tmp` 文件。
- Fetch、Review、Notes、paper-reader 和 MOC 阶段不得写 Manifest、Vault Task
  State 或自行提交 Git；它们只向父流程返回结构化报告。
- 不得直接编辑 `.dailypaper/tasks/daily-papers.json`；只能由 Run Coordinator
  间接调用 Vault 协调模块。
- 抢锁或发布 push 被拒绝后不得自动 rebase、重试抢锁或 force push。

## 自动化

- 本 skill 本身就是“一步跑完整流水线”的入口。
- 如果用户想做本地定时任务，默认也应该触发这一句，而不是写死三条内部命令。
