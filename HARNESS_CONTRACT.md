# Harness-independent workflow contract

This document defines the user-visible interface shared by the Claude Code and
Codex adapters. Harness branches may change discovery metadata, installation
paths, permission syntax, and internal Skill-to-Skill invocation. They must not
change the inputs and outputs below.

## Canonical inputs

Daily workflow:

```text
今日论文推荐
过去3天论文推荐
过去一周论文推荐
```

Single-paper workflow:

```text
读一下这篇论文 https://arxiv.org/abs/2509.24527
快速看一下这篇论文 /path/to/paper.pdf
批判性分析这篇论文 /path/to/paper.pdf
```

Index workflow:

```text
更新索引
```

Harness-specific forms such as `/daily-papers` or `$daily-papers` are optional
explicit adapters. They are not part of the canonical user interface.

## Configuration inputs

Both adapters read the same logical configuration:

- `paths.obsidian_vault`
- `paths.paper_notes_folder`
- `paths.daily_papers_folder`
- `paths.concepts_folder`
- `paths.inbox_folder`
- `paths.zotero_db` and `paths.zotero_storage` for explicit Zotero workflows
- `daily_papers.*`
- `runtime.timezone`
- `repository.*`
- `automation.*`

Research interests belong in shared or local configuration, never in a harness
adapter. Machine-local paths may differ, but all output-affecting configuration
must produce the same configuration fingerprint for a coordinated run.

On the persistent Linux server, the per-machine Vault location is:

```text
/workspace/dailypaper-vault
```

Set that value through `DAILYPAPER_VAULT` in the scheduler or service
environment. The tracked Vault configuration keeps `paths.obsidian_vault` as
`.`; absolute clone locations are deliberately excluded from the stable
configuration fingerprint. Other machines may use another absolute clone path.

The coordinated Vault repository is fixed to:

```text
git@github.com:haoz0206/dailypaper-vault.git
```

Both adapters use remote `origin`, branch `main`, and IANA timezone
`Asia/Shanghai` by default. They must reject a different remote or branch rather
than silently publishing elsewhere.

`automation.git_commit` and `automation.git_push` control standalone helper
Skills only. A full daily run always uses the acquisition and publication
commits required by the coordination protocol.

## Stable outputs

With the default configuration, both adapters write:

```text
<Obsidian Vault>/
├── DailyPapers/
│   ├── YYYY-MM-DD-论文推荐.md
│   └── .history.json
└── 论文笔记/
    ├── _概念/
    │   └── 0-待分类/
    ├── _待整理/
    └── <topic>/<MethodName>.md
```

The daily recommendation and paper-note Markdown structures, frontmatter,
wikilinks, MOC pages, and image assets are stable outputs. Per-run manifests
and enriched JSON are internal implementation details and are not a stable
interface.

### Daily recommendation schema

`DailyPapers/YYYY-MM-DD-论文推荐.md` starts with:

```yaml
---
date: YYYY-MM-DD
keywords: <configured keywords in configured order>
tags: [daily-papers, auto-generated]
---
```

The body contains, in order:

1. `# 🔪 今日锐评`
2. `## 分流表`
3. numbered paper reviews grouped by topic
4. excluded papers when applicable
5. one closing trend judgment

The split table uses method/model-name wikilinks and the three stable levels
`🔥 必读`, `👀 值得看`, and `💤 可跳过`.

### Paper-note schema

Paper notes keep the frontmatter keys defined in
`skills/paper-reader/assets/paper-note-template.md`, including `title`,
`method_name`, `authors`, `year`, `venue`, `tags`, `zotero_collection`,
`image_source`, `arxiv_html`, and `created`. Non-Zotero inputs write an empty
`zotero_collection`.

The stable note sections are `元信息`, `一句话总结`, `核心贡献`, `问题背景`,
`方法详解`, `关键公式`, `关键图表`, `实验结果`, `批判性思考`, `关联笔记`,
and `速查卡片`. Harnesses may produce different prose, rankings, and extracted
details, but may not change this schema or the Vault paths.

## Vault coordination protocol

The Git remote branch is the atomic coordination component. The task document
is:

```text
.dailypaper/tasks/daily-papers.json
```

It records schema version, task, target date, status, globally unique run ID,
harness, owner, timestamps, lease, starting commit, stable configuration
fingerprint, and expected daily output.

Every full daily run must:

1. Idempotently bootstrap the Vault before creating local run state. Bootstrap
   verifies the Git root, fixed remote, and `main`; initializes an empty remote
   with a portable `.dailypaper/config.json` and `.gitignore`; otherwise it
   fast-forward pulls and only adds missing bootstrap files.
2. Create an isolated local run manifest under ignored
   `.dailypaper/runs/<run-id>/`.
3. Verify that the configured Vault is the Git root, `origin` matches the fixed
   repository, the current branch is `main`, and the worktree is clean.
4. Run `git pull --ff-only origin main`, then discard any cached configuration
   and reload the post-pull Vault configuration.
5. Stop successfully if the target day's recommendation already exists.
6. Stop without writing if another `running` task owns the document.
7. Write its own `running` state in an isolated candidate clone and push that
   acquisition commit.
8. Continue only when that ordinary push succeeds. A non-fast-forward rejection
   means another runner won.
9. Generate and validate outputs without independent Git commits.
10. Before publication, fetch and verify that the remote HEAD is still the
    acquisition commit, the task document still contains the same run ID, and
    the stable configuration fingerprint is unchanged.
11. Publish the task's `success` state and exactly the manifest's changed paths
    in one content commit with an ordinary push.

Acquisition or publication failure must never trigger force push, automatic
rebase, lock stealing, or content regeneration. A crashed run leaves a visible
`running` state for explicit operator recovery.

Per-run intermediate files remain under ignored `.dailypaper/runs/`. The task
document is tracked and is part of the coordination interface.

Bootstrap is the only first-run exception to the normal two-commit daily
protocol. It creates at most one reviewed initialization commit and preserves a
local commit if its ordinary push fails.

## Publication result

Git automation has the same observable result on both harnesses:

1. Create one acquisition commit before expensive work.
2. Generate and validate all daily outputs.
3. Stage only paths written by the current run plus the task document.
4. Create at most one content commit for the run.
5. Push both commits without force.

## Allowed adapter differences

- Skill discovery directory and metadata.
- Explicit invocation syntax.
- Harness permission and sandbox flags.
- Host-specific file, command, and network tool wording.
- Internal orchestration used to reach the shared workflow.

Changes to default research keywords, output directories, note templates,
scoring rules, or generated Markdown require a shared workflow change and
should be applied to both branches.

## Adapter validation

Codex adapters keep valid `agents/openai.yaml` metadata. Claude Code adapters
keep valid Claude discovery paths, invocation syntax, and any Claude-specific
frontmatter or tool permissions. Those adapters call the same Python run
context and Vault coordination scripts; they do not reimplement Git safety in
natural-language instructions.
