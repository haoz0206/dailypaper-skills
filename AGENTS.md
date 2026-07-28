# Project guide

## Purpose

`dailypaper-skills` is a harness-independent workflow for discovering, reviewing,
and reading academic papers into an Obsidian Vault. The unified branch uses
portable Agent Skills metadata and exposes the same daily prompts and stable
Vault outputs from one checkout.

## Stable user interface

Canonical prompts:

- `今日论文推荐`
- `过去3天论文推荐`
- `过去一周论文推荐`
- `读一下这篇论文 <arXiv-or-local-PDF>`
- `更新索引`
- `查看当前每日论文配置`
- `配置每日论文`

Stable outputs:

- `DailyPapers/YYYY-MM-DD-论文推荐.md`
- `DailyPapers/.history.json`
- `论文笔记/<topic>/<MethodName>.md`
- `论文笔记/_概念/`
- `论文笔记/_待整理/`

Harness-specific forms such as `/daily-papers` and `$daily-papers` are adapters,
not the canonical user interface.

## Architecture

- `skills/daily-papers/SKILL.md`: the only installer-visible entry and router.
- `skills/daily-papers/workflows/`: daily, single-paper, MOC, configuration,
  and private fetch/review/notes workflows.
- `skills/daily-papers/scripts/`: deterministic implementation grouped by
  workflow, with shared configuration and coordination under `scripts/shared/`.
- `skills/daily-papers/assets/` and `references/`: bundled note template and
  reading guidance.

The suite must remain self-contained: installing `skills/daily-papers/` alone
must include every runtime dependency. Internal workflows are resources, not
independently discoverable Skills.

Keep research keywords, scoring, paths, templates, generated Markdown, and
Subagent delegation rules in the shared workflow. Keep explicit invocation
syntax, CLI flags, and host tool wording in runtime adapters.

## Configuration and safety

- Tracked defaults live in
  `skills/daily-papers/scripts/shared/user-config.json`.
- Personal values belong in `user-config.local.json` or an external
  configuration supported by the current adapter.
- Zotero is optional. Do not access its SQLite database for ordinary arXiv or
  local-PDF inputs.
- The persistent Linux server stores its Vault clone at
  `/workspace/dailypaper-vault`. Treat this as per-machine environment
  configuration (`DAILYPAPER_VAULT`), not a tracked absolute path.
- Resolve scripts from the Skill location, not the caller's current directory.
- Do not use shared fixed temporary filenames.
- Shared configuration updates must refuse an active `running` daily task,
  validate supported fields deterministically, and stage only
  `.dailypaper/config.json`.
- Configuration Skills must reject inert fields rather than recording values
  that no runtime code consumes. Per-machine paths and credentials never enter
  the shared Vault config.
- Bootstrap may create one initialization commit for an empty Vault. A full
  coordinated daily run then creates one acquisition commit and at most one
  content commit, both with ordinary pushes. Standalone helper pushes remain
  opt-in.
- Git automation must stage only bootstrap files or paths written by the current
  run. Never use force push or automatic rebase for Vault coordination.

## Branch maintenance

- The unified branch contains exactly one portable `SKILL.md`, with only
  `name` and `description` frontmatter and no vendor-specific sidecar metadata.
- Harness identity is selected at runtime (`claude-code` or `codex`), never by
  switching the skills Git branch.
- Prompt-level Subagent delegation must degrade to inline execution. Subagents
  never own Vault coordination, manifests, or Git publication.
- Keep stable inputs, outputs, templates, scoring rules, and default research
  settings in one shared implementation.
- README must state the remaining runtime adapter differences without implying
  that users need separate branches.

## Validation

Run the checks available on the current branch:

```bash
python3 -m compileall -q skills
python3 -m unittest discover -s tests -v
```

The unit-test command applies when the branch contains `tests/`. Also validate
every changed `SKILL.md` with the harness's Skill validator.
