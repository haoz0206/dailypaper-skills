# DailyPaper

DailyPaper discovers, reviews, and records academic papers in a shared Vault
while coordinating runs across machines and Harnesses.

## Language

**DailyPaper Run**:
One attempt to produce the requested paper recommendations and associated
Vault content for a target date.
_Avoid_: Job, session

**Run Phase**:
The ordered stage of work reached by a DailyPaper Run.
_Avoid_: Run Condition, Run Outcome

**Run Condition**:
Whether a non-terminal DailyPaper Run is active, interrupted, or waiting for
user attention.
_Avoid_: Run Phase, Run Outcome

**Run Outcome**:
The immutable published, failed, or cancelled result of a terminal DailyPaper
Run.
_Avoid_: Run Phase, Run Condition

**Run Manifest**:
The local record of one DailyPaper Run's lifecycle and produced artifacts. It
does not grant or release cross-machine ownership.
_Avoid_: Lock file, task state

**Run Coordinator**:
The sole local writer of a Run Manifest and the execution responsible for
advancing that DailyPaper Run through its phases.
_Avoid_: Subagent, Vault Task State

**Run Guardian**:
The local process that holds a DailyPaper Run's execution/liveness lock and
reports its liveness. It does not write the Run Manifest or own the Vault task.
_Avoid_: Run Coordinator, Manifest writer

**Manifest Revision**:
The monotonically increasing version of one Run Manifest snapshot, used for
compare-and-set mutation and atomic recovery.
_Avoid_: Run Phase, Git commit

**Vault Task State**:
The shared record that arbitrates ownership of a DailyPaper task across
machines and Harnesses.
_Avoid_: Run Manifest, local state

**Run Ownership**:
The exclusive right of one DailyPaper Run to publish changes for a DailyPaper
task. It cannot be preempted unless a user explicitly cancels the owning Run.
_Avoid_: Local process lock, Run Manifest status

**Interrupted Run**:
A DailyPaper Run that stopped without publishing failure while its ownership
may still be valid. It is eligible only for explicit, ownership-verified resume.
_Avoid_: Failed Run, automatic retry

**Resumed Run**:
An Interrupted Run that continues under its original identity after ownership,
liveness, and existing artifacts have been verified.
_Avoid_: Retry, new run

**Run Checkpoint**:
A verified record of completed work within one lifecycle stage that a Resumed
Run may safely reuse.
_Avoid_: Lifecycle state, unverified partial output

**Run Artifact**:
An intermediate or final file produced by one DailyPaper Run. A Run Manifest
records a verified reference to it rather than embedding its content.
_Avoid_: Run Manifest field, unchecked file

**Run Change Set**:
The exact Vault paths attributed to one DailyPaper Run. Only these paths may be
reused during resume or included in that Run's publication.
_Avoid_: Dirty worktree, all Vault changes

**Configuration Fingerprint**:
The stable identity of output-affecting DailyPaper configuration used by a Run.
Machine-local paths and credentials are excluded.
_Avoid_: Entire configuration file, machine configuration

**Workflow Contract**:
The versioned meaning of lifecycle stages, Run Artifacts, and validation rules
shared by all supported Harnesses.
_Avoid_: Harness prompt, implementation version

**Recoverable Interruption**:
A temporary condition under which the same DailyPaper Run may continue with
the same intent and verified checkpoints.
_Avoid_: Deterministic Failure, retry as a new Run

**Attention-required Run**:
An Interrupted Run that has exhausted automatic recovery and still retains
ownership while waiting for a user decision.
_Avoid_: Failed Run, abandoned lock

**Deterministic Failure**:
A condition under which the same DailyPaper Run cannot complete with its
current intent, configuration, and Workflow Contract.
_Avoid_: Recoverable Interruption, user cancellation

**Failed Run**:
A DailyPaper Run whose failure has been published. It is terminal; another
attempt is a new DailyPaper Run.
_Avoid_: Interrupted Run, resumable run

**Cancelled Run**:
A DailyPaper Run whose ownership was explicitly revoked after user
confirmation. It is terminal and cannot be resumed.
_Avoid_: Failed Run, Interrupted Run

**Cross-machine Recovery**:
Recovery from an unavailable owner machine by explicitly cancelling its Run
and starting a new DailyPaper Run on a replacement machine. It is not a resume.
_Avoid_: Cross-machine resume, automatic takeover

**Validated Run**:
A DailyPaper Run whose intended artifacts have passed validation but have not
necessarily been accepted by the shared Vault.
_Avoid_: Published Run, successful run

**Published Run**:
A DailyPaper Run whose validated artifacts and successful task state have been
accepted by the shared Vault. It is terminal.
_Avoid_: Validated Run, locally complete run
