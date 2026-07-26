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
- `automation.*`

Research interests belong in shared or local configuration, never in a harness
adapter. Local overrides must not need to be committed.

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

## Publication result

Git automation has the same observable result on both harnesses:

1. Generate and validate all daily outputs.
2. Stage only paths written by the current run.
3. Create at most one daily commit.
4. Push only when explicitly enabled.

## Allowed adapter differences

- Skill discovery directory and metadata.
- Explicit invocation syntax.
- Harness permission and sandbox flags.
- Host-specific file, command, and network tool wording.
- Internal orchestration used to reach the shared workflow.

Changes to default research keywords, output directories, note templates,
scoring rules, or generated Markdown require a shared workflow change and
should be applied to both branches.

## Current implementation status

The Codex adapter implements isolated run manifests and a single exact-path
publication step. The Claude Code `main` branch still uses fixed temporary JSON
paths and may commit in both review and notes stages. These are documented
compatibility gaps, not intended differences in the stable interface.
