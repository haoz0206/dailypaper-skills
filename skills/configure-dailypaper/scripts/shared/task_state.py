#!/usr/bin/env python3
"""Strict, bounded codec for the cross-machine DailyPaper Task State v1."""

from __future__ import annotations

import copy
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from safe_io import (
    SafeIOError,
    atomic_write_bytes,
    encode_json_value,
    parse_json_object,
    read_regular_bytes,
)
from safe_path import SafePathError, relative_posix_path


STATE_VERSION = 1
TASK_NAME = "daily-papers"
MAX_TASK_STATE_BYTES = 64 * 1024
MAX_RUN_ID_LENGTH = 128
MAX_PATH_LENGTH = 1024
MAX_OWNER_LENGTH = 256
MAX_MESSAGE_LENGTH = MAX_TASK_STATE_BYTES
MAX_OUTPUTS = 32
MAX_CHANGED_PATHS = 4096

STATUSES = frozenset({"running", "success", "published", "failed", "cancelled"})
HARNESSES = frozenset({"claude-code", "codex"})
BASE_FIELDS = frozenset(
    {
        "version",
        "task",
        "target_date",
        "window_days",
        "status",
        "run_id",
        "harness",
        "owner",
        "started_at",
        "updated_at",
        "base_commit",
        "config_sha256",
        "outputs",
    }
)
STATUS_FIELDS = {
    "running": frozenset({"lease_until"}),
    "success": frozenset({"completed_at", "changed_paths"}),
    "published": frozenset({"completed_at", "changed_paths"}),
    "failed": frozenset({"failed_at", "message"}),
    "cancelled": frozenset({"cancelled_at"}),
}
ALL_FIELDS = BASE_FIELDS | frozenset().union(*STATUS_FIELDS.values())

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OUTPUT_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RESERVED_VAULT_ROOTS = frozenset({".git", ".dailypaper"})


class TaskStateError(ValueError):
    """Task State data is malformed, unsupported, or unsafe."""


def _require_string(
    value: Any,
    *,
    field: str,
    maximum: int,
    allow_newlines: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskStateError(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise TaskStateError(f"{field} must not exceed {maximum} characters")
    allowed_controls = {"\n", "\r", "\t"} if allow_newlines else set()
    if any(
        ord(character) < 32 and character not in allowed_controls
        for character in value
    ):
        raise TaskStateError(f"{field} contains unsafe control characters")
    return value


def _require_timestamp(value: Any, *, field: str) -> str:
    timestamp = _require_string(value, field=field, maximum=64)
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise TaskStateError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TaskStateError(f"{field} must include an explicit UTC offset")
    return timestamp


def _require_target_date(value: Any) -> str:
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise TaskStateError("target_date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise TaskStateError("target_date must be a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise TaskStateError("target_date must use canonical YYYY-MM-DD")
    return value


def _require_window_days(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 31:
        raise TaskStateError("window_days must be an integer from 1 to 31")
    return value


def _require_run_id(value: Any) -> str:
    run_id = _require_string(
        value,
        field="run_id",
        maximum=MAX_RUN_ID_LENGTH,
    )
    if (
        run_id in {".", ".."}
        or ".." in run_id
        or "/" in run_id
        or "\\" in run_id
        or not RUN_ID_PATTERN.fullmatch(run_id)
    ):
        raise TaskStateError(
            "run_id must be one safe ASCII path segment without separators or '..'"
        )
    return run_id


def _require_vault_path(value: Any, *, field: str) -> str:
    path = _require_string(value, field=field, maximum=MAX_PATH_LENGTH)
    try:
        pure = relative_posix_path(
            path,
            max_chars=MAX_PATH_LENGTH,
            label=field,
        )
    except SafePathError as exc:
        raise TaskStateError(
            f"{field} must be a safe Vault-relative POSIX path"
        ) from exc
    if pure.parts[0] in RESERVED_VAULT_ROOTS:
        raise TaskStateError(f"{field} must be a safe Vault-relative POSIX path")
    return pure.as_posix()


def _require_outputs(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise TaskStateError("outputs must be a non-empty JSON object")
    if len(value) > MAX_OUTPUTS:
        raise TaskStateError(f"outputs must not contain more than {MAX_OUTPUTS} entries")
    outputs: dict[str, str] = {}
    for role, path in value.items():
        if not isinstance(role, str) or not OUTPUT_ROLE_PATTERN.fullmatch(role):
            raise TaskStateError(f"Invalid outputs role: {role!r}")
        outputs[role] = _require_vault_path(path, field=f"outputs.{role}")
    return outputs


def _require_changed_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise TaskStateError("changed_paths must be a JSON array")
    if len(value) > MAX_CHANGED_PATHS:
        raise TaskStateError(
            f"changed_paths must not contain more than {MAX_CHANGED_PATHS} entries"
        )
    paths: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        path = _require_vault_path(item, field=f"changed_paths[{index}]")
        if path in seen:
            raise TaskStateError(f"changed_paths contains duplicate path: {path}")
        seen.add(path)
        paths.append(path)
    return paths


def validate_task_state(
    value: Any,
    *,
    source: str = "Task State",
) -> dict[str, Any]:
    """Validate and return an isolated canonical Task State v1 object."""
    if not isinstance(value, Mapping):
        raise TaskStateError(f"Task State at {source} must be a JSON object")
    state = copy.deepcopy(dict(value))
    version = state.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != STATE_VERSION:
        raise TaskStateError(f"Unsupported Task State version at {source}")
    if state.get("task") != TASK_NAME:
        raise TaskStateError(f"Unexpected task name at {source}")

    # Pre-release Task State v1 did not freeze the acquisition window. Its
    # only defined behavior was the one-day run, so that is the sole safe
    # interpretation during migration.
    state.setdefault("window_days", 1)

    status = state.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        raise TaskStateError(f"Unexpected task status {status!r} at {source}")
    required = BASE_FIELDS | STATUS_FIELDS[status]
    actual = set(state)
    missing = required - actual
    unknown = actual - ALL_FIELDS
    incompatible = actual - required
    if missing:
        raise TaskStateError(
            f"Task State at {source} is missing required fields: "
            + ", ".join(sorted(missing))
        )
    if unknown:
        raise TaskStateError(
            f"Task State at {source} contains unknown fields: "
            + ", ".join(sorted(unknown))
        )
    if incompatible:
        raise TaskStateError(
            f"Task State at {source} contains fields incompatible with "
            f"status {status!r}: "
            + ", ".join(sorted(incompatible))
        )

    state["target_date"] = _require_target_date(state["target_date"])
    state["window_days"] = _require_window_days(state["window_days"])
    state["run_id"] = _require_run_id(state["run_id"])
    harness = state["harness"]
    if not isinstance(harness, str) or harness not in HARNESSES:
        raise TaskStateError(
            "harness must be one of: " + ", ".join(sorted(HARNESSES))
        )
    state["owner"] = _require_string(
        state["owner"],
        field="owner",
        maximum=MAX_OWNER_LENGTH,
    )
    state["started_at"] = _require_timestamp(
        state["started_at"],
        field="started_at",
    )
    state["updated_at"] = _require_timestamp(
        state["updated_at"],
        field="updated_at",
    )
    base_commit = state["base_commit"]
    if not isinstance(base_commit, str) or not COMMIT_PATTERN.fullmatch(base_commit):
        raise TaskStateError("base_commit must be a lowercase Git commit hash")
    config_sha256 = state["config_sha256"]
    if (
        not isinstance(config_sha256, str)
        or not SHA256_PATTERN.fullmatch(config_sha256)
    ):
        raise TaskStateError("config_sha256 must be a lowercase SHA-256 digest")
    state["outputs"] = _require_outputs(state["outputs"])

    if status == "running":
        state["lease_until"] = _require_timestamp(
            state["lease_until"],
            field="lease_until",
        )
    elif status in {"success", "published"}:
        state["completed_at"] = _require_timestamp(
            state["completed_at"],
            field="completed_at",
        )
        state["changed_paths"] = _require_changed_paths(state["changed_paths"])
    elif status == "failed":
        state["failed_at"] = _require_timestamp(
            state["failed_at"],
            field="failed_at",
        )
        state["message"] = _require_string(
            state["message"],
            field="message",
            maximum=MAX_MESSAGE_LENGTH,
            allow_newlines=True,
        )
    else:
        state["cancelled_at"] = _require_timestamp(
            state["cancelled_at"],
            field="cancelled_at",
        )
    return state


def parse_task_state(
    raw: bytes | str,
    *,
    source: str = "Task State",
) -> dict[str, Any]:
    """Decode one bounded strict UTF-8 JSON Task State."""
    if isinstance(raw, str):
        try:
            payload = raw.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TaskStateError(f"Task State at {source} is not valid UTF-8") from exc
    elif isinstance(raw, bytes):
        payload = raw
    else:
        raise TaskStateError("Task State payload must be bytes or text")
    if len(payload) > MAX_TASK_STATE_BYTES:
        raise TaskStateError(
            f"Task State at {source} exceeds the "
            f"{MAX_TASK_STATE_BYTES}-byte safety limit"
        )
    try:
        value = parse_json_object(
            payload,
            max_bytes=MAX_TASK_STATE_BYTES,
            label=f"Task State at {source}",
        )
    except SafeIOError as exc:
        raise TaskStateError(str(exc)) from exc
    return validate_task_state(value, source=source)


def encode_task_state(
    state: Mapping[str, Any],
    *,
    source: str = "Task State",
) -> bytes:
    """Validate and encode one canonical Task State within the wire-size limit."""
    validated = validate_task_state(state, source=source)
    try:
        return encode_json_value(
            validated,
            max_bytes=MAX_TASK_STATE_BYTES,
            label=f"Task State at {source}",
        )
    except SafeIOError as exc:
        raise TaskStateError(
            str(exc)
        ) from exc


def read_task_state_file(path: Path) -> dict[str, Any] | None:
    """Race-safely read one bounded regular Task State file without following links."""
    state_path = path.expanduser()
    try:
        raw = read_regular_bytes(
            state_path,
            max_bytes=MAX_TASK_STATE_BYTES,
            required=False,
            label="Task State",
        )
    except SafeIOError as exc:
        raise TaskStateError(str(exc)) from exc
    if raw is None:
        return None
    return parse_task_state(raw, source=str(state_path))


def write_task_state_file(path: Path, state: Mapping[str, Any]) -> None:
    """Atomically validate and write one bounded Task State file."""
    state_path = path.expanduser()
    if state_path.is_symlink():
        raise TaskStateError(f"Task State is not a regular file: {state_path}")
    payload = encode_task_state(state, source=str(state_path))
    try:
        atomic_write_bytes(
            state_path,
            payload,
            mode=0o644,
            preserve_existing_mode=True,
            label="Task State",
        )
    except SafeIOError as exc:
        raise TaskStateError(str(exc)) from exc
