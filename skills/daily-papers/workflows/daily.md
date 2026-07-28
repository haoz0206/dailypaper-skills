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
2. 使用公开 `SKILL.md` 已解析的绝对路径 `SKILL_ROOT`。所有脚本和内部 workflow
   都从 `SKILL_ROOT` 解析，禁止依赖当前工作目录。
3. 先验证本机 onboarding：

   ```bash
   python3 "{SKILL_ROOT}/scripts/shared/machine_config.py" validate
   ```

   如果配置不存在或无效，停止并要求用户先运行公共
   `configure-dailypaper` Skill。不得退回当前工作目录、猜测 Vault，或在 Skills
   仓库中创建输出。
4. 读取本机配置指向的共享 Vault 配置并确定 `VAULT_PATH` 和
   `TIMEZONE = runtime.timezone`。根据当前宿主
   设置 `HARNESS_ID`：Claude Code 使用 `claude-code`，Codex 使用 `codex`；不得根据
   Vault 分支或输出路径猜测。
5. 在创建任何本地 run 文件前，幂等初始化并同步 Vault：

   ```bash
   python3 "{SKILL_ROOT}/scripts/shared/vault_coordination.py" bootstrap \
     --vault "{VAULT_PATH}"
   ```

   这一步会验证固定远程、`main` 和干净工作树。空远程会得到首个可移植配置提交；
   已初始化 Vault 会 fast-forward pull，并只在缺少 bootstrap 文件时提交。任何
   非零状态都必须停止，不能在脏工作树或错误远程上继续。
6. bootstrap 成功后，唯一允许的运行入口是 Run Coordinator 的
   `start-or-resume` 决策。运行：

   ```bash
   python3 "{SKILL_ROOT}/scripts/shared/run_coordinator.py" start \
     --harness "{HARNESS_ID}" --date YYYY-MM-DD
   ```

   完整保留其 JSON 返回；只有 `decision=ready` 时才从 `manifest` 字段取得绝对
   `RUN_MANIFEST` 并执行内部阶段。必须逐项处理决定：

   - `ready`：`mode=started` 表示新运行，`mode=resumed` 表示在本机校验后恢复原
     run；两者都从返回的当前 `phase` 继续，已经 checkpoint 成功的阶段不得重做。
   - `already-published`：同日结果已发布，向用户报告现有输出并结束。
   - `still-running`：相同 run 的 guardian 仍活跃。报告 exact `run_id` 并结束，
     不得启动第二个写入者，也不得抢占。
   - `cancel-confirmation-required`：远程存在 `running` run，但本机没有对应 run
     目录。向用户展示返回的 exact `run.run_id`、owner、harness、started_at，并
     明确询问是否取消这个旧 run。**没有用户对该 exact run_id 的明确确认时必须
     停止，绝不允许取消或继续。**
   - `attention-required`：保留现有所有权和产物，向用户展示原因和
     `confirmation_run_id`，询问是否重试这个 exact run。不得自动恢复、取消或
     创建新 run。只有用户明确确认重试该 run_id 后，才重新调用 `start` 并增加
     `--confirm-attention-run-id "<exact run_id>"`。
   - `blocked`：展示 `code` 和 `message` 后停止。不得绕过工作树、配置指纹、
     workflow contract、远程分支或所有权检查。

7. 只有用户明确确认取消 `cancel-confirmation-required` 中展示的 exact
   `run_id` 后，才把 **该次 `start` 返回的原始 `proposal` JSON 对象原样**交回
   Coordinator；不得重新构造、删字段或用后续 `start` 的输出替换：

   ```bash
   python3 "{SKILL_ROOT}/scripts/shared/run_coordinator.py" cancel \
     '<原始 proposal JSON>'
   ```

   可用 `@<独立临时文件>` 传递完全相同的 JSON，但不得使用共享固定临时文件。
   `cancel` 通过 compare-and-set 再次核对 exact `run_id` 和远程 head；返回
   `cancelled` 后重新执行第 6 步 `start`。若返回 `blocked`，说明 proposal 已过期，
   必须停止并展示原因，不能取消变化后的 run。跨机器 run 的安全性始终由用户人工
   确认。
8. `ready` 后按返回的 `phase`，顺序读取并执行以下内部阶段文件，把同一个
   `RUN_MANIFEST` 作为只读上下文传给阶段：
   - `{SKILL_ROOT}/workflows/fetch.md`
   - `{SKILL_ROOT}/workflows/review.md`
   - `{SKILL_ROOT}/workflows/notes.md`

   `fetching` 从 fetch 开始，`reviewing` 跳过已 checkpoint 的 fetch，
   `writing-notes` 跳过 fetch/review 并从 notes 的详细 checkpoint 继续。
   `validated` 或 `publishing` 不重做内容，只向 Coordinator 提交/恢复发布。

   每个内部阶段只返回结构化报告，不得修改 Manifest、Git 或 Vault Task State。
   父流程验证报告后调用 Coordinator：

   - fetch 成功：登记 `candidates`、`enriched` 两个 artifacts，再提交
     `--result success`，由 Coordinator 从 `fetching` 推进到 `reviewing`。
   - review 成功：登记推荐 Markdown 和 history 两个 artifacts 及其 Vault 相对
     changed paths，再提交 `--result success`，推进到 `writing-notes`。
   - notes 每完成并验证一篇论文：父流程用 `--result progress` 登记该篇的
     artifacts/changed paths 作为详细 checkpoint；progress 绝不推进 phase。
   - notes 全部成功：登记所有实际变更，再提交 `--result success`。Coordinator
     完成 `validated`/`publishing` 校验和唯一内容 commit/push；阶段本身不得发布。

   示例（路径和角色以阶段报告为准，参数可重复）：

   ```bash
   python3 "{SKILL_ROOT}/scripts/shared/run_coordinator.py" submit \
     "{RUN_MANIFEST}" --result success \
     --artifact "candidates={CANDIDATES_OUTPUT}" \
     --artifact "enriched={ENRICHED_OUTPUT}"
   ```

9. 任一阶段未成功时，父流程必须先分类，再把结果提交给同一个 Coordinator：

   - 临时网络错误、限流、宿主崩溃等可恢复中断：
     `--result recoverable --message "..." [--retry-at "..."]`。保留 run，下一次
     公开入口通过 `start` 恢复。
   - 重试预算耗尽、需要用户裁决或存在未知文件修改：
     `--result attention --message "..."`。保留远程所有权，不自动抢占。
   - 无效配置、不可兼容输入等确定性永久失败：
     `--result deterministic-failure --message "..."`。Coordinator 才可写失败
     outcome 和远程任务状态。

   内部 stage 不得自行调用这些命令；它们只返回分类建议、证据和路径，由父流程
   最终判断并提交。未经用户明确要求，不得把 active run 直接取消。
10. 全部完成后，用一句话告诉用户：
   - 推荐文件已生成
   - 重点论文笔记已生成多少篇
   - 目录页是否已自动刷新
   - Git 是否提交/推送

## 重要约束

- 当前入口始终负责 Vault bootstrap、Run Coordinator、`RUN_MANIFEST`、最终验证和 Git
  发布；不得把整个日报编排委派给 Subagent。只有逐篇论文阅读可以按
  `paper-reader` 的约定委派，父流程必须等待并逐篇验证。
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
