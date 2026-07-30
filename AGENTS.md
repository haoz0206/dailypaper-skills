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

- `skills/daily-papers/`: public coordinated daily workflow and canonical
  implementation source.
- `skills/paper-reader/`: public standalone paper-reading Skill.
- `skills/generate-mocs/`: public standalone Obsidian MOC maintenance Skill.
- `skills/configure-dailypaper/`: public first-run onboarding and configuration
  Skill.
- `skills/daily-papers/workflows/fetch.md`, `review.md`, and `notes.md`: private
  stages that require the parent run manifest and lock.
- `skills/daily-papers/scripts/shared/run_coordinator.py`: the only Harness-facing
  daily lifecycle interface (`start`, `submit`, `inspect`, `cancel`).
- `skills/daily-papers/scripts/shared/standalone_coordinator.py`: the only
  Harness-facing lifecycle and publication interface for standalone
  `paper-reader` and `generate-mocs` calls.
- `skills/daily-papers/scripts/shared/stage_report.py`: strict Stage Report v1
  parsing, phase binding, scoped path resolution, and report-artifact creation.
- `skills/daily-papers/scripts/shared/run_lifecycle.py`: Manifest v2 mutation,
  validation, revision CAS, checkpoints, artifacts, and atomic snapshots.
- `skills/daily-papers/scripts/shared/run_guardian.py`: run/session liveness
  guardian plus the clone-wide Vault writer lock shared by daily and
  standalone work. It exclusively owns long-lived guardian process launch,
  readiness proof, timeout cleanup, and child reaping; it never writes a
  Manifest.
- `skills/daily-papers/scripts/shared/paper_identity.py`: the single stable
  paper-ID and collision-preserving existing-note matching seam.
- `skills/daily-papers/scripts/shared/safe_io.py`: the single bounded local
  file/JSON seam. It performs nofollow regular-file reads, strict JSON parsing,
  deterministic strict JSON encoding with post-encoding byte budgets,
  descriptor-pinned inspect/copy snapshots, streaming hashes, and durable
  atomic replacement.
- `skills/daily-papers/scripts/shared/safe_http.py`: the single programmatic
  remote-fetch seam. It validates every redirect hop and DNS answer, pins
  public destinations, and enforces header, encoding, byte, and deadline
  budgets for metadata, HTML, PDF, and image downloads.
- `skills/daily-papers/scripts/shared/safe_process.py`: the single
  document-tool execution seam. It accepts argument vectors only, bounds both
  captured streams, kills and reaps the whole process group on timeout or
  overflow, and optionally limits child-created file size.
- `skills/daily-papers/scripts/shared/safe_path.py`: the single portable
  relative-path parsing and containment seam. It enforces normalized POSIX
  syntax, character budgets, control-character rejection, and symlink-aware
  root containment before domain policy is applied.
- `skills/daily-papers/scripts/shared/safe_git.py`: the single bounded Git
  process/blob seam. Repository commands have deadlines and stream limits;
  repository identity and NUL-delimited dirty status are parsed once; blob
  reads additionally pin an object ID, preflight its size, and materialize only
  caller-bounded content through `safe_process.py`.
- `skills/daily-papers/scripts/shared/runtime_context.py`: strict one-shot
  machine/shared configuration resolver. Daily entry creates one immutable
  Runtime Context and passes it to all internal stages.
- `skills/daily-papers/scripts/daily/candidate_approval.py`: the model-agnostic
  relevance-approval seam. It materializes one immutable Markdown per acquired
  paper, reports missing Evaluation v1 files, validates their paper/input
  binding, and collects the bounded enrichment pool.
- `skills/daily-papers/scripts/shared/config_schema.py`: the only configuration
  schema, overlay, normalization, and fingerprint implementation. All config
  consumers go through `user_config.load_user_config()` or this module.
- `skills/daily-papers/scripts/shared/active_run_guard.py`: lightweight
  read-only guard for standalone writers. It fetches and inspects task state
  from the configured remote branch without mutating the worktree.
- `tools/sync_public_skills.py`: materializes self-contained public Skills from
  the canonical suite through bounded nofollow source reads, bounded generated
  tree snapshots, and atomic target replacement. It rejects duplicate resource
  declarations, symlinks, special files, and unsafe or excessive generated
  trees before changing them, and checks file plus empty-directory drift.

Every public Skill must remain independently installable and include every
runtime dependency. Internal stage workflows are resources, not discoverable
Skills.

Keep research keywords, scoring, paths, templates, generated Markdown, and
Subagent delegation rules in the shared workflow. Keep explicit invocation
syntax, CLI flags, and host tool wording in runtime adapters.

Fetch, review, notes, and paper-reader must exchange canonical `paper_id`
values. Exact identity matches win; legacy method/title matching is permitted
only when unique, and ambiguity must never select or overwrite a note.

## Configuration and safety

- Tracked defaults live in
  `skills/daily-papers/scripts/shared/user-config.json`.
- Research settings belong in the shared Vault configuration. Machine-local
  Vault and optional Zotero paths belong only in the cross-Harness machine
  file; installed Skill directories never carry implicit local overlays.
- Installation onboarding writes the shared cross-Harness machine file at
  `~/.config/dailypaper/config.json` by default. It stores only per-machine
  Vault and optional Zotero paths; `DAILYPAPER_MACHINE_CONFIG` may override its
  location. `scripts/configure/onboard.py` owns clone/validation, resumable
  Vault bootstrap, and machine-file persistence in that order; prompts must not
  reconstruct those steps.
- Zotero is optional. Do not access its SQLite database for ordinary arXiv or
  local-PDF inputs.
- Explicit Zotero inputs may query only a temporary read-only SQLite snapshot.
  Public Skills never mutate the Zotero database or launch a nested Harness
  process; classification changes are user actions in the Zotero UI.
- The persistent Linux server stores its Vault clone at
  `/workspace/dailypaper-vault`. Treat this as per-machine environment
  configuration (`DAILYPAPER_VAULT`), not a tracked absolute path.
- Resolve scripts from the Skill location, not the caller's current directory.
- Do not use shared fixed temporary filenames.
- Programmatic remote reads must use `safe_http.py`; do not add another
  `urllib`, `curl`, redirect, DNS, or response-budget implementation.
- `safe_http.FetchedFile` owns the streaming SHA-256 and immutable file
  identity. Media inspection should use `read_verified_prefix()` and reuse
  its `bytes`/`sha256`; do not perform a second full-file hash of the same
  freshly downloaded artifact.
- Existing files returned by untrusted document tools enter through
  `safe_io.inspect_regular_file`; publishing one of them uses
  `safe_io.copy_regular_file` so the size, prefix, hash, and staged bytes all
  come from one nofollow descriptor. Do not reconstruct
  `stat + header read + hash + copy` as separate path-based operations.
- Aggregate remote byte and deadline accounting belongs to
  `safe_http.FetchBudget`. Callers may choose limits and map errors, but must
  not implement another budget state machine. One logical fetch/enrichment
  phase reuses one budget across endpoints, retries, and concurrent workers;
  the shared budget performs atomic byte accounting.
- Daily acquisition accepts a 1-31 day window. arXiv category/date acquisition
  must page against `totalResults`, respect the shared fetch budget and request
  delay, and fail closed when completeness cannot be proved. A non-empty arXiv
  snapshot is the authoritative acquired set; HF may enrich matching IDs and
  becomes a bounded fallback only when that snapshot is proven empty.
  Deterministic keyword or history signals never remove an acquired arXiv paper
  before per-paper approval. The acquisition summary binds the complete scope
  and acquired bytes; resume reuses a valid candidate index first, then a bound
  acquisition pair, and only then performs a new network fetch. Enrichment rejects
  excessive paper counts and creates at most one semaphore-sized batch of
  asyncio tasks at a time; a semaphore alone is not permission to materialize
  one task per untrusted input item.
- Untrusted document tools such as `pdftotext` and `pdfimages` must use
  `safe_process.run_bounded_tool`; do not add direct `subprocess.run`,
  `Popen`, or async subprocess capture paths for them.
- Coordinators start long-lived lock holders only through
  `run_guardian.ensure_guardian_running`; do not reconstruct `Popen`, readiness
  polling, timeout cleanup, or child-reaping logic in a caller.
- Read-only Git blob inspection must use `safe_git.read_git_blob`; do not
  capture `git show` or `git cat-file` content before checking the immutable
  object size. Other Git commands use `safe_git.run_git_command` or
  `run_git_program`, including clone/setup paths. Git transaction command
  choice and return-code semantics remain owned by their lifecycle modules.
- Repository root/remote/branch inspection uses `safe_git.inspect_repository`;
  dirty paths use `safe_git.repository_dirty_paths`. Do not reconstruct either
  with caller-local Git command sequences. Dirty status must stay
  NUL-delimited, preserve both rename/copy paths, and reject unsafe or excessive
  path sets before lifecycle logic consumes them.
- Untrusted relative paths use `safe_path.relative_posix_path` and
  `safe_path.resolve_within`; callers map errors and retain domain-specific
  reserved-root or Run/Vault-scope policy instead of implementing another
  lexical or symlink-containment parser.
- PDF image fallback must run a bounded `pdfimages -list` plan first, reject
  excessive image count or estimated decoded bytes, and recheck actual total
  extracted bytes before selecting an artifact.
- Vault and run-directory discovery must be bounded before sorting or
  materializing results. MOC planning snapshots each directory exactly once;
  paper-note indexing and recovery-report/session discovery enforce explicit
  directory, entry, or file-count limits.
- Shared configuration updates must refuse an active `running` daily task,
  enter through `config_manager.py apply`, hold the shared Vault writer lock,
  fresh-fetch remote ownership at the final apply boundary, require a clean
  current clone, validate supported fields deterministically, and publish only
  `.dailypaper/config.json` through its crash-resumable journal. Harness prompts
  never run Git publication commands for configuration changes. A lost
  temporary patch resumes through `config_manager.py resume`, using only the
  validated Git-dir journal.
- Configuration Skills must reject inert fields rather than recording values
  that no runtime code consumes. Per-machine paths and credentials never enter
  the shared Vault config.
- Disk JSON enters through `scripts/shared/safe_io.py`: reads are bounded,
  regular-file-only, strict UTF-8, and do not follow symlinks; duplicate keys
  and non-standard constants are rejected there. Writes use
  `safe_io.encode_json_value` when a domain must compare or hash encoded bytes,
  or when a bounded pipeline artifact must be serialized;
  `safe_io.atomic_write_json` handles direct state replacement. Do not add
  another manual `json.dumps + newline` disk codec. Regular-file hashing,
  bounded snapshot inspection, and verified copying also enter through this
  nofollow descriptor seam. Domain locks, byte comparisons, revision/hash CAS,
  journals, backups, and recovery remain owned by their lifecycle modules.
  Configuration-specific unknown fields, unsafe/reserved paths, overlapping
  output trees, and invalid automation combinations remain errors owned only
  by `config_schema.py`.
- Bootstrap may create one initialization commit for an empty Vault. A full
  coordinated daily run then creates one acquisition commit and at most one
  content commit, both with ordinary pushes. Standalone helper pushes remain
  opt-in.
- Daily entry must call only the coordinator's start-or-resume operation.
  That interface owns onboarding validation, necessary bootstrap and
  fast-forward synchronization, remote-state inspection, and Runtime Context
  resolution. A `ready` response carries the one Runtime Context that every
  internal stage must reuse without rereading or merging configuration, plus
  an immutable run-local `runtime-context.json` for child processes. Remote
  Task State is fetched from the fixed repository endpoint before stale local
  shared configuration is trusted. Fresh acquisition is bound to that fetched
  remote HEAD; a crash after lock push but before local Manifest recording is
  reconciled idempotently from `prepared`. The entry must not create or
  advance a Manifest directly.
- Standalone Vault writers must enter through `standalone_coordinator.py`.
  That interface owns fresh remote inspection, the shared Vault writer lock,
  frozen context and baseline, artifact/hash registration, resume/cancel, and
  optional exact commit+push. A dirty clone behind or divergent from the
  fetched remote must not be modified. Normal parent submissions pass exact
  Vault-relative paths and let the coordinator compute hashes; a prebuilt
  report is reserved for immutable delegated-worker handoff. Empty successful
  change sets terminate as `unchanged` without a commit.
- Internal stages write Stage Report v1 inside the current Run directory.
  Artifacts use explicit `run` or `vault` scopes, Run Change Set entries are
  Vault-relative, and the parent submits only with `submit --report`.
- Paper notes must pass the shared deterministic structural validator in both
  the reader and parent notes stage. Failed validation preserves the note and
  resources for retry or attention-required recovery.
- Vault Task State is the cross-machine ownership authority; Manifest v2 is
  only the local recovery authority. Same-machine resume requires matching
  ownership, configuration fingerprint, Workflow Contract, checkpoints,
  registered artifact hashes, and an allowed Run Change Set.
- Manifest v2 separates strict forward Phase (`prepared`, `fetching`,
  `reviewing`, `writing-notes`, `validated`, `publishing`), non-terminal
  Condition (`active`, `interrupted`, `attention-required`), and immutable
  Outcome (`published`, `failed`, `cancelled`).
- A missing local run directory for a remote `running` run means
  cross-machine recovery. Never auto-preempt it: show the exact `run_id`, ask
  the user, then fresh-fetch and cancel only through remote-HEAD/run-ID CAS.
- Production guardians have no idle expiry. A live local guardian is never
  preempted by elapsed time; after the user confirms the exact run/session ID,
  the coordinator may stop it and resume only the same validated local state.
- `attention-required` does not auto-resume. Cancellation preserves local
  artifacts.
- A canonical pending Stage Report may recover after guardian loss only for
  dirty paths declared by the report and backed by exact Vault artifacts;
  unrelated dirty paths still block. Every failure class checkpoints safe
  report evidence before terminal publication.
- Publication records one fixed content commit and reuses it idempotently.
  Immediately before publication, the lifecycle rechecks every bounded
  registered artifact and requires every existing Run Change Set path to be
  backed by one of them. Unknown dirty paths, registered artifact hash
  changes, late unregistered files, or an unexpected remote HEAD block the
  run. Before exact staging, the current Git index blob for every path must
  equal either its base version or registered artifact; never overwrite a
  third staged version.
- Git automation must stage only bootstrap files or paths registered by the
  current daily Run or standalone Session. Standalone commit/push toggles must
  move together. Never use force push or automatic rebase for Vault
  coordination.

## Branch maintenance

- The unified branch contains exactly four public portable `SKILL.md` files,
  each with only `name` and `description` frontmatter and no vendor-specific
  sidecar metadata.
- Formal releases use immutable semantic tags (`vMAJOR.MINOR.PATCH`) on commits
  reachable from `main`. Update `CHANGELOG.md`, pinned README installation
  examples, and attribution before tagging; never move a published tag.
- `.github/release.yml` owns generated release-note categories.
  `.github/workflows/release.yml` must re-run the release gates before creating
  a GitHub Release and must not publish tags from feature-only history.
- Preserve the upstream attribution in `README.md` and `NOTICE`. This repository
  is a maintained derivative of `huangkiki/dailypaper-skills`, not an official
  upstream release.
- Harness identity is selected at runtime (`claude-code` or `codex`), never by
  switching the skills Git branch.
- Prompt-level Subagent delegation must degrade to inline execution. Relevance
  approval uses one logical task per candidate Markdown and a bounded worker
  pool, preferring a low-cost model when the Harness exposes model choice.
  Subagents only produce Evaluation v1 files, artifact candidates, and progress
  reports; they never mutate the Manifest, guardian lock, Vault ownership, or
  Git publication.
- Public Skill prompts use progressive disclosure. `paper-reader` and
  `generate-mocs` route independent calls to the bundled
  `references/standalone-session.md`; do not duplicate coordinator command
  recipes, resume/cancel rules, hashing, or Git semantics in both workflows.
- Preserve the prompt byte budgets enforced by `test_harness_contract.py`.
  Move low-frequency detail into an explicitly routed bundled reference rather
  than expanding the always-loaded `SKILL.md`.
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
