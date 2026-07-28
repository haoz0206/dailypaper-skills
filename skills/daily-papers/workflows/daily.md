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
6. 使用以下命令创建本次运行的独立 manifest，并记住返回的绝对路径
   `RUN_MANIFEST`：

   ```bash
   python3 "{SKILL_ROOT}/scripts/shared/run_context.py" create \
     --date YYYY-MM-DD --timezone "{TIMEZONE}"
   ```

7. 在抓取论文前，必须通过确定性协调器同步远程 Vault 并取得任务所有权：

   ```bash
   python3 "{SKILL_ROOT}/scripts/shared/vault_coordination.py" acquire \
     "{RUN_MANIFEST}" --harness "{HARNESS_ID}"
   ```

   协调器会验证固定远程和分支、要求干净工作树、执行 `git pull --ff-only`、检查
   同日输出和远程任务状态，然后用独立 commit/push 原子抢占任务。

   - 返回 `acquired`：继续执行。
   - 返回 `already-completed`：当天任务已由任一 harness 完成，直接结束。
   - 返回 `locked`、`lock-raced`、`dirty-worktree`、`wrong-remote` 或其他非零状态：
     停止运行，不得绕过、rebase、force push 或重新生成。
8. 按顺序读取并执行以下内部阶段文件，把同一个 `RUN_MANIFEST` 传给每个阶段：
   - `{SKILL_ROOT}/workflows/fetch.md`
   - `{SKILL_ROOT}/workflows/review.md`
   - `{SKILL_ROOT}/workflows/notes.md`
9. 只有三个阶段全部验证成功后，才允许 notes 阶段调用协调器完成一次内容
   commit/push，并把任务状态更新为 `success`。
10. 如果取得所有权后任一阶段失败，且当前仍持有远程锁，运行：

   ```bash
   python3 "{SKILL_ROOT}/scripts/shared/vault_coordination.py" fail \
     "{RUN_MANIFEST}" --message "简短失败原因"
   ```

   失败状态必须推送到远程；未完成的本地输出不得发布。进程崩溃留下的
   `running` 状态不得自动抢占，需要人工检查后处理。
11. 全部完成后，用一句话告诉用户：
   - 推荐文件已生成
   - 重点论文笔记已生成多少篇
   - 目录页是否已自动刷新
   - Git 是否提交/推送

## 重要约束

- 当前入口始终负责 Vault bootstrap、任务所有权、`RUN_MANIFEST`、最终验证和 Git
  发布；不得把整个日报编排委派给 Subagent。只有逐篇论文阅读可以按
  `paper-reader` 的约定委派，父流程必须等待并逐篇验证。
- 不要先要求用户手动跑 `跑一下论文抓取 / 点评 / 笔记`。
- 三个内部阶段不是用户入口，不得把直接用户请求转交给它们。维护者需要单阶段
  调试时，也必须显式提供已取得所有权的 `RUN_MANIFEST`。
- 不要通过自然语言假设内部阶段已被调用；必须实际读取对应 workflow 文件。
- 不要使用共享固定 `/tmp` 文件。
- Review、paper-reader 和 MOC 阶段不得自行提交 Git；完整流水线只在最后提交一次。
- 不得直接编辑 `.dailypaper/tasks/daily-papers.json`；只能调用协调器。
- 抢锁或发布 push 被拒绝后不得自动 rebase、重试抢锁或 force push。

## 自动化

- 本 skill 本身就是“一步跑完整流水线”的入口。
- 如果用户想做本地定时任务，默认也应该触发这一句，而不是写死三条内部命令。
