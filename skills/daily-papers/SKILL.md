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
3. 读取共享配置并确定 `VAULT_PATH` 和 `TIMEZONE = runtime.timezone`。如果启用了
   Git 自动化，开始前要求 Vault 工作树干净；发现已有修改时停止自动提交，但可以在
   用户确认后继续生成文件。
4. 使用以下命令创建本次运行的独立 manifest，并记住返回的绝对路径
   `RUN_MANIFEST`：

   ```bash
   python3 "{SKILLS_ROOT}/_shared/run_context.py" create \
     --date YYYY-MM-DD --timezone "{TIMEZONE}"
   ```

5. 按顺序读取并执行以下内部阶段文件，把同一个 `RUN_MANIFEST` 传给每个阶段：
   - `{SKILLS_ROOT}/daily-papers-fetch/SKILL.md`
   - `{SKILLS_ROOT}/daily-papers-review/SKILL.md`
   - `{SKILLS_ROOT}/daily-papers-notes/SKILL.md`
6. 只有三个阶段全部验证成功后，才允许 notes 阶段执行一次最终 Git commit/push。
7. 全部完成后，用一句话告诉用户：
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

## 自动化

- 本 skill 本身就是“一步跑完整流水线”的入口。
- 如果用户想做本地定时任务，默认也应该触发这一句，而不是写死三条内部命令。
