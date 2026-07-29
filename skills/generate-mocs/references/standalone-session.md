# Standalone session contract

`paper-reader` and `generate-mocs` share one crash-resumable Vault-writing
session. The parent task owns the session and its guardian; a delegated worker
only writes candidate artifacts and returns their paths.

## Start or resume

```bash
python3 "{SKILL_ROOT}/scripts/shared/standalone_coordinator.py" start \
  --operation "{paper-reader|generate-mocs}" \
  --harness "{claude-code|codex}" \
  --intent "{stable intent}"
```

- For `paper-reader`, use the stable `paper_id` when already known; otherwise
  use the normalized explicit paper input and keep it unchanged on retry.
- For `generate-mocs`, use `scope:all`, `scope:papers`, or `scope:concepts`.
- Continue only on `decision=ready`. Reuse the returned `session_id`,
  `runtime_context_file`, and frozen `runtime_context`.
- `start` owns machine-onboarding validation, clean-clone fast-forward,
  fresh remote DailyPaper Task State inspection, configuration freezing, and
  the local Vault writer lock. Never reproduce those steps in a Skill prompt.
- If machine onboarding is missing or invalid, stop and ask the user to run
  `configure-dailypaper`; never guess a Vault from the current directory.
- `still-running` means the existing session still owns the clone-wide Vault
  writer lock. Show the returned exact `session_id` to the user and stop. Do
  not infer abandonment from elapsed time, and never preempt it automatically.
- Only after the user explicitly confirms that exact ID may the parent repeat
  the same start request with
  `--confirm-running-session-id "{SESSION_ID}"`. This stops only the live local
  guardian and resumes the same session after all normal recovery validation;
  it does not cancel remote state or delete artifacts:

  ```bash
  python3 "{SKILL_ROOT}/scripts/shared/standalone_coordinator.py" start \
    --operation "{paper-reader|generate-mocs}" \
    --harness "{claude-code|codex}" \
    --intent "{the same stable intent}" \
    --confirm-running-session-id "{SESSION_ID}"
  ```

- A missing/dead guardian is an abnormal interruption and remains
  automatically recoverable after configuration, baseline, artifact, dirty
  path, and remote-state checks. It does not require the live-guardian
  confirmation flag.
- `attention-required` must be inspected. Never cancel it until the user has
  been shown the exact ID and explicitly confirms cancellation.

## Submit changed paths

For the normal parent workflow, pass each final Vault-relative path once. The
coordinator normalizes each path, opens it without following symlinks, computes
its SHA-256, and records the exact change set:

```bash
python3 "{SKILL_ROOT}/scripts/shared/standalone_coordinator.py" submit \
  --session-id "{SESSION_ID}" \
  --result success \
  --path "论文笔记/Robotics/Method.md" \
  --path "论文笔记/_概念/Concept.md"
```

`--result` is `progress`, `success`, `recoverable`, or `attention-required`.
`--message` is optional. A successful submission with no `--path` returns
`unchanged` and never creates an empty commit.

Rules:

- Every path is normalized and Vault-relative. Absolute paths, `..`, `.git`,
  `.dailypaper`, symlinks, missing files, and duplicate paths are rejected.
- Include notes, concepts, localized images, and generated MOCs that actually
  changed. Do not claim paths that were dirty when the session started.
- Do not include session metadata or other files below `.dailypaper`.

The coordinator rechecks artifact hashes, baseline ownership, remote Task
State, remote HEAD, and the frozen configuration. If Git automation is off it
returns `completed-local`. If enabled, commit and push happen together as one
recoverable transaction; a retry reuses the recorded commit.
Pre-existing dirty paths are preserved and cannot be claimed by the session.

## Optional prebuilt report

Use a bounded report only when a delegated worker must hand an immutable
artifact declaration to the parent. Write it beside `standalone-session.json`
inside the ignored local Run directory:

```json
{
  "version": 1,
  "session_id": "standalone-paper-reader-0123456789abcdef",
  "operation": "paper-reader",
  "result": "success",
  "artifacts": [
    {
      "path": "论文笔记/Robotics/Method.md",
      "sha256": "64-lowercase-hex-digest",
      "kind": "note"
    }
  ],
  "changed_paths": ["论文笔记/Robotics/Method.md"],
  "message": null
}
```

Additional report rules:

- Every path is normalized and Vault-relative. Absolute paths, `..`, `.git`,
  `.dailypaper`, symlinks, missing files, duplicate paths, and hash mismatches
  are rejected.
- `changed_paths` exactly equals the artifact path set.
- Include notes, concepts, localized images, and generated MOCs that actually
  changed. Do not claim paths that were dirty when the session started.
- Use SHA-256 of the final on-disk bytes. Do not include the report itself.

Submit it:

```bash
python3 "{SKILL_ROOT}/scripts/shared/standalone_coordinator.py" submit \
  --session-id "{SESSION_ID}" \
  --report "{SESSION_DIR}/report.json"
```

Do not combine `--report` with `--result`, `--path`, or `--message`.

## Inspect, resume, or cancel

```bash
python3 "{SKILL_ROOT}/scripts/shared/standalone_coordinator.py" inspect \
  --session-id "{SESSION_ID}"
```

After an interruption, call `start` again with the same operation and intent.
Registered artifacts whose hashes still match are resumed. Unknown dirty paths
are preserved and require an explicit report. If this returns `still-running`,
the previous execution is not considered interrupted; use the explicit
live-guardian confirmation flow above only after asking the user.

Cancellation preserves every artifact:

```bash
python3 "{SKILL_ROOT}/scripts/shared/standalone_coordinator.py" cancel \
  --session-id "{SESSION_ID}" \
  --confirm-session-id "{SESSION_ID}"
```

Only run cancellation after showing the exact session ID to the user and
receiving explicit confirmation.
