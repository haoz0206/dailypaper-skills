---
status: accepted
---

# Deepen the DailyPaper Run lifecycle

The Run Coordinator will expose one lifecycle module through three operations:
`start`, `submit`, and `cancel`, plus a read-only `inspect` operation. `start`
creates or resumes a DailyPaper Run and returns its next instruction; `submit`
reports a phase result and lets the coordinator validate and advance it;
`cancel` requires user confirmation of a specific `run_id` and performs a
fresh-pull compare-and-set; `inspect` never mutates state. This small interface
keeps lifecycle rules identical across short-lived Claude Code and Codex CLI
calls.

The local Run Manifest is authoritative for one Run's Phase, Condition,
Outcome, checkpoints, artifacts, change set, configuration fingerprint, and
Workflow Contract. Vault Task State remains the sole shared authority for
cross-machine ownership. Phase advances strictly from `prepared` through
`fetching`, `reviewing`, `writing-notes`, `validated`, and `publishing`;
Condition expresses `active`, `interrupted`, or `attention-required`;
immutable Outcome expresses `published`, `failed`, or `cancelled`.

`submit` accepts only `progress`, `success`, `recoverable`, `attention`, or
`deterministic-failure`. The coordinator chooses the next Phase; a caller
cannot skip lifecycle validation by naming a target Phase. Phase progression
is forward-only; resuming the current Phase and repeating an unchanged
`progress` checkpoint are idempotent. The Manifest holds coarse lifecycle
checkpoints plus stage-specific progress checkpoints.

Same-machine resume requires the matching local Run directory, valid Manifest,
compatible configuration and Workflow Contract, and verified checkpoints.
Checkpoint reuse requires registered paths and matching content hashes; dirty
paths outside the Run Change Set, or changed registered files, stop recovery to
protect user edits. Publication records and reuses the same content commit so a
lost push response can be recovered idempotently.

`start` first fetches the immutable configured repository endpoint and captures
one remote HEAD plus strict Task State before trusting the local shared
configuration. After a safe fast-forward it resolves one Runtime Context,
freezes it as run-local `runtime-context.json`, and passes that file to child
processes. Acquisition compares against the captured remote HEAD and consumes
the frozen context instead of pulling or rereading configuration internally.
If the lock commit reaches the remote but the process crashes before local
acquisition metadata is recorded, the next `start` verifies the matching
`run_id`, configuration fingerprint, and remote HEAD, then reconciles the
`prepared` Manifest idempotently.

A Run Manifest has one writer. Because the Harness invokes several
short-lived CLI helpers and those processes cannot retain an OS file lock for
the whole Run, a per-Run local guardian process holds the run-wide
execution/liveness `flock`. The guardian prevents concurrent parent
coordinators and exposes liveness diagnostics, but it does not write the
Manifest. The Run Coordinator is the only supported Manifest writer. Each
mutation goes through RunLifecycle, which takes a short-lived manifest lock,
checks the expected revision, and atomically replaces the snapshot while
retaining the previous revision. Subagents may produce artifacts but never
write the Manifest, hold the guardian lock, change ownership, or publish.
Production guardians do not expire from idleness. If a detached guardian
survives an interrupted Harness, the AI shows the exact Run or Session ID and
asks the user before stopping it; the coordinator then resumes the same local
state only after all normal recovery checks.

Internal stages hand results to the parent through a strict Stage Report v1
stored inside the current Run directory. Artifact paths name an explicit
`run` or `vault` scope and change-set paths are Vault-relative. The coordinator
binds the report to the current phase, registers the report itself as an
artifact, and checkpoints safe evidence before a recoverable interruption.

Cross-machine recovery is deliberately not resume. When Vault Task State names
a running Run but the local Run directory is absent, the AI must show that Run
to the user and ask whether to cancel its exact `run_id`. After confirmation,
`cancel` fetches again and uses both the proposed remote HEAD and the still
running `run_id` as a compare-and-set; a replacement machine then starts a new
Run. A lease timeout is diagnostic only and never authorizes preemption.

Recoverable network, quota, or process interruption may become
`interrupted`. A deterministic failure becomes terminal `failed`. Exhausted
automatic recovery becomes `attention-required`, retains ownership, and does
not resume without user direction. Cancellation is terminal but retains local
artifacts.

Publication creates and records one fixed content commit. If a push response
is lost, recovery pushes that same commit when the remote remains at the
acquisition commit, or finalizes when the remote already equals the recorded
content commit. Any other remote HEAD requires attention. Publication never
rebases, force pushes, or creates a replacement content commit.

## Considered options

- A Run Manifest as the global lock was rejected because local ignored state
  cannot arbitrate across machines.
- Per-command file locking was rejected because it cannot enforce a run-wide
  execution owner between short CLI calls. A per-mutation Manifest lock is
  still required for revision CAS and crash-safe snapshots.
- `machine_id` and resume tokens were rejected because different machines are
  used only for disaster recovery, where explicit user-confirmed cancellation
  is simpler and matches the operating model.

## Consequences

The Run Coordinator concentrates recovery, ownership checks, validation, and
publication behind one interface, but requires lifecycle management for a
small local guardian. If the guardian dies, the OS releases its lock; a later
`start` may recreate it only after validating the existing local Run and
current Vault Task State. If it remains alive after its original Harness has
stopped, an exact-ID user confirmation is required before the same Run can be
resumed. Cancelled Runs retain local artifacts until the user explicitly
chooses to clean them up.
