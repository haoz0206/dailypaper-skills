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
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import run_guardian
import vault_coordination
from run_lifecycle import (
    DAILY_WORKFLOW_CONTRACT,
    ArtifactCandidate,
    Interruption,
    LifecycleError,
    RunLifecycle,
    RunSnapshot,
)
from user_config import obsidian_vault_path, repository_config, timezone_name


WORKFLOW_CONTRACT = DAILY_WORKFLOW_CONTRACT
DEFAULT_GUARDIAN_IDLE_TIMEOUT = 3600.0
GUARDIAN_READY_TIMEOUT = 5.0
REMOTE_RUNNING = frozenset({"running"})
REMOTE_PUBLISHED = frozenset({"success", "published"})


class CoordinatorError(RuntimeError):
    """A safe, expected Run Coordinator failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _configuration_fingerprint() -> str:
    public = getattr(vault_coordination, "configuration_fingerprint", None)
    return str(public() if public is not None else vault_coordination._config_fingerprint())


def _dirty_paths(vault: Path) -> set[str]:
    public = getattr(vault_coordination, "dirty_paths", None)
    values = public(vault) if public is not None else vault_coordination._dirty_paths(vault)
    return {str(value) for value in values}


def _inspect_task_state(vault: Path) -> dict[str, Any] | None:
    """Read current remote-backed task state, with legacy private compatibility."""
    public = getattr(vault_coordination, "inspect_task_state", None)
    if public is not None:
        result = public(vault)
        if result is None:
            return None
        if isinstance(result, dict) and "task_state" in result:
            state = result["task_state"]
            return state if isinstance(state, dict) else None
        if isinstance(result, dict) and "state" in result:
            state = result["state"]
            return state if isinstance(state, dict) else None
        return result if isinstance(result, dict) else None

    config, remote = vault_coordination._repository_identity(vault)
    vault_coordination._sync_before_run(vault, config, remote)
    task_path = vault / vault_coordination._task_relative_path()
    return vault_coordination._read_state(task_path)


def _prepare_cancel(
    vault: Path,
    *,
    expected_run_id: str,
) -> dict[str, Any]:
    public = getattr(vault_coordination, "prepare_cancel", None)
    if public is not None:
        return dict(public(vault, expected_run_id=expected_run_id))
    state = _inspect_task_state(vault)
    if (
        not state
        or state.get("status") != "running"
        or state.get("run_id") != expected_run_id
    ):
        raise CoordinatorError(
            "stale-cancellation",
            "Vault Task State changed before cancellation could be proposed.",
        )
    return {
        "version": 1,
        "task": state.get("task", "daily-papers"),
        "run_id": expected_run_id,
        "target_date": state.get("target_date"),
        "harness": state.get("harness"),
        "owner": state.get("owner"),
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "vault": str(vault),
    }


def _cancel_vault(proposal: dict[str, Any]) -> dict[str, Any]:
    public = getattr(vault_coordination, "cancel", None)
    if public is None:
        raise CoordinatorError(
            "cancel-unavailable",
            "This vault_coordination version does not support CAS cancellation.",
        )
    return dict(public(proposal))


def _target_date(value: str | None, timezone: str) -> str:
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    return datetime.now(ZoneInfo(timezone)).date().isoformat()


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
        "manifest": str(snapshot.manifest_path),
        "phase": snapshot.phase,
        "condition": snapshot.condition,
        "outcome": snapshot.outcome,
        "revision": snapshot.revision,
    }


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
    idle_timeout_seconds: float,
) -> None:
    subprocess.Popen(
        [
            sys.executable,
            str(Path(run_guardian.__file__).resolve()),
            "serve",
            str(run_dir),
            "--idle-timeout",
            str(idle_timeout_seconds),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + GUARDIAN_READY_TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            run_guardian.probe_guardian(run_dir, timeout=0.1)
            return
        except run_guardian.GuardianError as exc:
            last_error = exc
            time.sleep(0.02)
    raise CoordinatorError(
        "guardian-unavailable",
        f"Run guardian did not become ready: {last_error}",
    )


def _stop_guardian(run_dir: Path) -> None:
    try:
        run_guardian.stop_guardian(run_dir)
    except run_guardian.GuardianError:
        pass


def _open_lifecycle(
    manifest: Path,
    *,
    vault: Path | None = None,
    run_id: str | None = None,
) -> RunLifecycle:
    expected_vault = (
        vault.expanduser().resolve()
        if vault is not None
        else obsidian_vault_path().expanduser().resolve()
    )
    return RunLifecycle.open(
        manifest,
        contract=WORKFLOW_CONTRACT,
        configuration_fingerprint=_configuration_fingerprint(),
        expected_vault=expected_vault,
        expected_run_id=run_id or manifest.expanduser().resolve().parent.name,
    )


def _verify_remote_owner(vault: Path, run_id: str) -> dict[str, Any]:
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
    return state


def _resume_lifecycle(
    lifecycle: RunLifecycle,
    vault: Path,
    *,
    require_user_confirmation: bool = False,
) -> RunSnapshot:
    snapshot = lifecycle.snapshot()
    observed_dirty = _dirty_paths(vault)
    unexpected = observed_dirty - set(snapshot.run_change_set)
    if unexpected:
        raise CoordinatorError(
            "unexpected-dirty-paths",
            "Vault contains changes outside this Run Change Set: "
            + ", ".join(sorted(unexpected)),
        )
    return lifecycle.resume(
        observed_dirty_paths=observed_dirty,
        require_user_confirmation=require_user_confirmation,
    )


def _ensure_guardian(
    lifecycle: RunLifecycle,
    *,
    idle_timeout_seconds: float,
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
    _verify_remote_owner(vault, snapshot.run_id)
    resumed = _resume_lifecycle(lifecycle, vault)
    _spawn_guardian(run_dir, idle_timeout_seconds=idle_timeout_seconds)
    return resumed


def start(
    *,
    harness: str,
    target_date: str | None = None,
    idle_timeout_seconds: float = DEFAULT_GUARDIAN_IDLE_TIMEOUT,
    confirm_attention_run_id: str | None = None,
) -> dict[str, Any]:
    """Start, resume, skip, or request cancellation for one DailyPaper Run."""
    if harness not in {"claude-code", "codex"}:
        raise CoordinatorError("invalid-harness", f"Unsupported Harness: {harness}")
    vault = obsidian_vault_path().expanduser().resolve()
    timezone = timezone_name()
    date = _target_date(target_date, timezone)
    state = _inspect_task_state(vault)

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
                lifecycle = _open_lifecycle(
                    local_manifest,
                    vault=vault,
                    run_id=published_run_id,
                )
                snapshot = lifecycle.snapshot()
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
            return _decision(
                "attention-required",
                run=summary,
                problem="Local Run directory exists but its Manifest is missing.",
            )
        if _guardian_is_alive(manifest.parent):
            return _decision("still-running", run=summary, manifest=str(manifest))

        try:
            lifecycle = _open_lifecycle(
                manifest,
                vault=vault,
                run_id=run_id,
            )
            snapshot = lifecycle.snapshot()
            if snapshot.outcome is not None:
                return _decision(
                    "blocked",
                    code="remote-local-conflict",
                    message="Remote ownership is running but local Run is terminal.",
                    **_snapshot_fields(snapshot),
                )
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
            )
            _spawn_guardian(
                manifest.parent,
                idle_timeout_seconds=idle_timeout_seconds,
            )
            return _decision("ready", mode="resumed", **_snapshot_fields(resumed))
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

    run_id = f"{date}-{uuid4().hex[:12]}"
    manifest = _manifest_path(vault, run_id)
    lifecycle = RunLifecycle.create(
        manifest,
        run_id=run_id,
        target_date=date,
        timezone=timezone,
        vault=vault,
        contract=WORKFLOW_CONTRACT,
        configuration_fingerprint=_configuration_fingerprint(),
    )
    try:
        _spawn_guardian(
            manifest.parent,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        acquisition = vault_coordination.acquire(manifest, harness=harness)
        if acquisition.get("status") == "already-completed":
            _stop_guardian(manifest.parent)
            return _decision(
                "already-published",
                target_date=date,
                outputs={"daily_note": acquisition.get("daily_output")},
            )
        if acquisition.get("status") != "acquired":
            raise CoordinatorError(
                "acquisition-failed",
                f"Unexpected acquisition result: {acquisition.get('status')}",
            )
        repository = repository_config()
        lifecycle.record_acquisition(
            acquisition_commit=str(acquisition["lock_commit"]),
            remote=str(acquisition.get("remote", repository["remote"])),
            branch=str(acquisition.get("branch", repository["branch"])),
        )
        snapshot = lifecycle.advance("fetching")
        return _decision("ready", mode="started", **_snapshot_fields(snapshot))
    except Exception:
        _stop_guardian(manifest.parent)
        raise


def submit(
    manifest: Path,
    *,
    result: str,
    message: str | None = None,
    retry_at: str | None = None,
    artifacts: Iterable[ArtifactCandidate] = (),
    changed_paths: Iterable[Path | str] = (),
    idle_timeout_seconds: float = DEFAULT_GUARDIAN_IDLE_TIMEOUT,
) -> dict[str, Any]:
    """Submit one parent-Harness phase result; callers cannot choose a phase."""
    artifact_candidates = tuple(artifacts)
    change_candidates = tuple(changed_paths)
    lifecycle = _open_lifecycle(manifest)
    snapshot = _ensure_guardian(
        lifecycle,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    data = snapshot.as_dict()
    vault = Path(data["paths"]["vault"])
    _verify_remote_owner(vault, snapshot.run_id)

    if result == "progress":
        updated = lifecycle.checkpoint(
            artifacts=artifact_candidates,
            changed_paths=change_candidates,
            allow_artifact_updates=True,
            enforce_contract=False,
        )
        return _decision("ready", mode="checkpointed", **_snapshot_fields(updated))
    if result == "recoverable":
        updated = lifecycle.interrupt(
            Interruption(message=message or "Recoverable interruption", retry_at=retry_at)
        )
        _stop_guardian(manifest.parent)
        return _decision("interrupted", **_snapshot_fields(updated))
    if result == "attention":
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
                data = json.loads(manifest.read_text(encoding="utf-8"))
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
    if value.startswith("@"):
        text = Path(value[1:]).read_text(encoding="utf-8")
    else:
        text = value
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Cancellation proposal must be a JSON object")
    return data


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
    start_parser.add_argument("--confirm-attention-run-id")
    start_parser.add_argument(
        "--guardian-idle-timeout",
        type=float,
        default=DEFAULT_GUARDIAN_IDLE_TIMEOUT,
    )

    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("manifest", type=Path)
    submit_parser.add_argument(
        "--result",
        required=True,
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
    submit_parser.add_argument(
        "--guardian-idle-timeout",
        type=float,
        default=DEFAULT_GUARDIAN_IDLE_TIMEOUT,
    )

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
                idle_timeout_seconds=args.guardian_idle_timeout,
                confirm_attention_run_id=args.confirm_attention_run_id,
            )
        elif args.command == "submit":
            response = submit(
                args.manifest,
                result=args.result,
                message=args.message,
                retry_at=args.retry_at,
                artifacts=args.artifact,
                changed_paths=args.changed_path,
                idle_timeout_seconds=args.guardian_idle_timeout,
            )
        elif args.command == "inspect":
            response = inspect(args.manifest)
        else:
            response = cancel(_proposal_json(args.proposal_json))
        _print_json(response)
    except (
        CoordinatorError,
        LifecycleError,
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
