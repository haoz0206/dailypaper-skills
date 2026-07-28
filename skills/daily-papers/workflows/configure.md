# 配置 DailyPaper

把自然语言配置请求转换成经过验证的共享 Vault 配置。只管理期望配置；不得直接
修改每日任务状态或单次运行 manifest。

## 状态边界

- 可写：`{VAULT_PATH}/.dailypaper/config.json`
- 只读：`{VAULT_PATH}/.dailypaper/tasks/daily-papers.json`
- 不管理：`{VAULT_PATH}/.dailypaper/runs/`
- 不得把绝对 Vault 路径、SSH key、token 或 Harness 安装路径写入共享配置

## Step 0：定位环境

使用公开 `SKILL.md` 已解析的绝对路径 `SKILL_ROOT`；配置脚本路径为
`{SKILL_ROOT}/scripts/configure/config_manager.py`。
确认 `{SKILL_ROOT}/scripts/shared/user_config.py` 和
`{SKILL_ROOT}/scripts/shared/vault_coordination.py` 存在；缺失时说明安装的不是完整
DailyPaper suite 并停止，不得临时复制或猜测默认配置。

按以下顺序确定 `VAULT_PATH`：

1. 使用绝对路径环境变量 `DAILYPAPER_VAULT`。
2. 当前 Git 根目录包含 `.dailypaper/config.json` 时，使用该 Git 根目录。
3. 否则停止并要求用户设置 `DAILYPAPER_VAULT`；禁止把 Skills 仓库误认成 Vault。

共享配置固定为 `{VAULT_PATH}/.dailypaper/config.json`。如果
`DAILYPAPER_CONFIG` 已设置，它必须解析为同一个文件，否则停止并报告冲突。

## Step 1：选择操作模式

- “查看、检查、解释配置”：只读，不写文件、不提交 Git。
- “配置、修改、调整”：更新现有共享配置并发布。
- “初始化配置”：允许调用 Vault bootstrap；这是唯一允许在配置不存在时继续的
  模式。普通修改遇到缺失配置时先说明需要初始化，不得静默创建远程提交。

初始化命令：

```bash
python3 "{SKILL_ROOT}/scripts/shared/vault_coordination.py" bootstrap \
  --vault "{VAULT_PATH}"
```

bootstrap 非零退出时停止。它可能创建并推送 Vault 的首次初始化提交。

## Step 2：同步并检查并发状态

任何更新前必须：

1. 验证 `VAULT_PATH` 是 Git 根目录、当前分支为 `main`，且
   `origin` 为配置约定的固定 Vault 远程。
2. 要求工作树干净。
3. 执行 `git pull --ff-only origin main`。
4. 运行：

   ```bash
   python3 "{SKILL_ROOT}/scripts/configure/config_manager.py" \
     --vault "{VAULT_PATH}" guard
   ```

如果远程任务状态是 `running`，立即停止。不得修改、删除、过期或接管任务状态，
也不得通过改配置让正在运行的日报失效。

只读查看也应先 `pull --ff-only`；工作树不干净时可以读取当前配置，但必须明确说明
结果不是已同步快照，且不得写入。

## Step 3：理解当前配置

运行：

```bash
python3 "{SKILL_ROOT}/scripts/configure/config_manager.py" \
  --vault "{VAULT_PATH}" show

python3 "{SKILL_ROOT}/scripts/configure/config_manager.py" \
  --vault "{VAULT_PATH}" validate
```

解释配置时区分：

- `arxiv_categories`：arXiv API 的硬抓取分类范围，分类之间使用 OR；它不限制
  HuggingFace Daily/Trending 来源。
- `keywords`：抓取后的正向评分；标题命中权重大于摘要。
- `negative_keywords`：标题或摘要命中后硬排除。
- `domain_boost_keywords`：领域相关性加分。
- `min_score`：最终候选最低分。
- `top_n`：每天保留数量；当前多日调用会乘以天数。
- `auto_refresh_indexes`：写入后是否刷新 Obsidian MOC。

当前不支持的请求必须如实报告，禁止写入不会生效的字段。包括：

- 严格 calendar-day arXiv 模式
- 自定义 arXiv API 查询表达式
- 自定义每次 API `max_results`
- 多日调用固定总 `top_n`
- 禁用单个 HuggingFace 来源

这些请求需要先修改 DailyPaper 实现，而不是仅修改配置。

## Step 4：生成和预览 patch

只允许生成以下结构的 JSON patch：

```json
{
  "daily_papers": {
    "arxiv_categories": ["cs.RO", "cs.CV", "cs.AI"],
    "keywords": ["vision-language-action", "robot learning"],
    "negative_keywords": ["medical imaging"],
    "domain_boost_keywords": ["robot", "manipulation"],
    "min_score": 2,
    "top_n": 15
  },
  "automation": {
    "auto_refresh_indexes": true
  }
}
```

patch 只包含用户要求修改的字段。所有关键词使用小写；数组顺序表达用户优先顺序。
将 patch 写入本次唯一的临时文件，不使用共享固定文件名。

先运行只读预览：

```bash
python3 "{SKILL_ROOT}/scripts/configure/config_manager.py" \
  --vault "{VAULT_PATH}" plan --patch "{PATCH_PATH}"
```

向用户总结实际变化以及对抓取/筛选范围的影响。脚本拒绝未知字段、错误类型、空分类、
重复项、正负关键词冲突和不安全的共享配置。

## Step 5：应用和发布

用户已经明确要求修改时，预览与其意图一致即可执行：

```bash
python3 "{SKILL_ROOT}/scripts/configure/config_manager.py" \
  --vault "{VAULT_PATH}" apply --patch "{PATCH_PATH}"

python3 "{SKILL_ROOT}/scripts/configure/config_manager.py" \
  --vault "{VAULT_PATH}" validate
```

`apply` 会在原子替换 `.dailypaper/config.json` 的最后一刻再次读取任务状态；即使
`plan` 之后有日报任务抢先取得所有权，也必须拒绝写入。不得绕过这道脚本内检查。

然后：

1. 确认只有 `.dailypaper/config.json` 被修改。
2. 只暂存该文件，禁止 `git add -A`。
3. commit message 使用 `configure daily papers`。
4. 普通 push 到 `origin main`。
5. push 失败时保留本地提交并停止；不得自动 rebase、force push 或覆盖远程配置。

结束时报告配置路径、变化摘要、commit/push 结果。删除临时 patch。

## 配置原则

- 缩小真正的网络抓取范围优先修改 `arxiv_categories`；关键词只影响抓取后的筛选。
- 用户说“只抓这些分类”时，明确说明分类限制只作用于 arXiv；当前合并候选仍包含
  HuggingFace 来源。
- 不要仅因用户说“更严格”就猜测字段；根据当前命中逻辑说明
  `negative_keywords`、`min_score` 和分类收窄的不同影响。
- 共享数组 patch 会替换整个数组。保留用户仍然需要的旧值，不要只写新增项。
- Zotero 数据库和服务器绝对路径属于 per-machine 配置，不写入共享 Vault。
- repository、稳定输出目录和任务状态文件不是本 Skill 的可配置项。
