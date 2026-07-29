# Changelog

All notable changes to this project are documented in this file.

The project follows [Semantic Versioning](https://semver.org/). Because Skills
contain prompts and executable helpers, incompatible workflow contracts,
configuration schemas, stable output paths, or installation interfaces count
as breaking changes.

## [Unreleased]

### Added

- Nothing yet.

## [1.0.0] - 2026-07-30

First formal release of the maintained unified-harness derivative.

### Added

- Four independently installable public Skills: `configure-dailypaper`,
  `daily-papers`, `paper-reader`, and `generate-mocs`.
- Shared Claude Code and Codex inputs, outputs, prompts, and machine
  configuration.
- First-run Vault onboarding with resumable bootstrap and atomic machine
  configuration persistence.
- Remote Task State, local Manifest v2, Stage Report v1, Runtime Context, and
  Standalone Session lifecycle contracts.
- Stable paper identity and collision-preserving existing-note matching.
- Shared paper-reading core for daily notes and standalone reading.
- Deterministic paper-note validation, link backfill, and MOC generation.
- Bounded safe I/O, HTTP, Git, process, path, JSON, PDF, and artifact seams.
- Cross-machine and cross-harness Vault coordination with exact change-set
  publication.
- Python 3.10/3.12 CI and self-contained installation tests.
- One shared Release Gate for generated-package drift, high-signal static
  safety checks, source compilation, regression tests, and patch formatting.
- A pinned `skills@1.5.20` Claude Code/Codex installation smoke test that
  verifies exact copied trees before every release.
- A release-oriented deployment guide covering prerequisites, installation
  acceptance, arXiv window semantics, local scheduling, recovery,
  troubleshooting, upgrades, rollback, and uninstall.

### Changed

- Replaced harness-specific branches and sidecars with portable `SKILL.md`
  packages and runtime harness identity.
- Converted fetch, review, and notes from public Skills into private coordinated
  stages.
- Moved machine-local Vault and optional Zotero paths into one cross-harness
  machine configuration file.
- Made `window_days` an immutable daily Run intent.
- Centralized paper reading, configuration schema, Git inspection, MOC
  generation, and publication ownership to remove duplicate workflows.
- Anchored fallback history discovery to the immutable Run target date instead
  of the server's local wall-clock date.

### Security

- Active Runs and Sessions cannot be preempted by lease or idle expiry.
- Live guardian replacement and cross-machine cancellation require exact
  user-confirmed Run or Session IDs.
- Publication rejects unknown dirty paths, changed registered artifacts,
  unexpected remote commits, symlink escapes, and unregistered staged content.
- Bounded document tools apply child file limits through an isolated
  exec-wrapper instead of thread-unsafe `preexec_fn`.
- arXiv Atom parsing rejects DTD/entity declarations before XML parsing, and
  Zotero recursive collection lookup uses a cycle-safe parameterized CTE.
- Production safety checks remain active under optimized Python instead of
  depending on removable `assert` statements.
- Zotero access is optional, explicit-input-only, and read from a temporary
  read-only SQLite snapshot.

### Attribution

- Derived from
  [huangkiki/dailypaper-skills](https://github.com/huangkiki/dailypaper-skills)
  under Apache-2.0. See `NOTICE`.

[Unreleased]: https://github.com/haoz0206/dailypaper-skills/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/haoz0206/dailypaper-skills/releases/tag/v1.0.0
