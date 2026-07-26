# Project guide

## Purpose

`dailypaper-skills` is a harness-independent workflow for discovering, reviewing,
and reading academic papers into an Obsidian Vault. Claude Code and Codex use
different adapters, but should expose the same daily prompts and stable Vault
outputs.

## Stable user interface

Canonical prompts:

- `今日论文推荐`
- `过去3天论文推荐`
- `过去一周论文推荐`
- `读一下这篇论文 <URL-or-PDF>`
- `更新索引`

Stable outputs:

- `DailyPapers/YYYY-MM-DD-论文推荐.md`
- `DailyPapers/.history.json`
- `论文笔记/<topic>/<MethodName>.md`
- `论文笔记/_概念/`
- `论文笔记/_待整理/`

Harness-specific forms such as `/daily-papers` and `$daily-papers` are adapters,
not the canonical user interface.

## Architecture

- `skills/daily-papers/`: one-shot daily orchestrator.
- `skills/daily-papers-fetch/`: fetch, score, and enrich candidates.
- `skills/daily-papers-review/`: write the recommendation and history.
- `skills/daily-papers-notes/`: generate required notes, backfill links, and
  perform the final publication step.
- `skills/paper-reader/`: read one paper or an explicit Zotero input.
- `skills/generate-mocs/`: rebuild Obsidian navigation pages.
- `skills/_shared/`: shared configuration and deterministic Python helpers.

Keep research keywords, scoring, paths, templates, and generated Markdown in
the shared workflow. Keep discovery metadata, explicit invocation syntax,
permission flags, and host tool wording in the harness adapter.

## Configuration and safety

- Tracked defaults live in `skills/_shared/user-config.json`.
- Personal values belong in `user-config.local.json` or an external
  configuration supported by the current adapter.
- Zotero is optional. Do not access its SQLite database for ordinary arXiv or
  local-PDF inputs.
- The persistent Linux server stores its Vault clone at
  `/workspace/dailypaper-vault`. Treat this as per-machine environment
  configuration (`DAILYPAPER_VAULT`), not a tracked absolute path.
- Resolve scripts from the Skill location, not the caller's current directory.
- Do not use shared fixed temporary filenames.
- Bootstrap may create one initialization commit for an empty Vault. A full
  coordinated daily run then creates one acquisition commit and at most one
  content commit, both with ordinary pushes. Standalone helper pushes remain
  opt-in.
- Git automation must stage only bootstrap files or paths written by the current
  run. Never use force push or automatic rebase for Vault coordination.

## Branch maintenance

- `main` is the Claude Code adapter.
- Codex branches add Codex discovery metadata and runtime behavior.
- When a stable input, output, template, scoring rule, or default research
  setting changes, apply the change to both adapters.
- Each branch README must state both shared behavior and known adapter
  differences. Do not describe a target contract as already implemented when a
  branch still has a documented gap.

## Validation

Run the checks available on the current branch:

```bash
python3 -m compileall -q skills
python3 -m unittest discover -s tests -v
```

The unit-test command applies when the branch contains `tests/`. Also validate
every changed `SKILL.md` with the harness's Skill validator.
