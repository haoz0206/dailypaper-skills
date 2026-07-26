---
name: daily-papers
description: |
  每日论文推荐的一句话总入口。用户说“今日论文推荐”“过去3天论文推荐”“过去一周论文推荐”
  “最近3天论文”“看看这周有啥论文”时使用。

  内部会自动串联论文抓取、推荐生成、重点论文笔记三步，无需用户手动拆开。
---

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
2. 将本 `SKILL.md` 所在目录的父目录解析为绝对路径 `SKILLS_ROOT`。所有脚本和内部
   Skill 都从 `SKILLS_ROOT` 解析，禁止依赖当前工作目录。
3. 读取共享配置并确定 `VAULT_PATH` 和 `TIMEZONE = runtime.timezone`。
4. 使用以下命令创建本次运行的独立 manifest，并记住返回的绝对路径
   `RUN_MANIFEST`：

   ```bash
   python3 "{SKILLS_ROOT}/_shared/run_context.py" create \
     --date YYYY-MM-DD --timezone "{TIMEZONE}"
   ```

5. 在抓取论文前，必须通过确定性协调器同步远程 Vault 并取得任务所有权：

   ```bash
   python3 "{SKILLS_ROOT}/_shared/vault_coordination.py" acquire \
     "{RUN_MANIFEST}" --harness codex
   ```

   协调器会验证固定远程和分支、要求干净工作树、执行 `git pull --ff-only`、检查
   同日输出和远程任务状态，然后用独立 commit/push 原子抢占任务。

   - 返回 `acquired`：继续执行。
   - 返回 `already-completed`：当天任务已由任一 harness 完成，直接结束。
   - 返回 `locked`、`lock-raced`、`dirty-worktree`、`wrong-remote` 或其他非零状态：
     停止运行，不得绕过、rebase、force push 或重新生成。
6. 按顺序读取并执行以下内部阶段文件，把同一个 `RUN_MANIFEST` 传给每个阶段：
   - `{SKILLS_ROOT}/daily-papers-fetch/SKILL.md`
   - `{SKILLS_ROOT}/daily-papers-review/SKILL.md`
   - `{SKILLS_ROOT}/daily-papers-notes/SKILL.md`
7. 只有三个阶段全部验证成功后，才允许 notes 阶段调用协调器完成一次内容
   commit/push，并把任务状态更新为 `success`。
8. 如果取得所有权后任一阶段失败，且当前仍持有远程锁，运行：

   ```bash
   python3 "{SKILLS_ROOT}/_shared/vault_coordination.py" fail \
     "{RUN_MANIFEST}" --message "简短失败原因"
   ```

   失败状态必须推送到远程；未完成的本地输出不得发布。进程崩溃留下的
   `running` 状态不得自动抢占，需要人工检查后处理。
9. 全部完成后，用一句话告诉用户：
   - 推荐文件已生成
   - 重点论文笔记已生成多少篇
   - 目录页是否已自动刷新
   - Git 是否提交/推送

## 重要约束

- 不要先要求用户手动跑 `跑一下论文抓取 / 点评 / 笔记`。
- 这 3 句是内部流水线和调试入口，不是首页主交互。
- 如果用户明确只想跑其中一步，再交给对应 skill。
- 不要通过自然语言假设另一个 Skill 已被调用；必须实际读取对应 `SKILL.md`。
- 不要使用共享固定 `/tmp` 文件。
- Review、paper-reader 和 MOC 阶段不得自行提交 Git；完整流水线只在最后提交一次。
- 不得直接编辑 `.dailypaper/tasks/daily-papers.json`；只能调用协调器。
- 抢锁或发布 push 被拒绝后不得自动 rebase、重试抢锁或 force push。

## 自动化

- 本 skill 本身就是“一步跑完整流水线”的入口。
- 如果用户想做本地定时任务，默认也应该触发这一句，而不是写死三条内部命令。
