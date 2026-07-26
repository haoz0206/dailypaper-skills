---
name: paper-reader
description: |
  Use when user asks to "read paper", "analyze paper", "summarize paper",
  "读论文", "分析文献", "帮我看一下这篇paper", "论文笔记", or provides a PDF file
  that appears to be an academic paper. Specialized for CV/DL papers.

  Also supports Zotero integration: "读一下这篇论文 ...", "快速看一下这篇论文 ...",
  "批判性分析这篇论文 ...", "读一下 Zotero 里的 XXX", "批量读一下 Zotero 里 VLA 分类下的论文"

  **重要触发词**: "读一下 XXX"、"读一下这篇"、"帮我读" → 必须调用此 skill
context: fork
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch
---

> **开始前**: 先跟用户打个招呼 🐕

# 学术论文阅读助手 (Paper Reader)

专注 CV/DL 领域，支持 Zotero 集成和 Obsidian 笔记保存。

## Step 0: 读取共享配置

将本 `SKILL.md` 所在目录的父目录解析为绝对路径 `SKILLS_ROOT`。读取
`{SKILLS_ROOT}/_shared/user-config.json`；如果同目录的
`user-config.local.json` 存在，再用它覆盖默认值。也允许
`DAILYPAPER_CONFIG` 指向外部配置。

显式生成并在后续统一使用这些变量：

- `VAULT_PATH`
- `NOTES_PATH`
- `CONCEPTS_PATH`
- `INBOX_PATH`
- `ZOTERO_DB`
- `ZOTERO_STORAGE`
- `AUTO_REFRESH_INDEXES`
- `GIT_COMMIT_ENABLED`
- `GIT_PUSH_ENABLED`
- `DAILYPAPER_PARENT_RUN`（由每日流水线调用时为 true）

其中：

- `NOTES_PATH = {VAULT_PATH}/{paper_notes_folder}`
- `CONCEPTS_PATH = {NOTES_PATH}/{concepts_folder}`
- `INBOX_PATH = {NOTES_PATH}/{inbox_folder}`
- `GIT_PUSH_ENABLED` 只有在 `GIT_COMMIT_ENABLED=true` 时才可能为真

后续统一使用上面的变量。

## 1. 接收论文

| 输入方式 | 示例 | 处理方法 |
|----------|------|----------|
| PDF 路径 | `/path/to/paper.pdf` | 直接读取本地文件 |
| arXiv 链接 | `https://arxiv.org/abs/xxxx` | 优先获取 arXiv HTML，必要时下载 PDF |
| Zotero 分类 | "VLA 分类的论文" | 查询数据库 → 列出 → 用户选择 |
| Zotero 搜索 | "Zotero 里的 π0.5" | 搜索标题 → 找到 PDF |
| 无 PDF | Zotero 条目无附件 | 从网上获取（见下方） |

### 无 PDF 时的获取流程

1. 只有输入明确来自 Zotero 时，才运行
   `python3 "{SKILLS_ROOT}/paper-reader/assets/zotero_helper.py" info {item_id}`。
2. 按优先级获取：arXiv HTML > arXiv PDF > DOI > 标题检索。
3. 从用户 URL、Zotero extra 字段或 arXiv API 标题检索确定 arXiv ID。
4. 优先用可用网络工具或 `curl` 获取 `https://arxiv.org/html/{arxiv_id}`。
5. 跳过条件：既无 PDF 也无在线来源 / 非论文内容

对 arXiv URL 或本地 PDF 输入，**不得检查或访问 Zotero SQLite**。Zotero 是显式的
可选集成，不是 paper-reader 的前置条件。

> Zotero 详细操作见 `{SKILLS_ROOT}/paper-reader/references/zotero-guide.md`

## 2. 阅读模式

| 模式 | 触发词 | 输出 |
|------|--------|------|
| **快速摘要** | "快速看一下"、"quick" | 3-5 句核心贡献 |
| **完整解析** | "详细分析"、默认 | 结构化笔记（用模板） |
| **批判分析** | "批判性分析"、"critique" | 方法论优缺点评估 |
| **知识提取** | "提取公式"、"技术细节" | 公式 + 算法伪代码 |

## 3. 笔记生成

**模板**: 严格遵循
`{SKILLS_ROOT}/paper-reader/assets/paper-note-template.md`，不可自行简化。

### 核心质量规则

1. **零遗漏**: 论文中所有 Figure、所有公式、所有 Table 必须全部出现在笔记中
2. **内联概念链接**: 正文中首次出现的技术术语必须用 `[[概念]]` 链接，不仅仅是结尾
3. **严禁 ASCII 流程图**: 用结构化 Markdown 列表 + `$数学符号$` 描述架构
4. **公式完整性**: 每个公式必须有名称（`[[概念|名称]]`）、LaTeX 公式、含义、符号说明
5. **图片外链优先**: arXiv HTML / 项目主页 / GitHub，找不到再本地下载

> 公式/图片/表格的详细质量规范见
> `{SKILLS_ROOT}/paper-reader/references/quality-standards.md`

### 图片获取流程（多源 fallback）

**目标**: 确保笔记中包含论文的**所有 Figure**，先统计论文 Figure 总数再逐一获取。

1. 使用 arXiv API 或可用搜索能力按标题获取 arXiv ID
2. **来源 A — arXiv HTML**（首选）：
   - 获取 `https://arxiv.org/html/{arxiv_id}`，提取所有 `<figure>` 的标题与 img src URL
   - 统计论文 Figure 总数，确认提取数量是否完整
3. **来源 B — 项目主页**（HTML 404 或图片不全时）：
   - 从摘要/HTML 中查找项目主页 URL（常见模式：`project page`、`github.io`、`our website`）
   - 获取项目主页并提取展示图片（通常包含 teaser / demo 图）
4. **来源 C — PDF 提取**（前两者都失败时）：
   - `pdfimages -png` 从 PDF 中提取，筛选 >10KB 的有效图片
5. 笔记中用 `![Figure X](url)` 外链嵌入
6. 验证：外链可加载 / 本地文件 >10KB
7. **URL 去重**：写入前检查 URL 中是否有重复的 arxiv_id 路径段（如 `2603.05312v1/2603.05312v1/`），有则删除重复段。详见
   `{SKILLS_ROOT}/paper-reader/references/image-troubleshooting.md`

> ar5iv 编号不一定对应 Figure 编号，排错见
> `{SKILLS_ROOT}/paper-reader/references/image-troubleshooting.md`

### 图片可靠性保障（生成后自动执行）

笔记保存后，运行图片可达性检查脚本，自动将不可访问的外链图片下载到本地：
```bash
python3 "{SKILLS_ROOT}/daily-papers/download_note_images.py" "{笔记完整路径}"
```
- 可达的外链保持不动，不可达的自动下载到 `assets/` 并替换为 Obsidian wikilink
- 如有本地化操作，frontmatter `image_source` 自动更新为 `mixed`

### 公式格式

每个公式必须包含：名称（`[[概念|名称]]`）、LaTeX `$$` 块（前后留空行）、含义、符号列表。
`$$` 块前后**必须有空行**否则 Obsidian 不渲染。超长公式用 `aligned` 拆分。

## 4. Obsidian 保存

### 文件命名

只用**方法名/模型名**：`{方法名}.md`（如 `Pi05.md`，不加年份前缀）。
方法名判断：标题冒号前 / Abstract 中 "We propose XXX" / 希腊字母转 ASCII。
不确定时保存到 `{INBOX_PATH}`。

### 保存路径

- Zotero 输入：优先按 `{NOTES_PATH}/{zotero_collection_path}/{方法名}.md` 保存。
- arXiv URL 或本地 PDF：按论文主题选择现有分类；无法可靠分类时保存到
  `{INBOX_PATH}/{方法名}.md`。
- 所有目标路径必须位于 `NOTES_PATH` 内。

### YAML frontmatter

```yaml
---
title: "论文标题"
method_name: "MethodName"
authors: [Author1, Author2]
year: 2025
venue: arXiv
tags: [tag1, tag2]  # 小写连字符，3-8 个
zotero_collection: ""  # 非 Zotero 输入留空
image_source: online
created: YYYY-MM-DD
---
```

Tags 判断：看 Related Work 小标题 + Abstract 关键词。第一个 tag 是最核心主题。

### 保存后自动执行

1. 只有在 `AUTO_REFRESH_INDEXES=true` 时才刷新目录页：
   ```bash
   python3 "{SKILLS_ROOT}/_shared/generate_concept_mocs.py"
   python3 "{SKILLS_ROOT}/_shared/generate_paper_mocs.py"
   ```
2. 当 `DAILYPAPER_PARENT_RUN=true` 时，将变更路径返回给父流程，**不得执行任何
   git add、commit 或 push**。
3. 只有独立调用且 `GIT_COMMIT_ENABLED=true` 时才做 git：
   - 先确认 `VAULT_PATH/.git` 存在
   - 开始前要求工作树干净
   - 只暂存本次创建或修改的明确路径，不得使用 `git add -A`
   - 满足条件后再执行：
   ```bash
   git -C "{VAULT_PATH}" add -- {本次变更路径...}
   git -C "{VAULT_PATH}" commit -m "add paper note: {方法名}"
   ```
   - 只有在 `GIT_PUSH_ENABLED=true` 且仓库已配置远端时才 push

## 5. 概念库维护（每篇论文必做）

概念库位置：`{CONCEPTS_PATH}`

### 流程

1. **扫描**论文笔记中所有 `[[概念]]` 链接
2. **检查**每个链接对应的概念笔记是否存在（`ls` + `find`）
3. **创建**不存在的概念（不可跳过），自动归类到对应子目录

> 分类规则和模板见 `{SKILLS_ROOT}/paper-reader/references/concept-categories.md`

### 自检

- [ ] 笔记中所有 `[[概念]]` 链接的概念笔记都存在？
- [ ] 概念笔记包含本论文作为"代表工作"？

## 6. 完成后自检（合并 checklist）

- [ ] 所有 Figure 都在笔记中（数量与论文一致）？
- [ ] 所有公式都在笔记中（变量一致、无冲突）？
- [ ] 所有 Table 完整保留（所有行列）？
- [ ] 正文中技术术语有 `[[概念]]` 内联链接？
- [ ] 概念库已更新（缺失的概念已创建）？
- [ ] 图片可用（外链可加载 / 本地 >10KB）？

## 7. 交互式功能

完成解析后询问：深入解释？对比其他论文？保存到 Obsidian？
保存后自动创建缺失概念笔记，报告新增概念数量。

## 8. 批量处理

支持 Zotero 分类批量处理（默认递归子分类）。流程：递归获取论文 → 去重 → 跳过已有笔记 → 依次处理 → 汇总。

## 参考文件（按需查阅）

- **`{SKILLS_ROOT}/paper-reader/references/zotero-guide.md`** — Zotero 查询、分类、PDF 路径获取、智能分类判断
- **`{SKILLS_ROOT}/paper-reader/references/image-troubleshooting.md`** — ar5iv 图片编号对应、PDF 提取备选
- **`{SKILLS_ROOT}/paper-reader/references/concept-categories.md`** — 概念自动归类的 16 个子目录规则 + 模板
- **`{SKILLS_ROOT}/paper-reader/references/quality-standards.md`** — 公式/图片/表格的详细质量规范 + 自检清单
