# Harness-independent workflow contract

This document defines the user-visible interface shared by the Claude Code and
Codex adapters. The unified branch uses portable `SKILL.md` metadata and one
shared workflow for both harnesses. Runtime adapters may change explicit
invocation syntax and CLI flags, but must not change the inputs and outputs
below.

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

Configuration workflow:

```text
查看当前每日论文配置
配置每日论文
把研究方向改成 VLA 和 robot learning
只抓取 cs.RO 和 cs.CV，每天推荐 15 篇
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

On the persistent Linux server, the recommended per-machine Vault location is:

```text
/workspace/dailypaper-vault
```

Installation is not complete until the user runs the public
`configure-dailypaper` Skill. It validates or initializes the Vault and stores
the absolute clone path in `~/.config/dailypaper/config.json` by default.
`DAILYPAPER_MACHINE_CONFIG` may relocate that machine file, while
`DAILYPAPER_VAULT` remains a temporary explicit override. Scheduled runs must
not fall back to their current working directory when this configuration is
missing or invalid; they stop and direct the user to `configure-dailypaper`.

The tracked Vault configuration keeps `paths.obsidian_vault` as `.`; absolute
clone locations are deliberately excluded from the stable configuration
fingerprint. Other machines may configure another absolute clone path.

The coordinated Vault repository is fixed to:

```text
git@github.com:haoz0206/dailypaper-vault.git
```

Both adapters use remote `origin`, branch `main`, and IANA timezone
`Asia/Shanghai` by default. They must reject a different remote or branch rather
than silently publishing elsewhere.

Before acquisition, the Skill sets the task state's harness identity from the
current host: `claude-code` for Claude Code and `codex` for Codex. Harness
identity is runtime metadata; it does not select a skills Git branch.

`automation.git_commit` and `automation.git_push` control standalone helper
Skills only. A full daily run always uses the acquisition and publication
commits required by the coordination protocol.

### Shared configuration mutation

The public `configure-dailypaper` Skill is both the installation onboarding
entry and the canonical natural-language adapter for shared configuration.
Both Harnesses must map the same request to the same supported fields and
deterministic validator. It may update:

- `daily_papers.keywords`
- `daily_papers.negative_keywords`
- `daily_papers.domain_boost_keywords`
- `daily_papers.arxiv_categories`
- `daily_papers.min_score`
- `daily_papers.top_n`
- `automation.auto_refresh_indexes`

It must reject unknown or currently inert fields rather than writing settings
that the workflow does not consume. Shared keyword lists are lowercase,
trimmed, de-duplicated, and conflict-checked. `paths.obsidian_vault` remains
`.`; absolute clone paths and credentials never enter the shared file.

Before a configuration write, the adapter fast-forward pulls `origin/main`,
requires a clean Vault, and reads `.dailypaper/tasks/daily-papers.json`. A
`running` daily task blocks configuration changes. The deterministic apply
helper repeats this guard immediately before atomically replacing the config,
so a run acquired after preview also blocks the write. A successful update
writes, stages, commits, and pushes only `.dailypaper/config.json`; push
rejection must not trigger automatic rebase or force push. Configuration
helpers never edit the task document or per-run manifests.

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
`skills/daily-papers/assets/paper-note-template.md`, including `title`,
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

## Allowed runtime adapter differences

- Skill discovery and installation directory.
- Explicit invocation syntax.
- Harness permission and sandbox flags.
- Host-specific file, command, and network tool wording.
- Internal orchestration used to reach the shared workflow.

## Installable package boundary

The repository exposes exactly four installer-visible Skills:

- `skills/daily-papers/SKILL.md`: complete daily recommendation workflow.
- `skills/paper-reader/SKILL.md`: manual single-paper and Zotero reading.
- `skills/generate-mocs/SKILL.md`: manual Obsidian index refresh.
- `skills/configure-dailypaper/SKILL.md`: first-run machine onboarding and
  shared research configuration.

Fetch, review, and notes remain private stages of `daily-papers`; they are not
independently discoverable or installable. Each public Skill is independently
self-contained and must run without the source repository or sibling Skill
directories. The three focused public packages are generated from canonical
workflows and their minimal runtime resources by
`tools/sync_public_skills.py`; CI/tests must run its `--check` mode to prevent
drift.

Harness identity is selected at runtime; installers do not select a
Claude-specific or Codex-specific Git branch. Changes to default research
keywords, output directories, note templates, scoring rules, or generated
Markdown require a canonical shared workflow change in the single unified
branch followed by public-package synchronization.

## Adapter validation

All four public Skills keep only portable `name` and `description` frontmatter
and have no vendor-specific sidecar metadata. Their descriptions have
non-overlapping primary trigger boundaries. When supported, `paper-reader`
requests one Subagent through portable workflow instructions; otherwise it runs
inline. Subagents never own the Vault lock, manifest, or Git publication. Both
Harnesses call the same Python run context and Vault coordination scripts; they
do not reimplement Git safety in natural-language instructions.
