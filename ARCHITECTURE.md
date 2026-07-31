# Architecture

这份文档记录各模块的实现逻辑，方便想改代码或理解内部机制的人参考。

## 整体架构

```
用户说一句话
    │
    ├─ configure-dailypaper（安装后首个入口；本机路径 + 共享研究配置）
    ├─ daily-papers（每日推荐）
    │    └─ workflows/daily.md
    │         ├─ fetch.md（完整元数据 + 逐篇低成本审批）
    │         ├─ review.md（当前 agent 点评）
    │         └─ notes.md（当前 agent + paper-reader workflow）
    ├─ paper-reader（手动读单篇论文 / Zotero）
    └─ generate-mocs（手动刷新 Obsidian 索引）
```

`skills/daily-papers/` 是完整日报能力的自包含深模块。`paper-reader`、
`generate-mocs` 和 `configure-dailypaper` 是公开、可独立安装的聚焦入口；
它们由 `tools/sync_public_skills.py` 从日报模块中的规范 workflow 和最小运行资源
生成，避免复制实现发生漂移。安装器发现四个公共 `SKILL.md`，而 fetch、review、
notes 三个流水线阶段仍然只作为 `daily-papers` 的内部资源。

### 公共 Skill 的岗位、依赖与复用边界

| Skill / 岗位 | 独立用户价值 | 依赖的共享能力 | 不负责 |
| --- | --- | --- | --- |
| `configure-dailypaper` | 首次 onboarding、查看/修改配置 | `config_schema`、machine config、active-run guard、Vault coordination | 抓论文、写笔记、刷新 MOC |
| `daily-papers` | 完整日报入口和唯一协调者 | 全部共享能力；内部顺序调用 fetch → review → notes | 把内部阶段暴露成独立入口 |
| `paper-reader` | 手动精读一篇论文并保存 | Standalone Session、`paper_identity`、note validator、MOC builder | 日报 Manifest、批量发布 |
| `generate-mocs` | 手动重建 Obsidian 导航 | Standalone Session、MOC plan/apply | 读论文、改配置 |

内部 fetch 是确定性采集与逐篇相关度审批岗位；review 是主模型筛选和写作岗位；notes 是逐篇
编排与最终内容收口岗位。它们不是三个可独立安装的 Skill，因为 review 依赖
fetch 的 enriched artifact，notes 又依赖 recommendation、history、匹配报告和
父 Run 的锁。把它们公开会制造悬空输入和第二套生命周期。

曾经重复的流程现统一为以下深模块：

| 重复流程 | 唯一实现 |
| --- | --- |
| 有界 nofollow 读取、单描述符检查/复制/哈希、严格 JSON 编解码与 durable atomic replace | `safe_io.py` |
| portable POSIX 相对路径解析、长度预算与 symlink containment | `safe_path.py` |
| 单篇候选 Markdown、Evaluation v1 校验、缺失审批恢复与候选汇总 | `candidate_approval.py` |
| Git 命令预算、仓库身份/dirty snapshot、blob OID 固定、有界读取与 index 版本守卫 | `safe_git.py` + `safe_process.py` |
| 配置合并、字段验证、路径安全、指纹 | `config_schema.py` |
| 配置同步、原子应用、无 patch 恢复和精确发布 | `config_manager.py prepare/apply/resume` + `run_guardian.py` |
| 首次机器配置的 clone、bootstrap、持久化顺序 | `configure/onboard.py` |
| 日报 onboarding 验证、bootstrap、同步、恢复判定 | `run_coordinator.py start` |
| Guardian 后台启动、就绪证明、超时清理和进程回收 | `run_guardian.ensure_guardian_running` |
| 独立写入准备、锁、恢复、精确变更集与发布 | `standalone_coordinator.py` + `run_guardian.py` |
| 论文身份、已有笔记匹配、重名处理 | `paper_identity.py` |
| 论文笔记结构与期望身份校验 | `validate_paper_note.py` |
| 论文与概念 MOC 原子规划及刷新 | `refresh_mocs.py` + `moc_builder.py` plan/apply |
| 日报阶段状态、断点和发布 | `run_coordinator.py` + `run_lifecycle.py` |

这里有三类看似重复但不应强行合并：

- 日报发布、独立 Skill 发布和配置发布都调用 Git，但三者的事务单位分别是
  Task State + Run Change Set、Standalone Session artifacts、单个共享配置文件。
  它们共享 Vault writer lock 和底层安全约束，不共享一个“大而全”的 Git
  workflow，避免某个岗位获得不属于它的暂存或恢复权限。
- `paper-reader` 在日报 notes 阶段和手动入口中复用同一份阅读、身份和质量规则，
  但生命周期 adapter 不同：前者只能向父 Run 提交 artifact，后者只能向
  Standalone Session 提交 artifact。内容能力复用，所有权不能复用。
- fetch、review、notes 都生成 Stage Report，是因为它们处于同一状态机的不同
  checkpoint，并不是三个可互换入口。报告解析和校验集中在 `stage_report.py`，
  各岗位只负责生成自身证据。

低层文件替换已统一到 `safe_io.atomic_write_bytes`；它负责随机同目录临时文件、
精确 mode、file/directory fsync、nofollow 目标检查和原子 replace。它刻意不是
CAS：Manifest revision、MOC plan/apply、图片笔记更新、配置 journal 和 Vault
publication 仍由各自领域模块持有锁、hash、备份与恢复语义，避免通用 helper
隐藏所有权边界。面向远程内容和外部工具的读取也有明确的时间与字节预算；PDF
affiliation fallback 不再通过 shell pipeline 传递无界数据。所有 Git 子进程统一
具有 deadline 和 stdout/stderr 上限；仓库 root/remote/branch 由一个只读 snapshot
接口解析，dirty paths 用一次 NUL-delimited status 获取，并同时保留 rename/copy
的源、目标路径。配置、Task State 和 standalone artifact 的 blob 检查还会先把
ref/index expression 固定为 immutable OID，再读取 object size 并按调用方上限
物化内容。日报与 standalone publication 还共用 index 版本守卫：每个待发布路径
在 `git add` 前只能是 base blob、已登记 artifact blob，或受控的新建/删除状态，
从而不会覆盖用户单独 staged 的第三个版本。Git 发布事务本身仍由三个领域协调器
拥有。

状态 JSON 的反向路径同样集中在 `safe_io.py`：
`encode_json_value` 统一 UTF-8、key 排序、缩进、禁止 NaN、单个结尾换行以及
编码后的字节预算；`atomic_write_json` 再组合 durable replace。需要先比较字节、
计算 hash 或写 current/previous 双快照的领域只调用前者，直接替换单个状态文件
的领域调用后者，因此共享编码规则不会吞掉 Manifest、Task State 或配置事务语义。
fetch 与 enrich 的数组 artifact 也在写文件或 stdout 之前先经同一编码器施加输出
字节预算，外部元数据不能绕过序列化上限制造无界的中间文件。

长期 guardian 是有意保留的后台进程，但它的创建不是协调器职责。
`run_guardian.ensure_guardian_running` 会先复用已经响应的 guardian，否则启动
一个隔离 session 的子进程，并在统一 deadline 内证明 socket 已就绪；子进程提前
退出或超时都会被回收，超时后仍存活的进程会被终止。日报和独立 Skill 因而不再
各自维护 `Popen`、轮询或清理实现。

所有来自配置、Task State、Stage Report、Manifest、Git status 或独立会话的相对
路径先通过 `safe_path.py` 的同一 portable POSIX 语法和长度预算；需要落到磁盘时
再进行 symlink-aware root containment。`.git/.dailypaper` 是否允许以及路径属于
Run 还是 Vault 仍由各领域模块决定，因此共享 parser 不会扩大任何岗位的写权限。

`tools/sync_public_skills.py` 只复制每个公开岗位真正依赖的最小闭包，并在测试中把
每个 Skill 单独复制到临时目录运行，防止“开发仓库能跑、安装后缺兄弟目录”。
规范源文件经有界 nofollow 读取，目标树在排序/比较前限制 entry 与深度；重复资源、
symlink 和特殊文件会阻止同步，目标文件使用 durable atomic replace。`--check`
同时检测内容、额外文件和空目录漂移；正式同步会清理过期生成资源和安装目录中的
隐式配置覆盖。研究范围只由版本化 Vault 共享配置提供；机器路径只由跨 Harness
的 machine config 提供，因此四个独立安装包不会因升级或各自产生隐式 overlay
而发生配置和指纹漂移。

三步流水线的设计主要是为了控制单次上下文长度。入口只调用
`run_coordinator.py start`，由协调器完成 onboarding 验证、必要的 bootstrap、
正常运行前同步，并决定创建新 Run、验证并恢复同机 Run，或停止。
三个阶段通过 Manifest v2 登记的运行级 JSON 路径传递数据，避免并发和失败重跑读到
其他任务的文件。

Coordinator 先用固定仓库端点 fetch 远程 HEAD/Task State，再决定是否允许同步和
加载本地共享配置；因此另一台机器的 `running` 状态不会被缺失或陈旧的本地配置
遮蔽。安全同步后只解析一次最终 Runtime Context，把它放进 `start` 的 `ready`
响应，并冻结为 run-local `runtime-context.json`。fetch、review、notes 以及它们
调用的子进程都复用同一个对象/文件，不再各自读取默认配置和覆盖层，因此一次 Run
内不会出现不同阶段看到不同配置的情况。若远程任务处于 `running`，Coordinator
会跳过要求干净工作树的
bootstrap，保留当前 run 已落盘但尚未 checkpoint 的合法 artifact，再用 Manifest
和 Stage Report 约束恢复；这避免外层预检反过来破坏 resume 能力。

所有磁盘 JSON 首先经过 `safe_io.py` 的有界、nofollow、regular-file、严格 UTF-8
入口；重复 key、`NaN` 和非 object 根在进入领域逻辑前即被拒绝。普通持久化文件也
通过该模块完成 durable atomic replace，而领域 CAS 和 journal 保留在其所有者中。
所有配置规则集中在 `config_schema.py`。`user_config`、配置 Skill、Runtime Context
和协调器指纹都经过这个 seam；它负责版本化共享文档、legacy overlay 显式迁移、
规范化、路径隔离、固定仓库/时区策略以及 fingerprint。已配置用户的有效设置来自
机器文件与完整 Vault 快照，包内 defaults 只用于 bootstrap 和迁移。
`user_config` 返回缓存内容的深拷贝，
调用方不能通过修改 dict 污染后续阶段。机器 Zotero 路径最后注入，任何 Vault
共享配置或本地 overlay 都不能覆盖它。

论文身份规则集中在 `paper_identity.py`。fetch 为候选生成版本无关的
`paper_id`；review 用它建立已有笔记匹配报告；notes 和 paper-reader 用同一 ID
验证、补全或新建笔记；链接回填也复用同一个索引。稳定 ID 支持 arXiv、DOI、
本地 PDF SHA-256 和 Zotero key。旧笔记仍可用唯一方法名/完整标题兜底，但任何
重名都会保留为歧义，不会像旧字典索引那样静默覆盖。

独立 `paper-reader` 和 `generate-mocs` 只调用
`standalone_coordinator.py start/submit/inspect/cancel`。协调器内部复用
Runtime Context 和 remote active-run guard，并持有与日报相同的本机 Vault
writer lock。会话冻结配置、base HEAD、初始 dirty 路径及 artifact hash；异常后
同 intent 自动恢复，未知 dirty 路径和已登记 artifact 变化进入
`attention-required`。发布只暂存严格 change set，commit 后先持久化 SHA，再普通
push；回包丢失和 push 重试复用同一个 commit。常规 `submit --result --path`
由协调器计算 artifact hash；严格 report 只保留为 Subagent 冻结交接格式。空变更
以 `unchanged` 终止，不生成空 commit。暂存前还会比较 index blob，只接受 base
版本或已登记 artifact 版本，避免覆盖用户仅存在于 Git index 的修改。

安装后必须先运行 `configure-dailypaper`。服务器推荐使用
`/workspace/dailypaper-vault`；该绝对路径默认写入本机
`~/.config/dailypaper/config.json`，不会进入 Git。四个公共 Skill 启动时都会验证
机器配置；缺失或无效时停止并要求配置，不会把当前工作目录当成 Vault。
`DAILYPAPER_VAULT` 只作为临时显式覆盖。

Vault 内跟踪的
`.dailypaper/config.json` 使用相对值 `"."`，从而允许 Mac、服务器和其他 harness
在不同 clone 路径上共享同一配置指纹。

首次配置由 `configure/onboard.py` 把 clone/已有仓库验证、bootstrap 和机器文件
持久化收进一个接口；机器文件只会在 bootstrap 成功后写入。其内部调用
`vault_coordination.py bootstrap` 处理空远程：验证固定 `origin`、`main`，创建
可移植配置及忽略 `.dailypaper/runs/` 的 `.gitignore`。
Git-dir journal 在任何工作树修改前持久化，文件通过原子 replace 写入，commit
由固定 tree/parent/身份/时间生成。任一写入、stage、commit、push 或响应丢失窗口
中断后都复用同一事务；两个 clone 并发初始化也收敛到同一个 commit，绝不 force
push 或自动 rebase。

---

## Run lifecycle v2

### 两层状态权威

- Vault 中跟踪的 `.dailypaper/tasks/daily-papers.json`（Vault Task State）是
  跨机器、跨 Harness 的所有权权威。只有 acquisition commit 普通 push 成功的
  `run_id` 可以发布。
- 本机忽略的 `.dailypaper/runs/<run-id>/manifest.json`（Run Manifest）是同一台
  机器上的恢复权威，记录生命周期、checkpoints、产物、Run Change Set、配置指纹和
  Workflow Contract。Manifest 本身不能取得或释放远程所有权。

Manifest v2 不再用一个含糊的 status 同时表达所有状态：

| 维度 | 值 |
| --- | --- |
| Phase | `prepared → fetching → reviewing → writing-notes → validated → publishing` |
| Condition | `active`、`interrupted`、`attention-required` |
| Outcome | 不可变的 `published`、`failed`、`cancelled` |

Phase 只能向前推进；恢复当前 Phase 以及重复提交内容不变的 `progress` checkpoint
是幂等的。协调器同时保存生命周期级 checkpoint 和阶段内 progress checkpoint；
大文件留在 run 目录，Manifest 只记录受验证的路径、元数据和 hash。

### 协调器接口

Harness 只通过四个外部操作驱动日报：

- `start`：bootstrap 后执行 start-or-resume，返回确定性的下一步。
- `submit --report`：读取当前 run 目录中的 Stage Report v1。报告把 artifact
  明确标为 `run`/`vault` scope，并把 changed path 表示为 Vault 相对路径；
  协调器绑定当前 Phase、验证路径和 hash 后决定 checkpoint、推进或中断，调用者
  不能指定下一 Phase。
- `inspect`：只读查看本地生命周期与最新远程任务状态。
- `cancel`：必须绑定用户确认的准确 `run_id`，并在 fresh fetch 后以远程 HEAD 和
  `run_id` 做 compare-and-set。

`RunLifecycle` 是 Manifest 的唯一受支持写入模块。每次变更都取得短期 manifest
文件锁，检查 revision，写入临时文件并 `fsync + replace` 原子替换，同时保留上一
revision 备份。`run_guardian.py` 持有整个运行期间的本地 execution/liveness
`flock`，用于防止同机并发和提供诊断；guardian 不写 Manifest。
生产协调器启动的 guardian 没有 idle expiry，运行时长不能成为抢占依据。若原
Harness 已中断但 detached guardian 仍存活，入口必须展示 exact `run_id` 并取得
用户确认，随后只停止 guardian、对同一 Manifest 做完整恢复校验；不得取消远程
所有权或删除 artifact。

`run_coordinator.py` 在 `start` 和每次重新打开 Manifest 的 mutation 边界再次调用
严格 Runtime Context 校验。工作流阶段和子进程复用入口冻结的同一个上下文以避免漂移；
协调器的重复校验用于防止绕过 workflow prompt 的直接 CLI 调用，以及运行期间配置
被改成无效结构。

父 Run Coordinator 是唯一允许调用 Manifest mutation、Vault ownership 和 Git
publication 的执行者。Subagent 只能生成候选产物或返回阶段报告；它不能写
Manifest、持有 guardian lock 或提交 Vault。

### 恢复与取消

同机异常中断可以沿用原 `run_id`，但 `start` 必须重新验证远程所有权、Manifest
revision、配置指纹、Workflow Contract、checkpoint 和已登记 artifact hash。只有
Run Change Set 中的已登记修改可以存在；未知 dirty path 或注册文件 hash 变化会
阻止恢复，以保护用户手动修改。临时网络、限流或进程崩溃进入 `interrupted`；
确定性无效配置等进入终态 `failed`；自动重试预算耗尽进入
`attention-required`，并继续保留所有权等待用户，不会自动 resume。只有用户明确
确认重试 exact `run_id` 后，入口才用
`start --confirm-attention-run-id <run-id>` 恢复同一个 run。
如果 guardian 仍响应，则不能据此自动判断原 Harness 是否存活。只有用户确认 exact
`run_id` 后，`start --confirm-running-run-id <run-id>` 才能停止旧 guardian 并
沿用同一 run；确认 ID 不匹配时安全拒绝。

进入 Git publication 前，`RunLifecycle` 会再次以 nofollow、有界哈希复核全部
已登记 artifact，并检查每个当前存在的 Run Change Set 路径都有对应的 Vault
artifact。这样 checkpoint 之后被替换的文件、symlink，以及后来才出现的未登记
文件都不能进入 content commit；单个协调式 Run artifact 的上限为 64 MiB。

如果阶段已经写完 Vault artifact 和 Stage Report、但 guardian 或父进程在
`submit` 前崩溃，`start` 会严格读取当前 phase 的规范 report。只有同时出现在
`changed_paths` 且由现存 Vault artifact 逐路径佐证的修改，才能临时进入恢复
allow-set；无关的用户 dirty path 仍阻止恢复。report 随后必须正常 submit 并登记
hash。`deterministic-failure` 也先保存这些安全证据 checkpoint，再发布失败状态。

如果 Vault Task State 仍为 `running`，但本机没有对应的 run 目录，说明它来自另一
台机器。系统不做跨机器 resume 或 lease 抢占，而是让 AI 展示准确 `run_id` 并询问
用户是否取消。确认后 `cancel` fresh fetch；仅当远程 HEAD 和仍在运行的 `run_id`
都与提案一致时写入 `cancelled`。状态发生变化则安全拒绝。取消后的本地产物不会
自动删除。

每日请求的 `window_days` 是 1–31 的不可变 Run intent。入口只解析一次，Manifest、
远程 Task State、恢复和发布都验证相同值；fetch 只读取 Manifest。相同日期但窗口
不同会返回 `intent-conflict`，不会复用已有日报或用当前 prompt 改写恢复中的 run。

### 幂等发布

进入 `publishing` 后，协调器只暂存 Run Change Set 与任务状态，创建并记录一个固定
内容 commit。恢复时：

1. 远程仍是 acquisition commit：重新 push 同一个内容 commit。
2. 远程已经是该内容 commit：直接标记 `published`。
3. 远程是其他提交：进入 `attention-required`，保留现场。

任何路径都不允许自动 rebase、force push 或重新生成另一个内容 commit。

---

## Step 1: fetch workflow

抓取和 artifact 处理使用确定性 Python；只有逐篇相关度审批消耗低成本模型 token。

### 1.1 完整元数据抓取（fetch_and_score.py）

数据源：
- HuggingFace Daily Papers API：`https://huggingface.co/api/daily_papers?date=YYYY-MM-DD`
- HuggingFace Trending API：`https://huggingface.co/api/daily_papers?sort=trending`
- arXiv API：`https://export.arxiv.org/api/query`，分类来自
  `daily_papers.arxiv_categories`，日期来自冻结的 Run window

arXiv 使用 `start` / `max_results` 分页，并在每页验证稳定的 `totalResults`。
超过 3000 篇安全上限、响应提前结束、总数变化或条目缺少标题/摘要/稳定 ID 时，
snapshot 不能证明完整，阶段失败而不是静默截断。

非空 arXiv snapshot 是选定分类的权威 acquisition 集合；HuggingFace 只为其中
相同 arXiv ID 叠加 upvote/trending 信号。只有 arXiv snapshot 已完整证明为空时，
才使用不超过 3200 篇总 acquisition 上限的 HF fallback，以保留周末/节假日行为。
抓取顺序优先 arXiv，避免可选 HF 请求消耗共享预算后妨碍完整性证明。

确定性信号：
- 命中 `keywords`：标题 +3，摘要 +1
- 命中 `domain_boost_keywords`：+1~2
- 命中 `negative_keywords`：扣分，但不删除论文
- Trending 加分：根据 upvotes 分档（5 / 10 / 20），相关论文 +1~3，不相关的只有 20+ upvotes 才加分

去重：
- 按稳定 arXiv ID 合并
- 单天模式：跟 `.history.json` 交叉标记，但不从 acquisition/审批池删除
- 周末模式：把 5+ upvotes 的热门历史论文标为可再推荐
- 多天模式（days > 1）：历史不影响后置入选资格
- 新论文不足 20 篇时，把得分最高的历史论文标为可回补；其他历史论文仍接受语义审批

输出：`{RUN_DIR}/acquired-papers.json` 和完整性
`{RUN_DIR}/acquisition-summary.json`。summary 记录 acquired artifact SHA-256、
完整 arXiv scope/count 和后置入选计数；关键词信号不拥有相关性否决权。

### 1.2 单篇相关度审批（candidate_approval.py）

`prepare` 先验证 acquisition summary 与原始 metadata 的 SHA-256 绑定，再为每篇
acquired paper 生成独立 Markdown，并把 summary/metadata SHA-256、规范 paper
payload、Markdown SHA-256 和预期 Evaluation 路径写入
`candidate-index.json`。候选文件和逐篇审批都在 Run 目录，不进入 Vault。

支持 Subagent 时，fetch workflow 使用最多 8 个低成本 worker；每个逻辑任务只读取
一篇候选和冻结的研究配置，并写一个 Evaluation v1：

- `approve`：直接相关或有明确可迁移价值；
- `uncertain`：交给主评审复核；
- `reject`：可以基于标题和摘要明确排除。

恢复时若 index 存在，workflow 必须先运行 `pending` 并复用已绑定 artifact，不得
重新抓取动态数据或覆盖已有审批；只有 index 缺失时才从已绑定 acquisition 运行
`prepare`，两者都缺失时才重新抓取。`pending` 只返回缺失审批。`collect` 重新验证
source、index、候选 SHA-256、`paper_id` 和 evaluation input hash，保留 `approve`、
`uncertain`，并用 `min_score` 救回可能被模型误拒的强关键词论文；随后按每日
`top_n` 形成 `{RUN_DIR}/candidates.json`。历史不合格项仍被评估和计数，但不进入
最终候选。Subagent 是执行 Adapter，不是生命周期所有者；不支持 Subagent 的
Harness 通过同一 Interface inline 执行。

### 1.3 元数据富化（enrich_papers.py）

用 `asyncio` + `safe_http.py` 并发（Semaphore=10）请求 arXiv 页面。所有跳转逐跳
重新校验，DNS 结果固定到公开地址，并统一限制响应头、编码、单请求/总字节数和
单请求/总时限；同一阶段的 endpoint、重试和并发 worker 共享一个线程安全的
FetchBudget，不能把“每请求有上限”误当成阶段总上限。失败时指数退避重试，预算
耗尽则立即停止重试。输入同时限制 JSON 字节与论文条目数，协程按 Semaphore
大小分批创建，而不是先为整个不可信数组物化 Task。文件下载在流式写入时只计算
一次 SHA-256，
后续媒体魔数检查通过不可变文件身份读取小前缀，不再完整重读 PDF 或图片。
`pdftotext` 与 `pdfimages` 统一经 `safe_process.py` 运行：不经过 shell，stdout
和 stderr 分别受限，超时或输出溢出时终止并回收完整进程组；图片输出另受子进程
文件大小上限约束。PDF 图片 fallback 先运行有界的 `pdfimages -list`，根据图像
数量、像素尺寸、颜色分量和位深拒绝过大的解码计划；提取后再核对实际文件数量、
单文件大小与总大小。外部工具生成的候选图片只通过一个 nofollow 文件描述符完成
大小、魔数和 SHA-256 检查；发布时同一描述符把完全相同的字节复制到 staging，
内容地址由这次复制的快照生成，避免 `stat`、检查、哈希和复制之间的路径竞态。

提取内容：
- 从 arXiv HTML 提取：首图 URL、作者、机构、章节标题、图表标题、方法名、是否有真机实验
- HTML 缺少作者或机构时 fallback 到 arXiv abs 页面的 `<meta>` 标签
- 机构仍为空时，将 PDF 有界下载到隔离临时目录，再用受限 `pdftotext` 提取

输出：`{RUN_DIR}/enriched.json`

---

## Step 2: review workflow

**当前 agent 主导，读候选列表写点评。**

### 2.1 匹配已有笔记

运行 `paper_identity.py match` 一次，读取论文笔记 frontmatter 并输出 Run 内的
`note-matches.json`。匹配优先级是稳定 `paper_id` → 唯一方法名 → 唯一完整标题；
重名为 `ambiguous`，不会自动选择。review 不再把整个 Vault 文件名和概念目录塞进
模型上下文。

### 2.2 写锐评

当前 agent 以"毒舌但有料的资深研究员"角色点评每篇论文：
- 分流表：🔥 必读 / 👀 值得看 / 💤 可跳过
- 每篇包含：作者、机构、链接、来源、核心方法（带 `[[概念]]` 链接）、对比方法、借鉴意义、锐评
- 已有笔记的论文走简化格式
- 跟用户方向完全无关的论文可以跳过，列出跳过原因

硬性约束：
- 不能凭空说"只有仿真"——必须检查 `has_real_world` 字段
- 不能说某篇是"山寨"——除非有具体方法论证据
- 不确定的信息必须注明"摘要未提及"

### 2.3 保存

- 写入 `{DAILY_PAPERS_PATH}/YYYY-MM-DD-论文推荐.md`
- 更新 `.history.json`：追加今日推荐的 arXiv ID + 标题，只保留最近 30 天
- 将产物候选和阶段进度返回父协调器；由父协调器校验 hash、登记 Run Change Set，
  不在本阶段提交

---

## Step 3: notes workflow

**当前 agent 编排 + 多次执行 paper-reader workflow。**

### 3.1 概念库补充

1. 扫描推荐文件里所有 `[[概念]]` 链接 + enriched JSON 的 `method_names`
2. 过滤：只保留方法 / 模型 / 数据集 / 仿真器 / 技术概念名，排除通用词、论文标题、人名
3. 自动分类到 16 个概念子目录（生成模型 / 强化学习 / 机器人策略 / 3D 视觉 / 仿真器 / 数据集等）
4. 创建概念笔记：定义 + 数学形式 + 核心要点 + 代表工作 + 相关概念

### 3.2 论文笔记生成

- 只为"🔥 必读"论文生成完整笔记
- 已有笔记由统一结构验证器检查结构和期望 `paper_id`；不合格时保留文件并原位补全
- 逐篇执行 paper-reader workflow

质量校验（每篇）：
- 文件 ≥ 120 行
- 包含 LaTeX 公式（≥ 2 处）
- 包含图片引用（≥ 1 处）
- 包含 `## 关键公式`、`## 关键图表` 和 `## 实验结果` section
- 不达标时保留已有成果，按结构验证报告补全；再次失败进入用户关注状态

### 3.3 链接回填

在推荐文件中，给已有笔记的论文插入 `📒 **笔记**: [[NoteName]]` 链接。

### 3.4 刷新目录页 + git

- 调用一次 `refresh_mocs.py --scope all`
- 将完成产物返回父协调器；全部验证成功后，由 `run_coordinator.py submit` 进入
  `validated` / `publishing`，并经 `vault_coordination.py` 创建或复用一次精确
  暂存的内容 commit/push
- acquisition commit 在抓取开始前已经取得跨机器任务所有权
- acquisition 使用同步完成后冻结的 Runtime Context 和已检查的 remote HEAD 做
  compare-and-set，不再在获取内部重复 pull 或重新读取配置
- 若锁 commit 已推送、但进程在本地记录 acquisition 前崩溃，下次 `start` 会从
  `prepared` 校验相同 `run_id`、配置指纹和 remote HEAD 后补记并进入 `fetching`

---

## paper-reader workflow

**既是独立公共 Skill，也由 notes workflow 作为内部能力调用。**

公共入口只保留岗位决策、论文质量规则和对共享契约的路由。独立会话的 start、
submit、resume、cancel、哈希与 Git 语义集中在打包的
`references/standalone-session.md`，仅独立调用时完整读取；日报内部调用复用父流程
上下文，不加载或重复执行该契约。

### 输入源

| 来源 | 处理方式 |
|------|----------|
| arXiv 链接 | 优先获取 arXiv HTML，必要时下载 PDF |
| 本地 PDF | 直接读取 |
| Zotero 搜索 | 查 DB → 定位 PDF / 在线源 |
| Zotero 分类批量 | 递归子分类 → 去重 → 逐篇处理 |

每个需要保存的输入先解析为稳定 `paper_id`。arXiv 去掉 `vN`，DOI 规范化为小写，
本地 PDF 使用内容 SHA-256，Zotero 优先复用 arXiv/DOI/PDF 身份，最后才使用条目
key。笔记目标存在但身份不同时禁止覆盖。

找不到 PDF 时的 fallback 顺序：
1. arXiv URL 直接获取 HTML 版本（优先，能拿图），不访问 Zotero
2. 明确的 Zotero 输入才用 `zotero_helper.py info` 定位元数据和本地 PDF
3. Fallback：arXiv PDF / DOI 页面
4. 最后：通过 arXiv API 或可用搜索能力检索论文标题
5. 都不行 → 跳过

### 阅读模式

| 模式 | 触发词 | 输出 |
|------|--------|------|
| 快速摘要 | "快速看一下" | 3-5 句核心贡献 |
| 完整解析 | 默认 | 结构化笔记（模板） |
| 批判性分析 | "批判性分析" | 优缺点评估 |
| 知识提取 | "提取公式" | 公式 + 算法伪代码 |

### 图片获取（多路 fallback）

1. arXiv HTML：提取 `<figure>` 标签的图片 URL（优先）
2. 项目主页：从摘要 / HTML 找项目链接，抓 teaser 图
3. PDF 提取：`pdfimages -png`，过滤 > 10KB 的
4. 写完后跑 `download_note_images.py` 做可达性检查，不可达的自动下载到本地

### 笔记生成

严格按 `paper-note-template.md` 模板：
- 所有 Figure、所有公式、所有 Table 都必须出现
- 技术术语首次出现必须用 `[[概念]]` 链接
- 每个公式需要：名称、LaTeX、含义、符号说明
- 文件名只用方法 / 模型名（如 `Pi05.md`），不加年份前缀

### 存储

- Zotero 输入：`{NOTES_PATH}/{zotero_collection_path}/{MethodName}.md`
- 非 Zotero 输入：根据主题分类，无法判断时写入 `{INBOX_PATH}/`
- 默认未分类目录为 `_待整理/`
- YAML frontmatter：title / method_name / authors / year / venue / tags / 可选 zotero_collection / image_source / created

### 概念库维护

每篇论文读完后：
1. 扫描笔记中所有 `[[概念]]` 链接
2. 检查概念笔记是否存在
3. 不存在的按 16 类自动分类并创建

### 批量阅读

批量 Zotero 输入仍由当前 `paper-reader` workflow 处理：先用只读数据库快照列出
条目，再按“一篇论文 = 一个独立执行上下文”的规则委派或 inline 执行。公共 Skill
不再附带会递归启动 Claude Code / Codex CLI 的后台守护进程，避免嵌套 Harness
拥有与父任务不同的权限和生命周期。

Zotero 集成是只读的。Skill 可以查询条目、分类和本地 PDF，并给出分类建议，但
不得直接修改 Zotero SQLite；需要调整分类时由用户在 Zotero UI 中完成。

---

## generate-mocs workflow

**纯 Python，递归扫目录生成索引页。**

核心函数 `build_tree_mocs()`：
- 递归遍历目录
- 每个目录生成一个 `目录名.md` 索引文件
- 包含：子目录链接（带笔记数统计）+ 当前目录笔记列表
- 幂等：内容没变的文件不重写
- 用 wikilink 格式

`refresh_mocs.py` 提供一个入口：

- `--scope all`：依次刷新概念与论文目录，并返回合并后的精确 changed paths
- `--scope concepts`：只扫描配置中的概念库目录（默认 `_概念/`）
- `--scope papers`：只扫描论文笔记并排除概念目录

---

## scripts/shared 公共模块

### defaults.json

包内只读 bootstrap 默认值。它不是用户配置，也不能作为安装后的隐式 overlay；
用户拥有的完整共享设置位于 Vault 的 `.dailypaper/config.json`：

```json
{
  "paths": {
    "obsidian_vault": ".",
    "paper_notes_folder": "论文笔记",
    "daily_papers_folder": "DailyPapers",
    "concepts_folder": "_概念",
    "inbox_folder": "_待整理",
    "zotero_db": "~/Zotero/zotero.sqlite",
    "zotero_storage": "~/Zotero/storage"
  },
  "daily_papers": {
    "keywords": ["world model", "diffusion model", "embodied ai", ...],
    "negative_keywords": ["medical imaging", "weather forecast", ...],
    "domain_boost_keywords": ["robot", "manipulation", ...],
    "arxiv_categories": ["cs.RO", "cs.CV", "cs.AI", "cs.LG"],
    "min_score": 2,
    "top_n": 30
  },
  "runtime": {
    "timezone": "Asia/Shanghai"
  },
  "repository": {
    "url": "git@github.com:haoz0206/dailypaper-vault.git",
    "remote": "origin",
    "branch": "main",
    "task_state_file": ".dailypaper/tasks/daily-papers.json",
    "pull_before_run": true,
    "require_clean": true,
    "coordination_enabled": true,
    "lease_hours": 24,
    "same_day_policy": "skip"
  },
  "automation": {
    "auto_refresh_indexes": true,
    "git_commit": false,
    "git_push": false
  }
}
```

### user_config.py

Python 配置加载器，带缓存。提供 `load_user_config()` / `paths_config()` / `daily_papers_config()` / `automation_config()` 等便捷函数。会校验 `git_push` 不能在 `git_commit` 关闭时开启。

### run_coordinator.py

统一的日报生命周期门面。对 Harness 暴露 `start`、`submit`、`inspect` 和
`cancel`，把阶段推进、恢复验证、跨机取消和发布决策集中在同一实现。

### stage_report.py

Stage Report v1 的严格解析模块。它拒绝未知字段、重复 JSON key、非标准 JSON、
错误 Phase、绝对/逃逸路径和不匹配的 artifact scope，并把验证后的报告转换为
Coordinator submission。报告自身也是 Run Artifact，因此 resume 可以验证阶段
交付证据没有变化。

### validate_paper_note.py

论文笔记的只读结构验证模块。paper-reader 与 notes 父阶段通过同一个接口检查
最低行数、公式、图片和必需 section；失败只返回结构化原因，不删除已有成果。

### run_lifecycle.py

Manifest v2 的唯一受支持写入模块。它执行 schema/Workflow Contract 校验、严格
Phase 推进、condition/outcome 变更、artifact hash 与 Run Change Set 登记，并用
per-mutation lock、revision compare-and-set、原子 snapshot 和上一 revision 备份
保护本地状态。

### run_guardian.py

运行期间持有本地 execution/liveness `flock`，阻止同机两个父协调器同时驱动同一
Run，并提供存活诊断。生产 guardian 不因 idle timeout 自动退出；存活 guardian
只能在用户确认 exact run/session ID 后由对应协调器停止并恢复同一工作。guardian
不写 Manifest，也不拥有 Vault Task State。

### vault_coordination.py

每日任务的确定性 Git 协调器。运行前验证固定远程和分支、执行 fast-forward pull，
并通过 `.dailypaper/tasks/daily-papers.json` 的 acquisition commit/push 原子抢占
任务。运行后验证远程 HEAD、`run_id` 和配置指纹，只发布 Manifest 登记的稳定
输出；发布记录并复用固定内容 commit。跨机取消必须 fresh fetch，并以准确的远程
HEAD 和 `run_id` 做 compare-and-set。协调失败时禁止自动 rebase、force push 或
锁抢占。

### moc_builder.py

MOC 生成引擎，由 `refresh_mocs.py` 通过单一接口调用。规划阶段对每个目录只做
一次有界快照，随后同时复用其中的子目录和 Markdown 笔记视图来生成父/子统计；
目录数、单目录 entry 数、总笔记数和最终单页字节数均在任何写入前校验。这样避免
旧实现为每个 MOC 重复扫描当前目录和每个子目录，也避免超大 Vault 在排序前放大
内存。

---

## Obsidian 目录结构

```
<Obsidian Vault>/
├── .dailypaper/
│   ├── tasks/
│   │   └── daily-papers.json        # 跨机器/跨 harness 任务状态
│   └── runs/                        # 本地忽略的中间 manifest
├── DailyPapers/
│   ├── YYYY-MM-DD-论文推荐.md      # 每日推荐
│   └── .history.json                # 跨天去重索引
├── 论文笔记/
│   ├── 3-Robotics/
│   │   ├── 1-VLX/VLA/
│   │   │   ├── VLA.md               # 目录页（自动生成）
│   │   │   ├── OpenVLA.md
│   │   │   └── Pi05.md
│   │   └── ...
│   ├── _概念/
│   │   ├── 1-生成模型/
│   │   │   ├── DiT.md
│   │   │   └── Flow Matching.md
│   │   ├── 3-机器人策略/
│   │   │   └── Diffusion Policy.md
│   │   ├── ... (共 16 个分类)
│   │   └── 0-待分类/
│   └── _待整理/                    # 无法自动归类的论文
└── ...
```
