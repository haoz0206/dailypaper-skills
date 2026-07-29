#!/usr/bin/env python3
"""Crash-resumable coordination for standalone Vault-writing Skills."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import active_run_guard
import config_schema
import run_guardian
import runtime_context
from safe_io import (
    DocumentTooLargeError,
    SafeIOError,
    anchored_file_path,
    atomic_write_json,
    load_json_object,
    sha256_regular_file,
)
from safe_git import (
    GitCommandResult,
    SafeGitError,
    read_git_blob,
    repository_dirty_paths,
    run_git_command,
    verify_index_versions,
)
from safe_path import SafePathError, relative_posix_path


SESSION_VERSION = 1
REPORT_VERSION = 1
MAX_STATE_BYTES = 1024 * 1024
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_STANDALONE_SESSION_FILES = 4096
OPERATIONS = frozenset({"paper-reader", "generate-mocs"})
HARNESSES = frozenset({"claude-code", "codex"})
REPORT_RESULTS = frozenset(
    {"progress", "success", "recoverable", "attention-required"}
)
TERMINAL_OUTCOMES = frozenset(
    {"completed-local", "published", "unchanged", "cancelled"}
)
SESSION_ID_PATTERN = re.compile(
    r"^standalone-(?:paper-reader|generate-mocs)-[0-9a-f]{16}$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_NAME = "dailypaper automation"
COMMIT_EMAIL = "dailypaper@localhost"


class StandaloneError(RuntimeError):
    """A safe standalone-session coordination failure."""

    def __init__(self, code: str, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        return {"status": "blocked", "code": self.code, "message": str(self)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(
    vault: Path,
    *args: str,
    check: bool = True,
) -> GitCommandResult:
    try:
        result = run_git_command(vault, *args)
    except SafeGitError as exc:
        raise StandaloneError("git-error", str(exc)) from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise StandaloneError(
            "git-error",
            f"git {' '.join(args)} failed: {detail}",
        )
    return result


def _git_output(vault: Path, *args: str) -> str:
    return _git(vault, *args).stdout.strip()


def _git_blob(vault: Path, object_name: str) -> bytes | None:
    try:
        return read_git_blob(
            vault,
            object_name,
            max_bytes=MAX_ARTIFACT_BYTES,
        )
    except SafeGitError as exc:
        raise StandaloneError("git-error", str(exc)) from exc


def _read_json(path: Path, *, limit: int, label: str) -> dict[str, Any]:
    try:
        value = load_json_object(
            path,
            max_bytes=limit,
            label=label,
        )
        if value is None:  # Defensive: required=True must already reject this state.
            raise SafeIOError(f"{label} file does not exist: {path}")
    except SafeIOError as exc:
        raise StandaloneError("invalid-json", str(exc)) from exc
    return value


def _atomic_json(path: Path, value: dict[str, Any], *, limit: int) -> None:
    try:
        atomic_write_json(
            path,
            value,
            max_bytes=limit,
            mode=0o600,
            label="Standalone state",
        )
    except DocumentTooLargeError as exc:
        raise StandaloneError(
            "state-too-large",
            f"Standalone state exceeds {limit} bytes",
        ) from exc
    except SafeIOError as exc:
        raise StandaloneError("state-write-failed", str(exc)) from exc


def _safe_relative(value: Any, *, field: str) -> str:
    try:
        pure = relative_posix_path(value, label=field)
    except SafePathError as exc:
        raise StandaloneError("invalid-path", str(exc)) from exc
    if pure.parts[0] in {".git", ".dailypaper"}:
        raise StandaloneError("invalid-path", f"Unsafe {field}: {value!r}")
    return pure.as_posix()


def _safe_session_id(value: str) -> str:
    if not SESSION_ID_PATTERN.fullmatch(value):
        raise StandaloneError("invalid-session-id", f"Invalid session ID: {value!r}")
    return value


def _runs_root(vault: Path) -> Path:
    return vault / ".dailypaper" / "runs"


def _configured_vault() -> Path:
    try:
        return runtime_context.resolve_vault_path().expanduser().resolve()
    except (
        config_schema.ConfigurationError,
        runtime_context.MachineConfigError,
        OSError,
    ) as exc:
        raise StandaloneError("invalid-runtime-context", str(exc)) from exc


def _session_dir(vault: Path, session_id: str) -> Path:
    metadata = vault / ".dailypaper"
    runs = metadata / "runs"
    for path in (metadata, runs):
        if path.is_symlink():
            raise StandaloneError(
                "invalid-session-path",
                f"Standalone session directory must not be a symlink: {path}",
            )
        if path.exists() and not path.is_dir():
            raise StandaloneError(
                "invalid-session-path",
                f"Standalone session parent is not a directory: {path}",
            )
    return runs / _safe_session_id(session_id)


def _manifest_path(vault: Path, session_id: str) -> Path:
    return _session_dir(vault, session_id) / "standalone-session.json"


@contextmanager
def _state_lock(vault: Path):
    lock_path = run_guardian.vault_writer_lock_path(vault).parent / "standalone.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise StandaloneError(
            "lock-error",
            f"Standalone coordination lock cannot be opened: {lock_path}",
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise StandaloneError("lock-error", "Standalone lock is not regular")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _dirty_paths(vault: Path) -> set[str]:
    try:
        return repository_dirty_paths(vault)
    except SafeGitError as exc:
        raise StandaloneError("git-error", str(exc)) from exc


def _resolved_artifact(vault: Path, relative: str) -> Path:
    safe = _safe_relative(relative, field="artifact.path")
    current = vault
    for part in PurePosixPath(safe).parts:
        current = current / part
        if current.is_symlink():
            raise StandaloneError(
                "invalid-path",
                f"Artifact path traverses a symlink: {safe}",
            )
    resolved = current.resolve()
    try:
        resolved.relative_to(vault)
    except ValueError as exc:
        raise StandaloneError(
            "invalid-path",
            f"Artifact escapes the Vault: {safe}",
        ) from exc
    return resolved


def _file_sha256(path: Path) -> str:
    try:
        return sha256_regular_file(
            path,
            max_bytes=MAX_ARTIFACT_BYTES,
            label="Standalone artifact",
        )
    except SafeIOError as exc:
        raise StandaloneError(
            "artifact-missing",
            str(exc),
        ) from exc


def _baseline(vault: Path) -> dict[str, str | None]:
    baseline: dict[str, str | None] = {}
    for relative in sorted(_dirty_paths(vault)):
        path = vault / relative
        baseline[relative] = (
            _file_sha256(path)
            if path.exists() and path.is_file() and not path.is_symlink()
            else None
        )
    return baseline


def _validate_manifest(value: dict[str, Any], *, path: Path) -> dict[str, Any]:
    fields = {
        "version",
        "session_id",
        "operation",
        "intent",
        "harness",
        "condition",
        "outcome",
        "started_at",
        "updated_at",
        "vault",
        "runtime_context_file",
        "configuration_fingerprint",
        "base_head",
        "baseline_dirty",
        "artifacts",
        "changed_paths",
        "publication",
        "message",
    }
    if set(value) != fields or value.get("version") != SESSION_VERSION:
        raise StandaloneError("invalid-session", f"Invalid session schema: {path}")
    session_id = _safe_session_id(str(value.get("session_id", "")))
    if path.parent.name != session_id:
        raise StandaloneError("invalid-session", "Session path and ID differ")
    if value.get("operation") not in OPERATIONS or value.get("harness") not in HARNESSES:
        raise StandaloneError("invalid-session", "Invalid operation or Harness")
    if value.get("condition") not in {
        "active",
        "interrupted",
        "attention-required",
    }:
        raise StandaloneError("invalid-session", "Invalid session condition")
    if value.get("outcome") not in {None, *TERMINAL_OUTCOMES}:
        raise StandaloneError("invalid-session", "Invalid session outcome")
    if not isinstance(value.get("intent"), str) or not value["intent"].strip():
        raise StandaloneError("invalid-session", "Session intent must not be empty")
    if len(value["intent"]) > 512 or "\x00" in value["intent"]:
        raise StandaloneError("invalid-session", "Invalid session intent")
    for timestamp_field in ("started_at", "updated_at"):
        timestamp = value.get(timestamp_field)
        if not isinstance(timestamp, str):
            raise StandaloneError("invalid-session", "Invalid session timestamp")
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise StandaloneError(
                "invalid-session",
                f"Invalid session {timestamp_field}",
            ) from exc
        if parsed.tzinfo is None:
            raise StandaloneError(
                "invalid-session",
                f"Session {timestamp_field} must be timezone-aware",
            )
    vault_value = value.get("vault")
    expected_vault = path.parents[3].resolve()
    if (
        not isinstance(vault_value, str)
        or not Path(vault_value).is_absolute()
        or Path(vault_value).expanduser().resolve() != expected_vault
    ):
        raise StandaloneError("invalid-session", "Invalid session Vault path")
    context_file = value.get("runtime_context_file")
    expected_context = path.parent / "runtime-context.json"
    if (
        not isinstance(context_file, str)
        or Path(context_file).expanduser().resolve() != expected_context.resolve()
    ):
        raise StandaloneError("invalid-session", "Invalid frozen context path")
    fingerprint = value.get("configuration_fingerprint")
    if not isinstance(fingerprint, str) or not SHA256_PATTERN.fullmatch(fingerprint):
        raise StandaloneError(
            "invalid-session",
            "Invalid session configuration_fingerprint",
        )
    base_head = value.get("base_head")
    if not isinstance(base_head, str) or not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}",
        base_head,
    ):
        raise StandaloneError("invalid-session", "Invalid session base_head")
    baseline = value.get("baseline_dirty")
    if not isinstance(baseline, dict):
        raise StandaloneError("invalid-session", "Invalid baseline_dirty")
    for relative, digest in baseline.items():
        _safe_relative(relative, field="baseline_dirty")
        if digest is not None and (
            not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest)
        ):
            raise StandaloneError("invalid-session", "Invalid baseline hash")
    artifacts = value.get("artifacts")
    changed_paths = value.get("changed_paths")
    if not isinstance(artifacts, dict) or not isinstance(changed_paths, list):
        raise StandaloneError("invalid-session", "Invalid session artifacts")
    if len(artifacts) > 4096 or len(changed_paths) > 4096:
        raise StandaloneError("invalid-session", "Session change set is too large")
    for relative, artifact in artifacts.items():
        _safe_relative(relative, field="artifacts")
        if not isinstance(artifact, dict) or set(artifact) != {"sha256", "kind"}:
            raise StandaloneError("invalid-session", "Invalid session artifact")
        if (
            not isinstance(artifact["sha256"], str)
            or not SHA256_PATTERN.fullmatch(artifact["sha256"])
            or not isinstance(artifact["kind"], str)
            or not artifact["kind"]
            or len(artifact["kind"]) > 64
        ):
            raise StandaloneError("invalid-session", "Invalid artifact metadata")
    normalized_changed = [
        _safe_relative(relative, field="changed_paths")
        for relative in changed_paths
    ]
    if (
        len(set(normalized_changed)) != len(normalized_changed)
        or set(normalized_changed) != set(artifacts)
    ):
        raise StandaloneError(
            "invalid-session",
            "Session paths must exactly match its artifacts",
        )
    publication = value.get("publication")
    if not isinstance(publication, dict) or set(publication) != {
        "remote",
        "branch",
        "commit",
    }:
        raise StandaloneError("invalid-session", "Invalid publication state")
    commit = publication.get("commit")
    if commit is not None and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise StandaloneError("invalid-session", "Invalid publication commit")
    for field in ("remote", "branch"):
        if not isinstance(publication[field], str) or not publication[field]:
            raise StandaloneError("invalid-session", "Invalid publication target")
    message = value.get("message")
    if message is not None and (
        not isinstance(message, str) or len(message) > 4096
    ):
        raise StandaloneError("invalid-session", "Invalid session message")
    return copy.deepcopy(value)


def _load_manifest(path: Path) -> dict[str, Any]:
    return _validate_manifest(
        _read_json(path, limit=MAX_STATE_BYTES, label="Standalone session"),
        path=path,
    )


def _save_manifest(path: Path, value: dict[str, Any]) -> None:
    value["updated_at"] = _now()
    _validate_manifest(value, path=path)
    _atomic_json(path, value, limit=MAX_STATE_BYTES)


def _active_manifests(vault: Path) -> list[tuple[Path, dict[str, Any]]]:
    root = _runs_root(vault)
    if not root.is_dir():
        return []
    candidates = list(
        islice(
            root.glob("standalone-*/standalone-session.json"),
            MAX_STANDALONE_SESSION_FILES + 1,
        )
    )
    if len(candidates) > MAX_STANDALONE_SESSION_FILES:
        raise StandaloneError(
            "too-many-sessions",
            "Standalone run directory exceeds the "
            f"{MAX_STANDALONE_SESSION_FILES}-session safety limit",
        )
    result = []
    for path in sorted(candidates):
        manifest = _load_manifest(path)
        if manifest["outcome"] is None:
            result.append((path, manifest))
    return result


def _guardian_alive(run_dir: Path) -> bool:
    try:
        run_guardian.probe_guardian(run_dir)
        return True
    except run_guardian.GuardianError:
        return False


def _spawn_guardian(run_dir: Path, vault: Path) -> None:
    try:
        run_guardian.ensure_guardian_running(
            run_dir,
            vault=vault,
        )
    except (run_guardian.GuardianError, OSError, ValueError) as exc:
        raise StandaloneError(
            "writer-busy",
            f"Could not acquire the Vault writer lock: {exc}",
            exit_code=3,
        ) from exc


def _stop_guardian(run_dir: Path) -> None:
    try:
        run_guardian.stop_guardian(run_dir)
    except run_guardian.GuardianError as exc:
        # A missing guardian is already stopped.  A responsive guardian that
        # rejected or failed the stop request must remain non-terminal so the
        # caller can retry without leaving an orphaned Vault writer lock.
        if _guardian_alive(run_dir):
            raise StandaloneError(
                "guardian-stop-failed",
                f"Could not stop the active session guardian: {exc}",
                exit_code=3,
            ) from exc


def _write_context(path: Path, context: dict[str, Any]) -> None:
    if path.exists():
        current = _read_json(path, limit=MAX_STATE_BYTES, label="Runtime context")
        if current != context:
            raise StandaloneError(
                "context-conflict",
                "Frozen runtime context differs from the current configuration",
            )
        return
    _atomic_json(path, context, limit=MAX_STATE_BYTES)


def _remote_guard(context: dict[str, Any], vault: Path) -> dict[str, Any]:
    repository = context["repository"]
    try:
        return active_run_guard.guard_remote_active_run(
            vault,
            repository_url=str(repository["url"]),
            remote=str(repository["remote"]),
            branch=str(repository["branch"]),
            task_state_file=str(repository["task_state_file"]),
            fetch_remote=True,
        )
    except active_run_guard.ActiveRunError as exc:
        raise StandaloneError("active-daily-run", str(exc), exit_code=3) from exc
    except active_run_guard.GuardError as exc:
        raise StandaloneError("remote-guard-failed", str(exc), exit_code=3) from exc


def _context_for_resume(manifest: dict[str, Any]) -> dict[str, Any]:
    local_ahead = manifest["publication"]["commit"] is not None
    try:
        return runtime_context.resolve_runtime_context(
            guard_active_run=local_ahead,
            prepare_standalone=not local_ahead,
        )
    except (
        active_run_guard.GuardError,
        config_schema.ConfigurationError,
        runtime_context.MachineConfigError,
        OSError,
    ) as exc:
        raise StandaloneError("invalid-runtime-context", str(exc)) from exc


def _verify_baseline(vault: Path, manifest: dict[str, Any]) -> None:
    for relative, expected in manifest["baseline_dirty"].items():
        _safe_relative(relative, field="baseline_dirty")
        path = vault / relative
        observed = (
            _file_sha256(path)
            if path.exists() and path.is_file() and not path.is_symlink()
            else None
        )
        if observed != expected:
            raise StandaloneError(
                "baseline-conflict",
                f"Pre-existing user change was modified: {relative}",
            )


def _verify_registered(vault: Path, manifest: dict[str, Any]) -> None:
    for relative, artifact in manifest["artifacts"].items():
        path = _resolved_artifact(vault, relative)
        observed = _file_sha256(path)
        if observed != artifact["sha256"]:
            raise StandaloneError(
                "artifact-conflict",
                f"Registered artifact changed: {relative}",
            )


def start(
    *,
    operation: str,
    harness: str,
    intent: str,
    confirm_running_session_id: str | None = None,
) -> dict[str, Any]:
    if operation not in OPERATIONS or harness not in HARNESSES:
        raise StandaloneError("invalid-request", "Unsupported operation or Harness")
    if not isinstance(intent, str) or not intent.strip() or len(intent) > 512:
        raise StandaloneError("invalid-request", "Intent must be 1-512 characters")
    vault = _configured_vault()
    with _state_lock(vault):
        active = _active_manifests(vault)
        matching = [
            item
            for item in active
            if item[1]["operation"] == operation
            and item[1]["intent"] == intent.strip()
        ]
        if active and not matching:
            other = active[0][1]
            raise StandaloneError(
                "active-session",
                (
                    "Another standalone session must be resumed or explicitly "
                    f"cancelled first: {other['session_id']}"
                ),
                exit_code=3,
            )
        if matching:
            path, manifest = matching[0]
            session_id = manifest["session_id"]
            if (
                confirm_running_session_id is not None
                and confirm_running_session_id != session_id
            ):
                raise StandaloneError(
                    "confirmation-required",
                    (
                        "Stopping a live guardian requires the exact session ID: "
                        f"{session_id}"
                    ),
                    exit_code=3,
                )
            resumed_after_confirmed_stop = False
            if _guardian_alive(path.parent):
                if confirm_running_session_id != session_id:
                    return {
                        "decision": "still-running",
                        "session_id": session_id,
                        "manifest": str(path),
                        "confirmation_session_id": session_id,
                        "message": (
                            "This session still owns the Vault writer lock. Show "
                            "the exact session ID to the user and obtain explicit "
                            "confirmation before stopping its guardian."
                        ),
                    }
                _stop_guardian(path.parent)
                resumed_after_confirmed_stop = True
            elif confirm_running_session_id is not None:
                # The guardian genuinely died between runs. Exact confirmation
                # is harmless, but ordinary crash recovery remains automatic.
                resumed_after_confirmed_stop = False
            if manifest["condition"] == "attention-required":
                return {
                    "decision": "attention-required",
                    "session_id": manifest["session_id"],
                    "manifest": str(path),
                    "message": manifest["message"],
                }
            context = _context_for_resume(manifest)
            if (
                context["configuration_fingerprint"]
                != manifest["configuration_fingerprint"]
            ):
                manifest["condition"] = "attention-required"
                manifest["message"] = "Configuration changed after session start"
                _save_manifest(path, manifest)
                raise StandaloneError(
                    "config-conflict",
                    manifest["message"],
                    exit_code=3,
                )
            _verify_baseline(vault, manifest)
            _verify_registered(vault, manifest)
            known = set(manifest["baseline_dirty"]) | set(manifest["changed_paths"])
            unknown = _dirty_paths(vault) - known
            if unknown:
                manifest["condition"] = "attention-required"
                manifest["message"] = (
                    "Unregistered local changes require an explicit report: "
                    + ", ".join(sorted(unknown))
                )
                _save_manifest(path, manifest)
                return {
                    "decision": "attention-required",
                    "session_id": manifest["session_id"],
                    "manifest": str(path),
                    "unknown_paths": sorted(unknown),
                }
            manifest["condition"] = "active"
            manifest["message"] = None
            _save_manifest(path, manifest)
            _spawn_guardian(path.parent, vault)
            return {
                "decision": "ready",
                "mode": (
                    "resumed-after-confirmed-stop"
                    if resumed_after_confirmed_stop
                    else "resumed"
                ),
                "session_id": manifest["session_id"],
                "manifest": str(path),
                "runtime_context_file": manifest["runtime_context_file"],
                "runtime_context": context,
            }

        if confirm_running_session_id is not None:
            raise StandaloneError(
                "confirmation-required",
                "No matching active session exists for the supplied confirmation ID",
                exit_code=3,
            )

        try:
            context = runtime_context.resolve_runtime_context(
                prepare_standalone=True,
            )
        except (
            active_run_guard.GuardError,
            config_schema.ConfigurationError,
            runtime_context.MachineConfigError,
            OSError,
        ) as exc:
            raise StandaloneError("invalid-runtime-context", str(exc)) from exc
        vault = Path(context["paths"]["vault"]).resolve()
        session_id = f"standalone-{operation}-{uuid4().hex[:16]}"
        run_dir = _session_dir(vault, session_id)
        path = run_dir / "standalone-session.json"
        context_file = run_dir / "runtime-context.json"
        base_head = str(context["preparation"]["remote_head"])
        repository = context["repository"]
        manifest = {
            "version": SESSION_VERSION,
            "session_id": session_id,
            "operation": operation,
            "intent": intent.strip(),
            "harness": harness,
            "condition": "active",
            "outcome": None,
            "started_at": _now(),
            "updated_at": _now(),
            "vault": str(vault),
            "runtime_context_file": str(context_file),
            "configuration_fingerprint": context["configuration_fingerprint"],
            "base_head": base_head,
            "baseline_dirty": _baseline(vault),
            "artifacts": {},
            "changed_paths": [],
            "publication": {
                "remote": str(repository["remote"]),
                "branch": str(repository["branch"]),
                "commit": None,
            },
            "message": None,
        }
        _write_context(context_file, context)
        _save_manifest(path, manifest)
        try:
            _spawn_guardian(run_dir, vault)
        except StandaloneError as exc:
            manifest["condition"] = "attention-required"
            manifest["message"] = str(exc)
            _save_manifest(path, manifest)
            raise
        return {
            "decision": "ready",
            "mode": "started",
            "session_id": session_id,
            "manifest": str(path),
            "runtime_context_file": str(context_file),
            "runtime_context": context,
        }


def _normalize_report(report: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "version",
        "session_id",
        "operation",
        "result",
        "artifacts",
        "changed_paths",
        "message",
    }
    if set(report) != fields or report.get("version") != REPORT_VERSION:
        raise StandaloneError("invalid-report", "Unsupported standalone report schema")
    if report.get("operation") not in OPERATIONS or report.get("result") not in REPORT_RESULTS:
        raise StandaloneError("invalid-report", "Invalid report operation or result")
    artifacts = report.get("artifacts")
    changed = report.get("changed_paths")
    if not isinstance(artifacts, list) or not isinstance(changed, list):
        raise StandaloneError("invalid-report", "Report artifacts/paths must be arrays")
    if len(artifacts) > 4096 or len(changed) > 4096:
        raise StandaloneError("invalid-report", "Report change set is too large")
    normalized: dict[str, dict[str, str]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {
            "path",
            "sha256",
            "kind",
        }:
            raise StandaloneError("invalid-report", "Invalid artifact record")
        relative = _safe_relative(artifact["path"], field="artifact.path")
        digest = artifact["sha256"]
        kind = artifact["kind"]
        if relative in normalized or not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(
            digest
        ):
            raise StandaloneError("invalid-report", "Duplicate path or invalid hash")
        if not isinstance(kind, str) or not kind or len(kind) > 64:
            raise StandaloneError("invalid-report", "Invalid artifact kind")
        normalized[relative] = {"sha256": digest, "kind": kind}
    normalized_changed = [
        _safe_relative(value, field="changed_paths") for value in changed
    ]
    if len(set(normalized_changed)) != len(normalized_changed):
        raise StandaloneError("invalid-report", "Duplicate changed_paths")
    if set(normalized_changed) != set(normalized):
        raise StandaloneError(
            "invalid-report",
            "changed_paths must exactly match artifact paths",
        )
    message = report.get("message")
    if message is not None and (not isinstance(message, str) or len(message) > 4096):
        raise StandaloneError("invalid-report", "Invalid report message")
    report["artifact_map"] = normalized
    report["changed_paths"] = normalized_changed
    return report


def _parse_report(path: Path) -> dict[str, Any]:
    try:
        anchored = anchored_file_path(path, label="Standalone report")
    except SafeIOError as exc:
        raise StandaloneError("report-read-failed", str(exc)) from exc
    return _normalize_report(
        _read_json(anchored, limit=MAX_REPORT_BYTES, label="Standalone report")
    )


def _build_direct_report(
    vault: Path,
    manifest: dict[str, Any],
    *,
    result: str,
    paths: tuple[str, ...],
    message: str | None,
) -> dict[str, Any]:
    artifacts = []
    for value in paths:
        relative = _safe_relative(value, field="path")
        artifacts.append(
            {
                "path": relative,
                "sha256": _file_sha256(_resolved_artifact(vault, relative)),
                "kind": "file",
            }
        )
    return _normalize_report(
        {
            "version": REPORT_VERSION,
            "session_id": manifest["session_id"],
            "operation": manifest["operation"],
            "result": result,
            "artifacts": artifacts,
            "changed_paths": list(paths),
            "message": message,
        }
    )


def _validate_report_artifacts(
    vault: Path,
    manifest: dict[str, Any],
    report: dict[str, Any],
) -> None:
    baseline = set(manifest["baseline_dirty"])
    for relative, artifact in report["artifact_map"].items():
        if relative in baseline:
            raise StandaloneError(
                "change-ownership-conflict",
                f"Session cannot claim a pre-existing dirty path: {relative}",
            )
        observed = _file_sha256(_resolved_artifact(vault, relative))
        if observed != artifact["sha256"]:
            raise StandaloneError(
                "artifact-conflict",
                f"Artifact hash differs from report: {relative}",
            )
        previous = manifest["artifacts"].get(relative)
        if previous is not None and previous != artifact:
            raise StandaloneError(
                "artifact-conflict",
                f"Registered artifact cannot be replaced: {relative}",
            )


def _commit_object(
    vault: Path,
    manifest: dict[str, Any],
    changed_paths: list[str],
) -> str:
    try:
        verify_index_versions(
            vault,
            base_commit=manifest["base_head"],
            expected_sha256_by_path={
                relative: manifest["artifacts"][relative]["sha256"]
                for relative in changed_paths
            },
            max_blob_bytes=MAX_ARTIFACT_BYTES,
        )
    except SafeGitError as exc:
        raise StandaloneError("index-conflict", str(exc)) from exc
    _git(vault, "add", "--", *changed_paths)
    staged = set(
        _git_output(
            vault,
            "-c",
            "core.quotePath=false",
            "diff",
            "--cached",
            "--name-only",
        ).splitlines()
    )
    if staged != set(changed_paths):
        raise StandaloneError(
            "index-conflict",
            f"Staged paths differ from session paths: {sorted(staged)}",
        )
    tree = _git_output(vault, "write-tree")
    command = [
        "commit-tree",
        tree,
        "-p",
        manifest["base_head"],
        "-m",
        f"{manifest['operation']}: {manifest['intent']}",
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": COMMIT_NAME,
            "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
            "GIT_AUTHOR_DATE": manifest["started_at"],
            "GIT_COMMITTER_NAME": COMMIT_NAME,
            "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
            "GIT_COMMITTER_DATE": manifest["started_at"],
        }
    )
    try:
        result = run_git_command(
            vault,
            *command,
            environment=environment,
        )
    except SafeGitError as exc:
        raise StandaloneError("git-error", str(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise StandaloneError("git-error", f"Could not create content commit: {detail}")
    return result.stdout.strip()


def _validate_commit(
    vault: Path,
    manifest: dict[str, Any],
    commit: str,
) -> None:
    parents = _git_output(vault, "rev-list", "--parents", "-n", "1", commit).split()
    if parents != [commit, manifest["base_head"]]:
        raise StandaloneError("commit-conflict", "Publication commit parent changed")
    changed = set(
        _git_output(
            vault,
            "-c",
            "core.quotePath=false",
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            commit,
        ).splitlines()
    )
    if changed != set(manifest["changed_paths"]):
        raise StandaloneError("commit-conflict", "Publication commit paths changed")
    for relative, artifact in manifest["artifacts"].items():
        blob = _git_blob(vault, f"{commit}:{relative}")
        if blob is None or hashlib.sha256(blob).hexdigest() != artifact["sha256"]:
            raise StandaloneError(
                "commit-conflict",
                f"Publication commit artifact differs: {relative}",
            )


def _publish(
    vault: Path,
    path: Path,
    manifest: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if manifest["baseline_dirty"]:
        raise StandaloneError(
            "dirty-baseline",
            "Automatic Git publication requires a clean session baseline",
        )
    commit = manifest["publication"]["commit"]
    remote_state = _remote_guard(context, vault)
    allowed_remote = {manifest["base_head"]}
    if commit is not None:
        allowed_remote.add(commit)
    if remote_state["remote_head"] not in allowed_remote:
        raise StandaloneError(
            "remote-advanced",
            "Remote HEAD changed while the standalone session was running",
            exit_code=3,
        )
    if commit is None:
        commit = _commit_object(vault, manifest, manifest["changed_paths"])
        manifest["publication"]["commit"] = commit
        _save_manifest(path, manifest)
    _validate_commit(vault, manifest, commit)
    local_head = _git_output(vault, "rev-parse", "HEAD")
    if local_head == manifest["base_head"]:
        update = _git(
            vault,
            "update-ref",
            f"refs/heads/{manifest['publication']['branch']}",
            commit,
            manifest["base_head"],
            check=False,
        )
        if update.returncode != 0:
            raise StandaloneError(
                "local-head-changed",
                "Local branch changed before publication",
                exit_code=3,
            )
    elif local_head != commit:
        raise StandaloneError(
            "local-head-changed",
            "Local branch is neither the session base nor publication commit",
            exit_code=3,
        )
    push = None
    if remote_state["remote_head"] != commit:
        push = _git(
            vault,
            "push",
            manifest["publication"]["remote"],
            f"{commit}:refs/heads/{manifest['publication']['branch']}",
            check=False,
        )
    observed = _remote_guard(context, vault)["remote_head"]
    if observed != commit:
        detail = ""
        if push is not None:
            detail = push.stderr.strip() or push.stdout.strip()
        manifest["condition"] = "interrupted"
        manifest["message"] = f"Publication push remains pending: {detail}"
        _save_manifest(path, manifest)
        raise StandaloneError(
            "push-failed",
            "Publication commit is preserved locally for retry",
            exit_code=3,
        )
    if _dirty_paths(vault):
        raise StandaloneError(
            "publication-verification-failed",
            "Vault is not clean after publication",
            exit_code=3,
        )
    _stop_guardian(path.parent)
    manifest["outcome"] = "published"
    manifest["condition"] = "active"
    manifest["message"] = None
    _save_manifest(path, manifest)
    return {
        "decision": "published",
        "session_id": manifest["session_id"],
        "commit": commit,
        "changed_paths": manifest["changed_paths"],
    }


def submit(
    *,
    session_id: str,
    report_path: Path | None = None,
    result: str | None = None,
    paths: tuple[str, ...] | list[str] = (),
    message: str | None = None,
) -> dict[str, Any]:
    direct_paths = tuple(paths)
    if report_path is not None and (
        result is not None or direct_paths or message is not None
    ):
        raise StandaloneError(
            "mixed-submission",
            "Use either --report or direct result/path arguments, not both",
        )
    if report_path is None and result is None:
        raise StandaloneError(
            "missing-submission",
            "Submit requires --report or --result",
        )
    vault = _configured_vault()
    path = _manifest_path(vault, session_id)
    parsed_report = (
        _parse_report(report_path)
        if report_path is not None
        else None
    )
    with _state_lock(vault):
        manifest = _load_manifest(path)
        if manifest["outcome"] is not None:
            _stop_guardian(path.parent)
            return {
                "decision": manifest["outcome"],
                "session_id": session_id,
                "changed_paths": manifest["changed_paths"],
                "commit": manifest["publication"]["commit"],
            }
        spawned_guardian = False
        if not _guardian_alive(path.parent):
            _spawn_guardian(path.parent, vault)
            spawned_guardian = True
        try:
            report = (
                parsed_report
                if parsed_report is not None
                else _build_direct_report(
                    vault,
                    manifest,
                    result=str(result),
                    paths=direct_paths,
                    message=message,
                )
            )
            if (
                report["session_id"] != session_id
                or report["operation"] != manifest["operation"]
            ):
                raise StandaloneError(
                    "invalid-report",
                    "Report identity differs from session",
                )
            context = _context_for_resume(manifest)
            if context["configuration_fingerprint"] != manifest["configuration_fingerprint"]:
                raise StandaloneError("config-conflict", "Session configuration changed")
            _verify_baseline(vault, manifest)
            _validate_report_artifacts(vault, manifest, report)
            manifest["artifacts"].update(report["artifact_map"])
            manifest["changed_paths"] = list(
                dict.fromkeys([*manifest["changed_paths"], *report["changed_paths"]])
            )
            manifest["message"] = report["message"]
            _verify_registered(vault, manifest)
            known = set(manifest["baseline_dirty"]) | set(manifest["changed_paths"])
            unknown = _dirty_paths(vault) - known
            if unknown:
                manifest["condition"] = "attention-required"
                manifest["message"] = (
                    "Unregistered Vault changes are preserved: "
                    + ", ".join(sorted(unknown))
                )
                _stop_guardian(path.parent)
                _save_manifest(path, manifest)
                raise StandaloneError(
                    "unknown-changes",
                    manifest["message"],
                    exit_code=3,
                )
            if report["result"] == "progress":
                manifest["condition"] = "active"
                _save_manifest(path, manifest)
                return {
                    "decision": "checkpointed",
                    "session_id": session_id,
                    "changed_paths": manifest["changed_paths"],
                }
            if report["result"] in {"recoverable", "attention-required"}:
                manifest["condition"] = (
                    "interrupted"
                    if report["result"] == "recoverable"
                    else "attention-required"
                )
                _stop_guardian(path.parent)
                _save_manifest(path, manifest)
                return {
                    "decision": manifest["condition"],
                    "session_id": session_id,
                    "changed_paths": manifest["changed_paths"],
                }

            if not manifest["changed_paths"]:
                _stop_guardian(path.parent)
                manifest["outcome"] = "unchanged"
                manifest["condition"] = "active"
                manifest["message"] = None
                _save_manifest(path, manifest)
                return {
                    "decision": "unchanged",
                    "session_id": session_id,
                    "changed_paths": [],
                }

            automation = context["automation"]
            if not automation["git_commit"] and not automation["git_push"]:
                _stop_guardian(path.parent)
                manifest["outcome"] = "completed-local"
                manifest["condition"] = "active"
                _save_manifest(path, manifest)
                return {
                    "decision": "completed-local",
                    "session_id": session_id,
                    "changed_paths": manifest["changed_paths"],
                }
            if not automation["git_commit"] or not automation["git_push"]:
                raise StandaloneError(
                    "invalid-config",
                    "Git commit and push must be enabled together",
                )
            try:
                return _publish(vault, path, manifest, context)
            except StandaloneError:
                _stop_guardian(path.parent)
                raise
        except Exception:
            if spawned_guardian:
                _stop_guardian(path.parent)
            raise


def inspect(*, session_id: str) -> dict[str, Any]:
    vault = _configured_vault()
    path = _manifest_path(vault, session_id)
    with _state_lock(vault):
        manifest = _load_manifest(path)
        registered_conflicts: list[str] = []
        for relative, artifact in manifest["artifacts"].items():
            try:
                if _file_sha256(_resolved_artifact(vault, relative)) != artifact["sha256"]:
                    registered_conflicts.append(relative)
            except StandaloneError:
                registered_conflicts.append(relative)
        known = set(manifest["baseline_dirty"]) | set(manifest["changed_paths"])
        return {
            "decision": "inspected",
            "session": manifest,
            "guardian_alive": _guardian_alive(path.parent),
            "registered_conflicts": sorted(registered_conflicts),
            "unknown_dirty_paths": sorted(_dirty_paths(vault) - known),
        }


def cancel(*, session_id: str, confirm_session_id: str) -> dict[str, Any]:
    if session_id != confirm_session_id:
        raise StandaloneError(
            "confirmation-required",
            "Cancellation requires the exact session ID",
        )
    vault = _configured_vault()
    path = _manifest_path(vault, session_id)
    with _state_lock(vault):
        manifest = _load_manifest(path)
        if manifest["outcome"] is not None:
            _stop_guardian(path.parent)
            return {
                "decision": manifest["outcome"],
                "session_id": session_id,
            }
        _stop_guardian(path.parent)
        manifest["outcome"] = "cancelled"
        manifest["condition"] = "active"
        manifest["message"] = "Cancelled after explicit user confirmation"
        _save_manifest(path, manifest)
        return {
            "decision": "cancelled",
            "session_id": session_id,
            "preserved_paths": manifest["changed_paths"],
        }


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Coordinate one standalone DailyPaper Vault-writing session."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--operation", choices=sorted(OPERATIONS), required=True)
    start_parser.add_argument("--harness", choices=sorted(HARNESSES), required=True)
    start_parser.add_argument("--intent", required=True)
    start_parser.add_argument("--confirm-running-session-id")
    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("--session-id", required=True)
    submit_parser.add_argument("--report", type=Path)
    submit_parser.add_argument("--result", choices=sorted(REPORT_RESULTS))
    submit_parser.add_argument("--path", action="append", default=[])
    submit_parser.add_argument("--message")
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--session-id", required=True)
    cancel_parser = subparsers.add_parser("cancel")
    cancel_parser.add_argument("--session-id", required=True)
    cancel_parser.add_argument("--confirm-session-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "start":
            result = start(
                operation=args.operation,
                harness=args.harness,
                intent=args.intent,
                confirm_running_session_id=args.confirm_running_session_id,
            )
        elif args.command == "submit":
            result = submit(
                session_id=args.session_id,
                report_path=args.report,
                result=args.result,
                paths=args.path,
                message=args.message,
            )
        elif args.command == "inspect":
            result = inspect(session_id=args.session_id)
        else:
            result = cancel(
                session_id=args.session_id,
                confirm_session_id=args.confirm_session_id,
            )
    except StandaloneError as exc:
        print(
            json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return exc.exit_code
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
