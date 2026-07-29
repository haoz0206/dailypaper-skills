#!/usr/bin/env python3
"""External start-or-resume interface for coordinated DailyPaper Runs.

This module is the sole Harness-facing writer of the local Run Manifest.  It
combines the local lifecycle and guardian modules with Vault Task State
coordination, while leaving Git and task-state mutation to vault_coordination.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import run_guardian
import runtime_context
import stage_report
import vault_coordination
from run_lifecycle import (
    DAILY_WORKFLOW_CONTRACT,
    MAX_MANIFEST_BYTES,
    ArtifactCandidate,
    Interruption,
    LifecycleError,
    RunLifecycle,
    RunSnapshot,
)
from safe_io import (
    DocumentTooLargeError,
    SafeIOError,
    anchored_file_path,
    atomic_write_bytes,
    encode_json_value,
    inspect_regular_file,
    load_json_object,
    parse_json_object,
    read_regular_bytes,
)


MAX_PENDING_STAGE_REPORTS = 1024


WORKFLOW_CONTRACT = DAILY_WORKFLOW_CONTRACT
DEFAULT_GUARDIAN_IDLE_TIMEOUT: float | None = None
REMOTE_RUNNING = frozenset({"running"})
REMOTE_PUBLISHED = frozenset({"success", "published"})
MAX_PROPOSAL_BYTES = 1024 * 1024
MAX_RUNTIME_CONTEXT_BYTES = 1024 * 1024


class CoordinatorError(RuntimeError):
    """A safe, expected Run Coordinator failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _validated_runtime() -> dict[str, Any]:
    """Resolve the strict runtime contract at every coordinator mutation boundary."""
    try:
        return runtime_context.resolve_runtime_context()
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise CoordinatorError("invalid-runtime-context", str(exc)) from exc


def _configured_vault() -> Path:
    """Resolve onboarding state without requiring an initialized shared config."""
    try:
        return runtime_context.resolve_vault_path()
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise CoordinatorError("invalid-machine-context", str(exc)) from exc


def _context_for_vault(vault: Path) -> dict[str, Any]:
    context = _validated_runtime()
    resolved = Path(str(context["paths"]["vault"])).expanduser().resolve()
    if resolved != vault:
        raise CoordinatorError(
            "vault-context-changed",
            f"Runtime Vault changed from {vault} to {resolved}.",
        )
    return context


def _bootstrap_vault(vault: Path) -> dict[str, Any]:
    result = vault_coordination.bootstrap_vault(vault)
    if result.get("status") not in {"bootstrapped", "already-bootstrapped"}:
        raise CoordinatorError(
            "bootstrap-failed",
            f"Unexpected Vault bootstrap result: {result.get('status')}",
        )
    return dict(result)


def _prepare_start_runtime(
    target_date: str | None,
) -> tuple[
    dict[str, Any] | None,
    Path,
    str,
    dict[str, Any],
    dict[str, Any],
]:
    """Prepare one safe start context without destroying resumable dirty artifacts."""
    vault = _configured_vault()
    date = _target_date(target_date, vault_coordination.FIXED_TIMEZONE)
    inspected = _remote_inspection(vault)
    state = inspected["task_state"]
    run_id = str(state.get("run_id") or "") if state else ""
    local_manifest = _manifest_path(vault, run_id) if run_id else None

    # Remote ownership is authoritative and can be inspected using only the
    # immutable repository endpoint. Cross-machine recovery must not be hidden
    # by a missing or stale local shared configuration.
    if state and state.get("status") in REMOTE_RUNNING:
        if (
            local_manifest is None
            or not local_manifest.parent.is_dir()
            or not local_manifest.exists()
        ):
            return (
                None,
                vault,
                date,
                inspected,
                {
                    "status": "preserved-for-cross-machine-recovery",
                    "vault": str(vault),
                    "run_id": run_id,
                    "local_manifest_missing": (
                        local_manifest is not None
                        and local_manifest.parent.is_dir()
                        and not local_manifest.exists()
                    ),
                },
            )
        context = _context_for_vault(vault)
        date = _target_date(target_date, str(context["runtime"]["timezone"]))
        return (
            context,
            vault,
            date,
            inspected,
            {
                "status": "preserved-for-recovery",
                "vault": str(vault),
                "run_id": run_id,
            },
        )

    if (
        state
        and state.get("status") in REMOTE_PUBLISHED
        and state.get("target_date") == date
    ):
        context = (
            _context_for_vault(vault)
            if local_manifest is not None and local_manifest.exists()
            else None
        )
        return (
            context,
            vault,
            date,
            inspected,
            {
                "status": "preserved-published-state",
                "vault": str(vault),
                "run_id": run_id,
            },
        )

    try:
        config_path = runtime_context.resolve_shared_config_path(vault)
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise CoordinatorError("invalid-runtime-context", str(exc)) from exc

    if not config_path.is_file():
        if os.environ.get("DAILYPAPER_CONFIG"):
            # An explicit source is authoritative; never silently replace it by
            # bootstrapping a different Vault-local configuration.
            _validated_runtime()
            raise AssertionError("unreachable")
        preparation = _bootstrap_vault(vault)
        context = _context_for_vault(vault)
        date = _target_date(target_date, str(context["runtime"]["timezone"]))
        return (
            context,
            vault,
            date,
            _remote_inspection(vault),
            preparation,
        )

    preparation = _bootstrap_vault(vault)
    # Bootstrap may fast-forward a new shared configuration. Re-resolve every
    # derived path and fingerprint, then re-fetch state to close the race
    # between the first inspection and synchronization.
    context = _context_for_vault(vault)
    date = _target_date(target_date, str(context["runtime"]["timezone"]))
    inspected = _remote_inspection(vault)
    return context, vault, date, inspected, preparation


def _dirty_paths(vault: Path) -> set[str]:
    return {str(value) for value in vault_coordination.dirty_paths(vault)}


def _inspect_task_state(
    vault: Path,
    *,
    snapshot: bool = False,
) -> dict[str, Any] | None:
    """Read the one public remote-backed Task State snapshot."""
    result = vault_coordination.inspect_task_state(vault)
    if snapshot:
        return dict(result)
    state = result.get("task_state")
    return dict(state) if isinstance(state, dict) else None


def _remote_inspection(vault: Path) -> dict[str, Any]:
    """Normalize production snapshots and lightweight test/legacy state readers."""
    value = _inspect_task_state(vault, snapshot=True)
    if isinstance(value, dict) and "task_state" in value:
        return value
    state = value if isinstance(value, dict) else None
    return {
        "status": "inspected",
        "vault": str(vault),
        "remote": vault_coordination.FIXED_REMOTE,
        "branch": vault_coordination.FIXED_BRANCH,
        "remote_head": None,
        "task_state": state,
    }


def _prepare_cancel(
    vault: Path,
    *,
    expected_run_id: str,
) -> dict[str, Any]:
    return dict(
        vault_coordination.prepare_cancel(
            vault,
            expected_run_id=expected_run_id,
        )
    )


def _cancel_vault(proposal: dict[str, Any]) -> dict[str, Any]:
    return dict(vault_coordination.cancel(proposal))


def _target_date(value: str | None, timezone: str) -> str:
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    return datetime.now(ZoneInfo(timezone)).date().isoformat()


def _window_days(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 31:
        raise CoordinatorError(
            "invalid-window-days",
            "window_days must be an integer from 1 to 31.",
        )
    return value


def _run_root(vault: Path) -> Path:
    configured = os.environ.get("DAILYPAPER_RUN_ROOT")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else vault / ".dailypaper" / "runs"
    )


def _manifest_path(vault: Path, run_id: str) -> Path:
    return _run_root(vault) / run_id / "manifest.json"


def _snapshot_fields(snapshot: RunSnapshot) -> dict[str, Any]:
    return {
        "run_id": snapshot.run_id,
        "window_days": snapshot.window_days,
        "manifest": str(snapshot.manifest_path),
        "phase": snapshot.phase,
        "condition": snapshot.condition,
        "outcome": snapshot.outcome,
        "revision": snapshot.revision,
    }


def _freeze_runtime_context(
    manifest: Path,
    context: dict[str, Any],
) -> Path:
    """Persist the one immutable context consumed by every stage process."""
    target = manifest.parent / "runtime-context.json"
    try:
        encoded = encode_json_value(
            context,
            max_bytes=MAX_RUNTIME_CONTEXT_BYTES,
            label="Frozen runtime context",
        )
    except DocumentTooLargeError as exc:
        raise CoordinatorError(
            "runtime-context-conflict",
            "Frozen runtime context exceeds the 1 MiB safety limit.",
        ) from exc
    except SafeIOError as exc:
        raise CoordinatorError("runtime-context-conflict", str(exc)) from exc
    if target.is_symlink():
        raise CoordinatorError(
            "runtime-context-conflict",
            f"Frozen runtime context must not be a symbolic link: {target}",
        )
    if target.exists():
        if not target.is_file():
            raise CoordinatorError(
                "runtime-context-conflict",
                f"Frozen runtime context is not a regular file: {target}",
            )
        try:
            current = read_regular_bytes(
                target,
                max_bytes=MAX_RUNTIME_CONTEXT_BYTES,
                label="Frozen runtime context",
            )
        except SafeIOError as exc:
            raise CoordinatorError("runtime-context-conflict", str(exc)) from exc
        if current != encoded:
            raise CoordinatorError(
                "runtime-context-conflict",
                "Frozen runtime context differs from the validated Run context.",
            )
        return target

    try:
        atomic_write_bytes(
            target,
            encoded,
            mode=0o600,
            label="Frozen runtime context",
        )
    except SafeIOError as exc:
        raise CoordinatorError("runtime-context-conflict", str(exc)) from exc
    return target


def _decision(name: str, **values: Any) -> dict[str, Any]:
    return {"decision": name, **values}


def _guardian_is_alive(run_dir: Path) -> bool:
    try:
        run_guardian.probe_guardian(run_dir)
        return True
    except run_guardian.GuardianError:
        return False


def _spawn_guardian(
    run_dir: Path,
    *,
    vault: Path,
    idle_timeout_seconds: float | None,
) -> None:
    try:
        run_guardian.ensure_guardian_running(
            run_dir,
            vault=vault,
            idle_timeout_seconds=idle_timeout_seconds,
        )
    except (run_guardian.GuardianError, OSError, ValueError) as exc:
        raise CoordinatorError("guardian-unavailable", str(exc)) from exc


def _stop_guardian(run_dir: Path) -> None:
    try:
        run_guardian.stop_guardian(run_dir)
    except run_guardian.GuardianError as exc:
        if _guardian_is_alive(run_dir):
            raise CoordinatorError(
                "guardian-stop-failed",
                f"Could not stop the active Run guardian: {exc}",
            ) from exc


def _open_lifecycle(
    manifest: Path,
    *,
    vault: Path | None = None,
    run_id: str | None = None,
) -> RunLifecycle:
    context = _validated_runtime()
    configured_vault = Path(context["paths"]["vault"]).expanduser().resolve()
    expected_vault = vault.expanduser().resolve() if vault is not None else configured_vault
    if expected_vault != configured_vault:
        raise CoordinatorError(
            "vault-mismatch",
            "Manifest Vault differs from the validated runtime Vault.",
        )
    return RunLifecycle.open(
        manifest,
        contract=WORKFLOW_CONTRACT,
        configuration_fingerprint=str(context["configuration_fingerprint"]),
        expected_vault=expected_vault,
        expected_run_id=run_id or manifest.expanduser().parent.resolve().name,
    )


def _verify_remote_owner(
    vault: Path,
    run_id: str,
    *,
    target_date: str,
    window_days: int,
) -> dict[str, Any]:
    state = _inspect_task_state(vault)
    if (
        not state
        or state.get("status") not in REMOTE_RUNNING
        or state.get("run_id") != run_id
    ):
        raise CoordinatorError(
            "ownership-lost",
            "Vault Task State is no longer running under this Run ID.",
        )
    if (
        state.get("target_date") != target_date
        or int(state.get("window_days", 1)) != window_days
    ):
        raise CoordinatorError(
            "intent-conflict",
            "Vault Task State intent no longer matches the local Run Manifest.",
        )
    return state


def _backed_pending_dirty_paths(
    *,
    vault: Path,
    artifacts: Iterable[ArtifactCandidate],
    changed_paths: Iterable[Path | str],
) -> set[str]:
    """Return declared Vault changes backed by an existing artifact file."""
    vault_root = vault.expanduser().resolve()
    backed: set[str] = set()
    for artifact in artifacts:
        try:
            path = anchored_file_path(artifact.path, label="Pending Run Artifact")
            relative = path.relative_to(vault_root).as_posix()
            inspect_regular_file(path, label="Pending Run Artifact")
        except (OSError, RuntimeError, SafeIOError, ValueError):
            continue
        backed.add(relative)

    declared: set[str] = set()
    for value in changed_paths:
        candidate = Path(value).expanduser()
        try:
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (vault_root / candidate).resolve()
            )
            declared.add(resolved.relative_to(vault_root).as_posix())
        except (OSError, RuntimeError, ValueError):
            continue
    return declared & backed


def _discover_pending_dirty_paths(snapshot: RunSnapshot) -> set[str]:
    """Strictly inspect canonical current-phase reports left by a crashed parent."""
    data = snapshot.as_dict()
    phase = snapshot.phase
    stage = stage_report.STAGE_BY_PHASE.get(phase)
    if stage is None:
        return set()
    run_dir = Path(data["paths"]["run_dir"])
    vault = Path(data["paths"]["vault"])
    progress_reports = list(
        islice(
            run_dir.glob(f"{stage}-progress-*.json"),
            MAX_PENDING_STAGE_REPORTS + 1,
        )
    )
    if len(progress_reports) > MAX_PENDING_STAGE_REPORTS:
        raise CoordinatorError(
            "too-many-pending-reports",
            "Run directory exceeds the "
            f"{MAX_PENDING_STAGE_REPORTS}-report recovery safety limit",
        )
    candidates = {
        run_dir / f"{stage}-result.json",
        *progress_reports,
    }
    pending: set[str] = set()
    for report_path in sorted(path for path in candidates if path.exists()):
        try:
            submission = stage_report.load_stage_report(
                report_path,
                phase=phase,
                run_dir=run_dir,
                vault=vault,
            )
            submission.verify_unchanged()
        except stage_report.StageReportError as exc:
            raise CoordinatorError(
                "invalid-pending-report",
                f"Pending stage report is invalid: {report_path}: {exc}",
            ) from exc
        pending.update(
            _backed_pending_dirty_paths(
                vault=vault,
                artifacts=submission.artifacts,
                changed_paths=submission.changed_paths,
            )
        )
    return pending


def _resume_lifecycle(
    lifecycle: RunLifecycle,
    vault: Path,
    *,
    require_user_confirmation: bool = False,
    pending_dirty_paths: Iterable[str] = (),
) -> RunSnapshot:
    snapshot = lifecycle.snapshot()
    observed_dirty = _dirty_paths(vault)
    pending = set(pending_dirty_paths)
    unexpected = observed_dirty - set(snapshot.run_change_set) - pending
    if unexpected:
        raise CoordinatorError(
            "unexpected-dirty-paths",
            "Vault contains changes outside this Run Change Set: "
            + ", ".join(sorted(unexpected)),
        )
    return lifecycle.resume(
        observed_dirty_paths=observed_dirty - pending,
        require_user_confirmation=require_user_confirmation,
    )


def _ensure_guardian(
    lifecycle: RunLifecycle,
    *,
    idle_timeout_seconds: float | None,
    pending_dirty_paths: Iterable[str] | None = None,
) -> RunSnapshot:
    snapshot = lifecycle.snapshot()
    if snapshot.condition == "attention-required":
        raise CoordinatorError(
            "attention-required",
            "This Run requires an explicit user decision before it can continue.",
        )
    run_dir = snapshot.manifest_path.parent
    if _guardian_is_alive(run_dir):
        return snapshot
    vault = Path(snapshot.as_dict()["paths"]["vault"])
    snapshot_data = snapshot.as_dict()
    _verify_remote_owner(
        vault,
        snapshot.run_id,
        target_date=str(snapshot_data["target_date"]),
        window_days=snapshot.window_days,
    )
    pending = (
        _discover_pending_dirty_paths(snapshot)
        if pending_dirty_paths is None
        else set(pending_dirty_paths)
    )
    resumed = _resume_lifecycle(
        lifecycle,
        vault,
        pending_dirty_paths=pending,
    )
    _spawn_guardian(
        run_dir,
        vault=vault,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    return resumed


def start(
    *,
    harness: str,
    target_date: str | None = None,
    window_days: int = 1,
    idle_timeout_seconds: float | None = DEFAULT_GUARDIAN_IDLE_TIMEOUT,
    confirm_attention_run_id: str | None = None,
    confirm_running_run_id: str | None = None,
) -> dict[str, Any]:
    """Start, resume, skip, or request cancellation for one DailyPaper Run."""
    if harness not in {"claude-code", "codex"}:
        raise CoordinatorError("invalid-harness", f"Unsupported Harness: {harness}")
    requested_window_days = _window_days(window_days)
    context, vault, date, inspected, preparation = _prepare_start_runtime(
        target_date
    )
    state = inspected["task_state"]
    state_window_days = int(state.get("window_days", 1)) if state else None

    if (
        state
        and state.get("target_date") == date
        and state.get("status") in REMOTE_RUNNING | REMOTE_PUBLISHED
        and state_window_days != requested_window_days
    ):
        return _decision(
            "intent-conflict",
            code="intent-conflict",
            message=(
                f"Existing Run {state.get('run_id')} for {date} uses "
                f"window_days={state_window_days}; the current request uses "
                f"window_days={requested_window_days}."
            ),
            requested_intent={
                "target_date": date,
                "window_days": requested_window_days,
            },
            existing_intent={
                "target_date": state.get("target_date"),
                "window_days": state_window_days,
            },
            run_id=state.get("run_id"),
        )

    if (
        state
        and state.get("target_date") == date
        and state.get("status") in REMOTE_PUBLISHED
    ):
        published_run_id = str(state.get("run_id") or "")
        local_manifest = (
            _manifest_path(vault, published_run_id)
            if published_run_id
            else None
        )
        local_finalization: dict[str, Any] | None = None
        if local_manifest is not None and local_manifest.exists():
            try:
                if context is None:
                    raise CoordinatorError(
                        "invalid-runtime-context",
                        "Local finalization requires the validated runtime context.",
                    )
                lifecycle = _open_lifecycle(
                    local_manifest,
                    vault=vault,
                    run_id=published_run_id,
                )
                snapshot = lifecycle.snapshot()
                if snapshot.window_days != state_window_days:
                    return _decision(
                        "intent-conflict",
                        code="intent-conflict",
                        message=(
                            "The published remote Task State and local Run Manifest "
                            "use different acquisition windows."
                        ),
                        requested_intent={
                            "target_date": date,
                            "window_days": requested_window_days,
                        },
                        existing_intent={
                            "target_date": state.get("target_date"),
                            "window_days": state_window_days,
                        },
                        manifest_intent={
                            "target_date": snapshot.as_dict()["target_date"],
                            "window_days": snapshot.window_days,
                        },
                        run_id=published_run_id,
                    )
                content_commit = snapshot.as_dict()["publication"].get(
                    "content_commit"
                )
                if snapshot.outcome is None and content_commit:
                    lifecycle.finish(
                        "published",
                        content_commit=str(content_commit),
                    )
                elif snapshot.outcome is None:
                    raise CoordinatorError(
                        "missing-content-commit",
                        "Remote is published but the local Run has no recorded "
                        "content commit.",
                    )
                _stop_guardian(local_manifest.parent)
                local_finalization = {"status": "finalized"}
            except (CoordinatorError, LifecycleError, OSError, ValueError) as exc:
                local_finalization = {
                    "status": "preserved",
                    "message": str(exc),
                }
        return _decision(
            "already-published",
            target_date=date,
            window_days=state_window_days,
            run_id=published_run_id or None,
            outputs=state.get("outputs", {}),
            local_finalization=local_finalization,
        )

    if state and state.get("status") in REMOTE_RUNNING:
        run_id = str(state.get("run_id", ""))
        manifest = _manifest_path(vault, run_id)
        summary = {
            "run_id": run_id,
            "target_date": state.get("target_date"),
            "window_days": state_window_days,
            "harness": state.get("harness"),
            "owner": state.get("owner"),
            "started_at": state.get("started_at"),
            "updated_at": state.get("updated_at"),
        }
        if not manifest.parent.is_dir():
            proposal = _prepare_cancel(vault, expected_run_id=run_id)
            return _decision(
                "cancel-confirmation-required",
                run=summary,
                proposal=proposal,
            )
        if not manifest.exists():
            proposal = _prepare_cancel(vault, expected_run_id=run_id)
            return _decision(
                "cancel-confirmation-required",
                run=summary,
                problem=(
                    "Local Run directory exists but its Manifest is missing; "
                    "artifacts were preserved."
                ),
                proposal=proposal,
            )

        try:
            if context is None:
                raise CoordinatorError(
                    "invalid-runtime-context",
                    "Same-machine recovery requires the validated runtime context.",
                )
            lifecycle = _open_lifecycle(
                manifest,
                vault=vault,
                run_id=run_id,
            )
            snapshot = lifecycle.snapshot()
            if (
                snapshot.window_days != requested_window_days
                or snapshot.window_days != state_window_days
            ):
                return _decision(
                    "intent-conflict",
                    code="intent-conflict",
                    message=(
                        "The requested, remote, and local Run acquisition windows "
                        "do not match."
                    ),
                    requested_intent={
                        "target_date": date,
                        "window_days": requested_window_days,
                    },
                    existing_intent={
                        "target_date": state.get("target_date"),
                        "window_days": state_window_days,
                    },
                    manifest_intent={
                        "target_date": snapshot.as_dict()["target_date"],
                        "window_days": snapshot.window_days,
                    },
                    run_id=run_id,
                )
            if _guardian_is_alive(manifest.parent):
                if confirm_running_run_id != run_id:
                    if confirm_running_run_id is not None:
                        raise CoordinatorError(
                            "confirmation-required",
                            (
                                "Stopping a live Run guardian requires the exact "
                                f"run ID: {run_id}"
                            ),
                        )
                    return _decision(
                        "still-running",
                        run=summary,
                        manifest=str(manifest),
                        confirmation_run_id=run_id,
                        message=(
                            "This Run still owns the Vault writer lock. Show the "
                            "exact run ID to the user and obtain explicit "
                            "confirmation before stopping its guardian."
                        ),
                    )
                _stop_guardian(manifest.parent)
            context_file = _freeze_runtime_context(manifest, context)
            if snapshot.outcome is not None:
                return _decision(
                    "blocked",
                    code="remote-local-conflict",
                    message="Remote ownership is running but local Run is terminal.",
                    **_snapshot_fields(snapshot),
                )
            state_fingerprint = state.get("config_sha256")
            if (
                state_fingerprint is not None
                and state_fingerprint != context["configuration_fingerprint"]
            ):
                raise CoordinatorError(
                    "config-conflict",
                    "Remote ownership uses a different configuration fingerprint.",
                )
            acquisition_commit = snapshot.as_dict()["publication"].get(
                "acquisition_commit"
            )
            remote_head = inspected.get("remote_head")
            if acquisition_commit and remote_head and acquisition_commit != remote_head:
                raise CoordinatorError(
                    "remote-advanced",
                    (
                        f"Remote moved from acquisition commit {acquisition_commit} "
                        f"to {remote_head}."
                    ),
                )
            if snapshot.phase == "prepared" and acquisition_commit is None:
                acquisition = vault_coordination.acquire(
                    manifest,
                    harness=harness,
                    expected_remote_head=remote_head,
                    runtime_context=context,
                    record_manifest=False,
                )
                lifecycle.record_acquisition(
                    acquisition_commit=str(acquisition["lock_commit"]),
                    remote=str(acquisition["remote"]),
                    branch=str(acquisition["branch"]),
                )
                snapshot = lifecycle.advance("fetching")
            if snapshot.condition == "attention-required":
                if confirm_attention_run_id != run_id:
                    return _decision(
                        "attention-required",
                        confirmation_run_id=run_id,
                        **_snapshot_fields(snapshot),
                    )
            resumed = _resume_lifecycle(
                lifecycle,
                vault,
                require_user_confirmation=confirm_attention_run_id == run_id,
                pending_dirty_paths=_discover_pending_dirty_paths(snapshot),
            )
            _spawn_guardian(
                manifest.parent,
                vault=vault,
                idle_timeout_seconds=idle_timeout_seconds,
            )
            return _decision(
                "ready",
                mode="resumed",
                runtime_context=context,
                runtime_context_file=str(context_file),
                vault_preparation=preparation,
                **_snapshot_fields(resumed),
            )
        except (LifecycleError, CoordinatorError, OSError, ValueError) as exc:
            return _decision(
                "blocked",
                code=(
                    exc.code
                    if isinstance(exc, CoordinatorError)
                    else "resume-validation-failed"
                ),
                message=str(exc),
                manifest=str(manifest),
                run_id=run_id,
            )

    if context is None:
        raise CoordinatorError(
            "invalid-runtime-context",
            "A fresh Run requires the validated runtime context.",
        )
    timezone = str(context["runtime"]["timezone"])
    run_id = f"{date}-{uuid4().hex[:12]}"
    manifest = _manifest_path(vault, run_id)
    lifecycle = RunLifecycle.create(
        manifest,
        run_id=run_id,
        target_date=date,
        window_days=requested_window_days,
        timezone=timezone,
        vault=vault,
        contract=WORKFLOW_CONTRACT,
        configuration_fingerprint=str(context["configuration_fingerprint"]),
    )
    try:
        context_file = _freeze_runtime_context(manifest, context)
        acquisition = vault_coordination.acquire(
            manifest,
            harness=harness,
            expected_remote_head=inspected.get("remote_head"),
            runtime_context=context,
            record_manifest=False,
        )
        if acquisition.get("status") == "already-completed":
            _stop_guardian(manifest.parent)
            return _decision(
                "already-published",
                target_date=date,
                window_days=requested_window_days,
                outputs={"daily_note": acquisition.get("daily_output")},
            )
        if acquisition.get("status") != "acquired":
            raise CoordinatorError(
                "acquisition-failed",
                f"Unexpected acquisition result: {acquisition.get('status')}",
            )
        repository = context["repository"]
        lifecycle.record_acquisition(
            acquisition_commit=str(acquisition["lock_commit"]),
            remote=str(acquisition.get("remote", repository["remote"])),
            branch=str(acquisition.get("branch", repository["branch"])),
        )
        snapshot = lifecycle.advance("fetching")
        _spawn_guardian(
            manifest.parent,
            vault=vault,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        return _decision(
            "ready",
            mode="started",
            runtime_context=context,
            runtime_context_file=str(context_file),
            vault_preparation=preparation,
            **_snapshot_fields(snapshot),
        )
    except Exception:
        _stop_guardian(manifest.parent)
        raise


def submit(
    manifest: Path,
    *,
    result: str | None = None,
    message: str | None = None,
    retry_at: str | None = None,
    artifacts: Iterable[ArtifactCandidate] = (),
    changed_paths: Iterable[Path | str] = (),
    report: Path | None = None,
    idle_timeout_seconds: float | None = DEFAULT_GUARDIAN_IDLE_TIMEOUT,
) -> dict[str, Any]:
    """Submit one parent-Harness phase result; callers cannot choose a phase."""
    lifecycle = _open_lifecycle(manifest)
    initial_snapshot = lifecycle.snapshot()
    data = initial_snapshot.as_dict()
    vault = Path(data["paths"]["vault"])
    direct_artifacts = tuple(artifacts)
    direct_changes = tuple(changed_paths)
    submission: stage_report.StageSubmission | None = None

    if report is not None:
        if (
            result is not None
            or message is not None
            or retry_at is not None
            or direct_artifacts
            or direct_changes
        ):
            raise CoordinatorError(
                "mixed-submission",
                "Use either --report or direct result arguments, not both.",
            )
        submission = stage_report.load_stage_report(
            report,
            phase=initial_snapshot.phase,
            run_dir=Path(data["paths"]["run_dir"]),
            vault=vault,
        )
        result = submission.result
        message = submission.message
        retry_at = submission.retry_at
        artifact_candidates = submission.artifacts
        change_candidates = submission.changed_paths
        submission.verify_unchanged()
    else:
        artifact_candidates = direct_artifacts
        change_candidates = direct_changes

    if result is None:
        raise CoordinatorError(
            "missing-result",
            "Submit requires either a stage report or a direct result.",
        )
    pending_dirty = _backed_pending_dirty_paths(
        vault=vault,
        artifacts=artifact_candidates,
        changed_paths=change_candidates,
    )
    snapshot = _ensure_guardian(
        lifecycle,
        idle_timeout_seconds=idle_timeout_seconds,
        pending_dirty_paths=pending_dirty,
    )
    if snapshot.phase != initial_snapshot.phase:
        raise CoordinatorError(
            "phase-changed",
            "Run phase changed while the stage report was being prepared.",
        )
    if submission is not None:
        submission.verify_unchanged()
    snapshot_data = snapshot.as_dict()
    _verify_remote_owner(
        vault,
        snapshot.run_id,
        target_date=str(snapshot_data["target_date"]),
        window_days=snapshot.window_days,
    )

    if result == "progress":
        updated = lifecycle.checkpoint(
            artifacts=artifact_candidates,
            changed_paths=change_candidates,
            allow_artifact_updates=True,
            enforce_contract=False,
        )
        return _decision("ready", mode="checkpointed", **_snapshot_fields(updated))
    if result == "recoverable":
        lifecycle.checkpoint(
            artifacts=artifact_candidates,
            changed_paths=change_candidates,
            allow_artifact_updates=True,
            enforce_contract=False,
        )
        updated = lifecycle.interrupt(
            Interruption(message=message or "Recoverable interruption", retry_at=retry_at)
        )
        _stop_guardian(manifest.parent)
        return _decision("interrupted", **_snapshot_fields(updated))
    if result == "attention":
        lifecycle.checkpoint(
            artifacts=artifact_candidates,
            changed_paths=change_candidates,
            allow_artifact_updates=True,
            enforce_contract=False,
        )
        updated = lifecycle.interrupt(
            Interruption(
                message=message or "User attention is required",
                retry_at=retry_at,
                attention_required=True,
            )
        )
        _stop_guardian(manifest.parent)
        return _decision("attention-required", **_snapshot_fields(updated))
    if result == "deterministic-failure":
        reason = message or "Deterministic failure"
        lifecycle.checkpoint(
            artifacts=artifact_candidates,
            changed_paths=change_candidates,
            allow_artifact_updates=True,
            enforce_contract=False,
        )
        failure = vault_coordination.fail(manifest, message=reason)
        updated = lifecycle.snapshot()
        if updated.outcome is None:
            updated = lifecycle.finish("failed", reason=reason)
        elif updated.outcome != "failed":
            raise CoordinatorError(
                "failure-outcome-conflict",
                f"Failure publication produced local outcome {updated.outcome!r}.",
            )
        _stop_guardian(manifest.parent)
        return _decision(
            "failed",
            failure_commit=failure.get("failure_commit"),
            **_snapshot_fields(updated),
        )
    if result != "success":
        raise CoordinatorError("invalid-result", f"Unknown phase result: {result}")

    snapshot = lifecycle.checkpoint(
        artifacts=artifact_candidates,
        changed_paths=change_candidates,
        allow_artifact_updates=True,
    )
    phase = snapshot.phase
    phases = WORKFLOW_CONTRACT.phases
    if phase not in {"validated", "publishing"}:
        snapshot = lifecycle.advance(phases[phases.index(phase) + 1])

    if snapshot.phase == "validated":
        lifecycle.checkpoint(
            artifacts=artifact_candidates,
            changed_paths=change_candidates,
            allow_artifact_updates=True,
        )
        snapshot = lifecycle.advance("publishing")

    if snapshot.phase != "publishing":
        return _decision("ready", mode="continued", **_snapshot_fields(snapshot))

    try:
        publication = vault_coordination.complete(manifest)
    except vault_coordination.CoordinationError:
        interrupted = lifecycle.snapshot()
        if interrupted.condition in {"interrupted", "attention-required"}:
            _stop_guardian(manifest.parent)
        raise
    if publication.get("status") not in {"success", "published"}:
        raise CoordinatorError(
            "publication-failed",
            f"Unexpected publication result: {publication.get('status')}",
        )
    content_commit = str(publication["content_commit"])
    published = lifecycle.snapshot()
    if published.outcome is None:
        lifecycle.record_content_commit(content_commit)
        published = lifecycle.finish("published", content_commit=content_commit)
    elif published.outcome != "published":
        raise CoordinatorError(
            "publication-outcome-conflict",
            f"Publication produced local outcome {published.outcome!r}.",
        )
    _stop_guardian(manifest.parent)
    return _decision(
        "published",
        changed_paths=publication.get("changed_paths", []),
        **_snapshot_fields(published),
    )


def inspect(manifest: Path) -> dict[str, Any]:
    lifecycle = _open_lifecycle(manifest)
    snapshot = lifecycle.snapshot()
    data = snapshot.as_dict()
    run_dir = snapshot.manifest_path.parent
    guardian: dict[str, Any] | None
    try:
        guardian = run_guardian.guardian_status(run_dir)
    except run_guardian.GuardianError:
        guardian = None
    return _decision(
        "inspection",
        **_snapshot_fields(snapshot),
        guardian=guardian,
        task_state=_inspect_task_state(Path(data["paths"]["vault"])),
        recovery=data["recovery"],
        publication=data["publication"],
        run_change_set=data["run_change_set"],
    )


def cancel(proposal: dict[str, Any]) -> dict[str, Any]:
    """Apply one user-confirmed Vault CAS cancellation and preserve artifacts."""
    result = _cancel_vault(proposal)
    proposal_state = proposal.get("task_state")
    nested_run_id = (
        proposal_state.get("run_id")
        if isinstance(proposal_state, dict)
        else None
    )
    run_id = str(
        proposal.get("expected_run_id")
        or proposal.get("run_id")
        or nested_run_id
        or result.get("run_id")
        or ""
    )
    vault_value = proposal.get("vault") or result.get("vault")
    local_updated = False
    local_problem: str | None = None
    manifest: Path | None = None
    if run_id and vault_value:
        manifest = _manifest_path(Path(str(vault_value)).expanduser().resolve(), run_id)
        if manifest.exists():
            try:
                data = load_json_object(
                    manifest,
                    max_bytes=MAX_MANIFEST_BYTES,
                    label="Run Manifest",
                )
                if data is None:
                    raise SafeIOError(f"Run Manifest file does not exist: {manifest}")
                lifecycle = RunLifecycle.open(
                    manifest,
                    contract=WORKFLOW_CONTRACT,
                    configuration_fingerprint=str(data["configuration_fingerprint"]),
                    expected_vault=Path(str(vault_value)).expanduser().resolve(),
                    expected_run_id=run_id,
                )
                if lifecycle.snapshot().outcome is None:
                    lifecycle.finish(
                        "cancelled",
                        reason=str(result.get("message") or "Cancelled by user"),
                    )
                _stop_guardian(manifest.parent)
                local_updated = True
            except (LifecycleError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                local_problem = str(exc)
    return _decision(
        "cancelled",
        run_id=run_id,
        cancellation_commit=result.get("cancellation_commit"),
        local_manifest=str(manifest) if manifest is not None else None,
        local_manifest_updated=local_updated,
        local_problem=local_problem,
    )


def _parse_artifact(value: str) -> ArtifactCandidate:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Artifact must use ROLE=PATH")
    role, path = value.split("=", 1)
    if not role or not path:
        raise argparse.ArgumentTypeError("Artifact must use non-empty ROLE=PATH")
    return ArtifactCandidate(role=role, path=Path(path))


def _proposal_json(value: str) -> dict[str, Any]:
    try:
        if value.startswith("@"):
            proposal = load_json_object(
                Path(value[1:]),
                max_bytes=MAX_PROPOSAL_BYTES,
                label="Cancellation proposal",
            )
            if proposal is None:
                raise SafeIOError(
                    f"Cancellation proposal file does not exist: {value[1:]}"
                )
            return proposal
        return parse_json_object(
            value.encode("utf-8"),
            max_bytes=MAX_PROPOSAL_BYTES,
            label="Cancellation proposal",
        )
    except (SafeIOError, UnicodeEncodeError) as exc:
        raise CoordinatorError("invalid-proposal", str(exc)) from exc


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument(
        "--harness",
        required=True,
        choices=("claude-code", "codex"),
    )
    start_parser.add_argument("--date")
    start_parser.add_argument("--window-days", type=int, default=1)
    start_parser.add_argument("--confirm-attention-run-id")
    start_parser.add_argument("--confirm-running-run-id")

    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("manifest", type=Path)
    result_group = submit_parser.add_mutually_exclusive_group(required=True)
    result_group.add_argument("--report", type=Path)
    result_group.add_argument(
        "--result",
        choices=(
            "progress",
            "success",
            "recoverable",
            "attention",
            "deterministic-failure",
        ),
    )
    submit_parser.add_argument("--message")
    submit_parser.add_argument("--retry-at")
    submit_parser.add_argument("--artifact", action="append", default=[], type=_parse_artifact)
    submit_parser.add_argument("--changed-path", action="append", default=[], type=Path)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("manifest", type=Path)

    cancel_parser = subparsers.add_parser("cancel")
    cancel_parser.add_argument("proposal_json")

    args = parser.parse_args()
    try:
        if args.command == "start":
            response = start(
                harness=args.harness,
                target_date=args.date,
                window_days=args.window_days,
                confirm_attention_run_id=args.confirm_attention_run_id,
                confirm_running_run_id=args.confirm_running_run_id,
            )
        elif args.command == "submit":
            response = submit(
                args.manifest,
                result=args.result,
                message=args.message,
                retry_at=args.retry_at,
                artifacts=args.artifact,
                changed_paths=args.changed_path,
                report=args.report,
            )
        elif args.command == "inspect":
            response = inspect(args.manifest)
        else:
            response = cancel(_proposal_json(args.proposal_json))
        _print_json(response)
    except (
        CoordinatorError,
        LifecycleError,
        stage_report.StageReportError,
        run_guardian.GuardianError,
        vault_coordination.CoordinationError,
    ) as exc:
        _print_json(
            _decision(
                "blocked",
                code=getattr(exc, "code", type(exc).__name__),
                message=str(exc),
            )
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
