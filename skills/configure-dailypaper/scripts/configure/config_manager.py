#!/usr/bin/env python3
"""Safely inspect and update the shared DailyPaper Vault configuration."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[2]
SHARED_DIR = SKILL_ROOT / "scripts" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

import active_run_guard
import config_schema
import run_guardian
from safe_io import (
    DocumentTooLargeError,
    SafeIOError,
    anchored_file_path,
    atomic_write_bytes,
    encode_json_value,
    parse_json_object,
    read_regular_bytes,
)
from safe_git import (
    GitCommandResult,
    SafeGitError,
    read_git_blob,
    repository_dirty_paths,
    run_git_command,
)
from user_config import DEFAULT_CONFIG


CONFIG_RELATIVE = Path(".dailypaper/config.json")
EDITABLE_DAILY_FIELDS = set(config_schema.DAILY_FIELDS)
EDITABLE_AUTOMATION_FIELDS = {
    "auto_refresh_indexes",
    "git_commit",
    "git_push",
}
ConfigError = config_schema.ConfigurationError
TRANSACTION_VERSION = 1
MAX_TRANSACTION_BYTES = 4 * 1024 * 1024
COMMIT_NAME = "dailypaper automation"
COMMIT_EMAIL = "dailypaper@localhost"
OID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ActiveRunError(ConfigError):
    """A daily run currently owns the shared Vault."""


def _configuration_failpoint(_name: str) -> None:
    """Private fault-injection seam used by transaction recovery tests."""


def _load_json(path: Path, *, required: bool = True) -> dict[str, Any]:
    return config_schema.load_json_object(
        path,
        required=required,
        label="Configuration",
    )


def resolve_config_path(vault: Path, configured: Path | None = None) -> Path:
    vault = vault.expanduser().resolve()
    expected = vault / CONFIG_RELATIVE
    current = vault
    for part in CONFIG_RELATIVE.parent.parts:
        current = current / part
        if current.is_symlink():
            raise ConfigError(
                f"Shared configuration parent must not be a symlink: {current}"
            )
        if current.exists() and not current.is_dir():
            raise ConfigError(
                f"Shared configuration parent is not a directory: {current}"
            )
    if expected.is_symlink():
        raise ConfigError(
            f"Shared configuration must not be a symlink: {expected}"
        )
    resolved = expected.resolve()
    try:
        resolved.relative_to(vault)
    except ValueError as exc:
        raise ConfigError("Shared configuration escapes the Vault") from exc
    if configured is not None and configured.expanduser().resolve() != resolved:
        raise ConfigError(
            f"Shared configuration must be {resolved}, not "
            f"{configured.expanduser().resolve()}"
        )
    return resolved


def _base_config() -> dict[str, Any]:
    tracked = _load_json(SHARED_DIR / "user-config.json")
    config_schema.validate_effective_config(tracked, DEFAULT_CONFIG)
    return tracked


def load_effective_config(config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    external = _load_json(config_path)
    base = _base_config()
    config_schema.validate_overlay(external, base, DEFAULT_CONFIG)
    effective = config_schema.deep_merge(copy.deepcopy(base), external)
    config_schema.validate_effective_config(effective, DEFAULT_CONFIG)
    return effective, external


def normalize_daily_config(value: Any) -> dict[str, Any]:
    return config_schema.normalize_daily_config(value)


def _validate_external_safety(external: dict[str, Any]) -> None:
    config_schema.validate_overlay(
        external,
        _base_config(),
        DEFAULT_CONFIG,
    )


def validate_config(config_path: Path) -> dict[str, Any]:
    effective, _external = load_effective_config(config_path)
    return effective


def _validate_patch(patch: dict[str, Any]) -> None:
    unknown_sections = set(patch) - {"daily_papers", "automation"}
    if unknown_sections:
        raise ConfigError(
            "Unsupported configuration sections: "
            + ", ".join(sorted(unknown_sections))
        )
    if not patch:
        raise ConfigError("Patch must not be empty")

    if "daily_papers" in patch:
        daily_patch = patch["daily_papers"]
        if not isinstance(daily_patch, dict):
            raise ConfigError("Patch daily_papers must be an object")
        unknown = set(daily_patch) - EDITABLE_DAILY_FIELDS
        if unknown:
            raise ConfigError(
                "Unsupported daily_papers patch fields: "
                + ", ".join(sorted(unknown))
            )
        if not daily_patch:
            raise ConfigError("Patch daily_papers must not be empty")

    if "automation" in patch:
        automation_patch = patch["automation"]
        if not isinstance(automation_patch, dict):
            raise ConfigError("Patch automation must be an object")
        unknown = set(automation_patch) - EDITABLE_AUTOMATION_FIELDS
        if unknown:
            raise ConfigError(
                "Unsupported automation patch fields: "
                + ", ".join(sorted(unknown))
            )
        if not automation_patch:
            raise ConfigError("Patch automation must not be empty")
        for field, value in automation_patch.items():
            if not isinstance(value, bool):
                raise ConfigError(
                    f"automation.{field} patch must be a boolean"
                )


def _changes(before: Any, after: Any, prefix: str = "") -> list[dict[str, Any]]:
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else key
            changes.extend(_changes(before.get(key), after.get(key), path))
        return changes
    if before == after:
        return []
    return [{"path": prefix, "before": before, "after": after}]


def build_plan(
    config_path: Path,
    patch_path: Path,
    *,
    allow_no_changes: bool = False,
) -> dict[str, Any]:
    effective, external = load_effective_config(config_path)
    patch = _load_json(patch_path)
    _validate_patch(patch)
    if not isinstance(effective.get("daily_papers"), dict):
        raise ConfigError("Existing effective daily_papers must be an object")
    if not isinstance(effective.get("automation"), dict):
        raise ConfigError("Existing effective automation must be an object")
    for field in EDITABLE_AUTOMATION_FIELDS:
        if not isinstance(effective["automation"].get(field), bool):
            raise ConfigError(
                f"Existing automation.{field} must be a boolean"
            )

    proposed_external = copy.deepcopy(external)

    if "daily_papers" in patch:
        daily = copy.deepcopy(effective["daily_papers"])
        daily.update(copy.deepcopy(patch["daily_papers"]))
        normalized_daily = normalize_daily_config(daily)
        proposed_external["daily_papers"] = normalized_daily

    if "automation" in patch:
        external_automation = proposed_external.setdefault("automation", {})
        if not isinstance(external_automation, dict):
            raise ConfigError("Existing external automation must be an object")
        external_automation.update(copy.deepcopy(patch["automation"]))

    _validate_external_safety(proposed_external)
    proposed_effective = _base_config()
    config_schema.deep_merge(proposed_effective, proposed_external)
    config_schema.validate_effective_config(
        proposed_effective,
        DEFAULT_CONFIG,
    )
    changes = _changes(
        {
            "daily_papers": effective["daily_papers"],
            "automation": {
                field: effective["automation"][field]
                for field in sorted(EDITABLE_AUTOMATION_FIELDS)
            },
        },
        {
            "daily_papers": proposed_effective["daily_papers"],
            "automation": {
                field: proposed_effective["automation"][field]
                for field in sorted(EDITABLE_AUTOMATION_FIELDS)
            },
        },
    )
    if not changes and not allow_no_changes:
        raise ConfigError("Patch produces no effective configuration changes")

    return {
        "config_path": str(config_path),
        "changes": changes,
        "proposed": proposed_external,
    }


def _git(
    vault: Path,
    *args: str,
    check: bool = True,
) -> GitCommandResult:
    try:
        result = run_git_command(vault, *args)
    except SafeGitError as exc:
        raise ConfigError(str(exc)) from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ConfigError(f"git {' '.join(args)} failed: {detail}")
    return result


def _git_output(vault: Path, *args: str) -> str:
    return _git(vault, *args).stdout.strip()


def _git_blob(vault: Path, object_name: str) -> bytes | None:
    try:
        return read_git_blob(
            vault,
            object_name,
            max_bytes=config_schema.MAX_CONFIG_BYTES,
        )
    except SafeGitError as exc:
        raise ConfigError(str(exc)) from exc


def _read_regular_bytes(path: Path, *, label: str, limit: int) -> bytes:
    try:
        content = read_regular_bytes(
            path,
            max_bytes=limit,
            label=label,
        )
    except SafeIOError as exc:
        raise ConfigError(str(exc)) from exc
    if content is None:
        raise ConfigError(f"{label} file does not exist: {path}")
    return content


def _atomic_bytes(path: Path, content: bytes, *, mode: int = 0o644) -> None:
    try:
        atomic_write_bytes(
            path,
            content,
            mode=mode,
            label="Configuration transaction file",
        )
    except SafeIOError as exc:
        raise ConfigError(str(exc)) from exc


def _encode_json(value: dict[str, Any]) -> bytes:
    try:
        return encode_json_value(
            value,
            max_bytes=MAX_TRANSACTION_BYTES,
            label="Configuration transaction",
        )
    except DocumentTooLargeError as exc:
        raise ConfigError("Configuration publication transaction is too large") from exc
    except SafeIOError as exc:
        raise ConfigError(str(exc)) from exc


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _transaction_path(vault: Path) -> Path:
    common = Path(_git_output(vault, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (vault / common).resolve()
    root = common / "dailypaper"
    if root.is_symlink():
        raise ConfigError(f"Configuration transaction directory is a symlink: {root}")
    if root.exists() and not root.is_dir():
        raise ConfigError(
            f"Configuration transaction parent is not a directory: {root}"
        )
    return root / "configuration-publication-v1.json"


def _write_transaction(path: Path, value: dict[str, Any]) -> None:
    content = _encode_json(value)
    _atomic_bytes(path, content, mode=0o600)


def _delete_transaction(path: Path) -> None:
    path.unlink(missing_ok=True)
    if path.parent.is_dir():
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _decode_transaction_content(value: Any, *, field: str) -> bytes:
    if not isinstance(value, str):
        raise ConfigError(f"Configuration transaction {field} is invalid")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ConfigError(
            f"Configuration transaction {field} is not valid base64"
        ) from exc


def _load_transaction(path: Path) -> tuple[dict[str, Any], bytes, bytes]:
    raw = _read_regular_bytes(
        path,
        label="Configuration publication transaction",
        limit=MAX_TRANSACTION_BYTES,
    )
    try:
        value = parse_json_object(
            raw,
            max_bytes=MAX_TRANSACTION_BYTES,
            label="Configuration publication transaction",
        )
    except SafeIOError as exc:
        raise ConfigError(str(exc)) from exc
    fields = {
        "version",
        "base_head",
        "patch_sha256",
        "before_sha256",
        "after_sha256",
        "before_base64",
        "after_base64",
        "started_at",
        "remote",
        "branch",
        "commit",
        "outcome",
        "changes",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("version") != TRANSACTION_VERSION
    ):
        raise ConfigError("Unsupported configuration publication transaction")
    for field in ("base_head",):
        if not isinstance(value[field], str) or not OID_PATTERN.fullmatch(value[field]):
            raise ConfigError(f"Invalid transaction {field}")
    for field in ("patch_sha256", "before_sha256", "after_sha256"):
        if not isinstance(value[field], str) or not SHA256_PATTERN.fullmatch(
            value[field]
        ):
            raise ConfigError(f"Invalid transaction {field}")
    commit = value["commit"]
    if commit is not None and (
        not isinstance(commit, str) or not OID_PATTERN.fullmatch(commit)
    ):
        raise ConfigError("Invalid transaction commit")
    if value["outcome"] not in {None, "published"}:
        raise ConfigError("Invalid transaction outcome")
    if value["outcome"] == "published" and commit is None:
        raise ConfigError("Published configuration transaction has no commit")
    if not all(
        isinstance(value[field], str) and value[field]
        for field in ("remote", "branch", "started_at")
    ):
        raise ConfigError("Invalid transaction publication metadata")
    repository = DEFAULT_CONFIG["repository"]
    if (
        value["remote"] != repository["remote"]
        or value["branch"] != repository["branch"]
    ):
        raise ConfigError("Configuration transaction publication target changed")
    try:
        timestamp = datetime.fromisoformat(value["started_at"])
    except ValueError as exc:
        raise ConfigError("Invalid transaction timestamp") from exc
    if timestamp.tzinfo is None:
        raise ConfigError("Transaction timestamp must be timezone-aware")
    if not isinstance(value["changes"], list) or len(value["changes"]) > 256:
        raise ConfigError("Invalid transaction changes")
    for change in value["changes"]:
        if (
            not isinstance(change, dict)
            or set(change) != {"path", "before", "after"}
            or not isinstance(change["path"], str)
            or not change["path"]
            or len(change["path"]) > 256
        ):
            raise ConfigError("Invalid transaction change record")
    before = _decode_transaction_content(value["before_base64"], field="before")
    after = _decode_transaction_content(value["after_base64"], field="after")
    if _sha256(before) != value["before_sha256"]:
        raise ConfigError("Configuration transaction before hash differs")
    if _sha256(after) != value["after_sha256"]:
        raise ConfigError("Configuration transaction after hash differs")
    previous = config_schema.parse_json_object(
        before,
        label="Previous shared configuration",
    )
    _validate_external_safety(previous)
    proposed = config_schema.parse_json_object(
        after,
        label="Proposed shared configuration",
    )
    _validate_external_safety(proposed)
    return value, before, after


def _dirty_paths(vault: Path) -> set[str]:
    try:
        return repository_dirty_paths(vault)
    except SafeGitError as exc:
        raise ConfigError(str(exc)) from exc


def _fresh_remote_state(vault: Path) -> dict[str, Any]:
    repository = DEFAULT_CONFIG["repository"]
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
        raise ActiveRunError(str(exc)) from exc
    except active_run_guard.GuardError as exc:
        raise ConfigError(str(exc)) from exc


def _create_configuration_commit(
    vault: Path,
    transaction: dict[str, Any],
) -> str:
    relative = CONFIG_RELATIVE.as_posix()
    _git(vault, "add", "--", relative)
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
    if staged != {relative}:
        raise ConfigError(
            f"Staged paths differ from the shared configuration: {sorted(staged)}"
        )
    tree = _git_output(vault, "write-tree")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": COMMIT_NAME,
            "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
            "GIT_AUTHOR_DATE": transaction["started_at"],
            "GIT_COMMITTER_NAME": COMMIT_NAME,
            "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
            "GIT_COMMITTER_DATE": transaction["started_at"],
        }
    )
    try:
        result = run_git_command(
            vault,
            "commit-tree",
            tree,
            "-p",
            transaction["base_head"],
            "-m",
            "configure daily papers",
            environment=environment,
        )
    except SafeGitError as exc:
        raise ConfigError(str(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ConfigError(f"Could not create configuration commit: {detail}")
    return result.stdout.strip()


def _validate_configuration_commit(
    vault: Path,
    transaction: dict[str, Any],
) -> None:
    commit = transaction["commit"]
    parents = _git_output(vault, "rev-list", "--parents", "-n", "1", commit).split()
    if parents != [commit, transaction["base_head"]]:
        raise ConfigError("Configuration commit parent changed")
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
    if changed != {CONFIG_RELATIVE.as_posix()}:
        raise ConfigError("Configuration commit contains unexpected paths")
    blob = _git_blob(vault, f"{commit}:{CONFIG_RELATIVE.as_posix()}")
    if (
        blob is None
        or hashlib.sha256(blob).hexdigest() != transaction["after_sha256"]
    ):
        raise ConfigError("Configuration commit content changed")


def _continue_publication(
    vault: Path,
    config_path: Path,
    transaction_path: Path,
    transaction: dict[str, Any],
    before: bytes,
    after: bytes,
) -> dict[str, Any]:
    remote_state = _fresh_remote_state(vault)
    commit = transaction["commit"]
    allowed_remote = {transaction["base_head"]}
    if commit is not None:
        allowed_remote.add(commit)
    if remote_state["remote_head"] not in allowed_remote:
        raise ConfigError(
            "Remote HEAD changed during configuration publication; "
            "the preserved transaction was not rebased or overwritten"
        )
    local_head = _git_output(vault, "rev-parse", "--verify", "HEAD")
    allowed_local = {transaction["base_head"]}
    if commit is not None:
        allowed_local.add(commit)
    if local_head not in allowed_local:
        raise ConfigError("Local HEAD changed during configuration publication")
    current = _read_regular_bytes(
        config_path,
        label="Shared configuration",
        limit=config_schema.MAX_CONFIG_BYTES,
    )
    if current not in {before, after}:
        raise ConfigError(
            "Shared configuration was changed outside the pending transaction"
        )
    unexpected = _dirty_paths(vault) - {CONFIG_RELATIVE.as_posix()}
    if unexpected:
        raise ConfigError(
            "Unrelated Vault changes are preserved and block configuration "
            f"publication: {sorted(unexpected)}"
        )
    relative = CONFIG_RELATIVE.as_posix()
    index_content = _git_blob(vault, f":{relative}")
    base_content = _git_blob(vault, f"{transaction['base_head']}:{relative}")
    if base_content != before:
        raise ConfigError("Transaction base configuration differs from its commit")
    if index_content not in {before, after}:
        raise ConfigError(
            "The Git index contains an unregistered configuration version; "
            "it was preserved"
        )
    if current == before:
        _atomic_bytes(config_path, after)
        _configuration_failpoint("after-config-write")
    validate_config(config_path)

    if commit is None:
        commit = _create_configuration_commit(vault, transaction)
        transaction["commit"] = commit
        _write_transaction(transaction_path, transaction)
        _configuration_failpoint("after-commit")
    _validate_configuration_commit(vault, transaction)

    local_head = _git_output(vault, "rev-parse", "--verify", "HEAD")
    if local_head == transaction["base_head"]:
        update = _git(
            vault,
            "update-ref",
            f"refs/heads/{transaction['branch']}",
            commit,
            transaction["base_head"],
            check=False,
        )
        if update.returncode != 0:
            raise ConfigError("Local branch changed before configuration publication")
        _configuration_failpoint("after-local-update")
    elif local_head != commit:
        raise ConfigError("Local branch no longer matches the configuration transaction")

    push = None
    if remote_state["remote_head"] != commit:
        push = _git(
            vault,
            "push",
            transaction["remote"],
            f"{commit}:refs/heads/{transaction['branch']}",
            check=False,
        )
        _configuration_failpoint("after-push")
    observed = _fresh_remote_state(vault)["remote_head"]
    if observed != commit:
        detail = ""
        if push is not None:
            detail = push.stderr.strip() or push.stdout.strip()
        raise ConfigError(
            "Configuration commit is preserved locally for retry"
            + (f": {detail}" if detail else "")
        )
    if _dirty_paths(vault):
        raise ConfigError("Vault is not clean after configuration publication")
    transaction["outcome"] = "published"
    _write_transaction(transaction_path, transaction)
    return {
        "status": "published",
        "config_path": str(config_path),
        "changes": transaction["changes"],
        "commit": commit,
        "remote_head": observed,
    }


def _published_receipt_result(
    vault: Path,
    config_path: Path,
    transaction: dict[str, Any],
    after: bytes,
) -> dict[str, Any] | None:
    remote = _fresh_remote_state(vault)["remote_head"]
    local = _git_output(vault, "rev-parse", "--verify", "HEAD")
    current = _read_regular_bytes(
        config_path,
        label="Shared configuration",
        limit=config_schema.MAX_CONFIG_BYTES,
    )
    if (
        remote != transaction["commit"]
        or local != transaction["commit"]
        or current != after
        or _dirty_paths(vault)
    ):
        return None
    return {
        "status": "already-published",
        "config_path": str(config_path),
        "changes": transaction["changes"],
        "commit": transaction["commit"],
        "remote_head": remote,
    }


def resume_publication(vault: Path, config_path: Path) -> dict[str, Any]:
    """Resume a journaled configuration publication without its patch file."""
    vault = vault.expanduser().resolve()
    config_path = resolve_config_path(vault, config_path)
    try:
        with run_guardian.hold_vault_writer_lock(vault):
            transaction_path = _transaction_path(vault)
            if transaction_path.is_symlink():
                raise ConfigError(
                    "Configuration publication transaction must not be a symlink"
                )
            if not transaction_path.exists():
                raise ConfigError("No configuration publication is available to resume")
            transaction, before, after = _load_transaction(transaction_path)
            if transaction["outcome"] == "published":
                result = _published_receipt_result(
                    vault,
                    config_path,
                    transaction,
                    after,
                )
                if result is None:
                    raise ConfigError(
                        "The completed configuration receipt no longer matches "
                        "the current Vault"
                    )
                return result
            return _continue_publication(
                vault,
                config_path,
                transaction_path,
                transaction,
                before,
                after,
            )
    except run_guardian.GuardianError as exc:
        raise ConfigError(str(exc)) from exc


def apply_plan(vault: Path, config_path: Path, patch_path: Path) -> dict[str, Any]:
    """Apply and publish one exact, crash-resumable configuration transaction."""
    vault = vault.expanduser().resolve()
    config_path = resolve_config_path(vault, config_path)
    try:
        patch_path = anchored_file_path(
            patch_path,
            label="Configuration patch",
        )
    except SafeIOError as exc:
        raise ConfigError(str(exc)) from exc
    try:
        writer_lock = run_guardian.hold_vault_writer_lock(vault)
        with writer_lock:
            transaction_path = _transaction_path(vault)
            patch_bytes = _read_regular_bytes(
                patch_path,
                label="Configuration patch",
                limit=config_schema.MAX_CONFIG_BYTES,
            )
            patch_sha = _sha256(patch_bytes)
            if transaction_path.is_symlink():
                raise ConfigError(
                    "Configuration publication transaction must not be a symlink"
                )
            if transaction_path.exists():
                transaction, before, after = _load_transaction(transaction_path)
                if transaction["outcome"] == "published":
                    receipt = _published_receipt_result(
                        vault,
                        config_path,
                        transaction,
                        after,
                    )
                    if (
                        transaction["patch_sha256"] == patch_sha
                        and receipt is not None
                    ):
                        return receipt
                    _delete_transaction(transaction_path)
                    transaction = {}
                elif transaction["patch_sha256"] != patch_sha:
                    raise ConfigError(
                        "Another configuration publication is pending; retry it "
                        "with the original patch before starting a new change"
                    )
            if not transaction_path.exists():
                repository = DEFAULT_CONFIG["repository"]
                try:
                    preparation = active_run_guard.prepare_standalone_vault(
                        vault,
                        repository_url=str(repository["url"]),
                        remote=str(repository["remote"]),
                        branch=str(repository["branch"]),
                    )
                except active_run_guard.GuardError as exc:
                    raise ConfigError(str(exc)) from exc
                if preparation["dirty"]:
                    raise ConfigError(
                        "Vault worktree must be clean before configuration apply"
                    )
                remote_guard = _fresh_remote_state(vault)
                if remote_guard["remote_head"] != preparation["remote_head"]:
                    raise ConfigError(
                        "Remote HEAD changed while preparing configuration apply"
                    )
                plan = build_plan(
                    config_path,
                    patch_path,
                    allow_no_changes=True,
                )
                if not plan["changes"]:
                    return {
                        "status": "unchanged",
                        "config_path": str(config_path),
                        "changes": [],
                        "commit": None,
                        "remote_head": preparation["remote_head"],
                    }
                before = _read_regular_bytes(
                    config_path,
                    label="Shared configuration",
                    limit=config_schema.MAX_CONFIG_BYTES,
                )
                after = _encode_json(plan["proposed"])
                proposed = config_schema.parse_json_object(
                    after,
                    label="Proposed shared configuration",
                )
                _validate_external_safety(proposed)
                transaction = {
                    "version": TRANSACTION_VERSION,
                    "base_head": preparation["remote_head"],
                    "patch_sha256": patch_sha,
                    "before_sha256": _sha256(before),
                    "after_sha256": _sha256(after),
                    "before_base64": base64.b64encode(before).decode("ascii"),
                    "after_base64": base64.b64encode(after).decode("ascii"),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "remote": str(repository["remote"]),
                    "branch": str(repository["branch"]),
                    "commit": None,
                    "outcome": None,
                    "changes": plan["changes"],
                }
                _write_transaction(transaction_path, transaction)
                _configuration_failpoint("after-transaction")
            return _continue_publication(
                vault,
                config_path,
                transaction_path,
                transaction,
                before,
                after,
            )
    except run_guardian.GuardianError as exc:
        raise ConfigError(str(exc)) from exc


def prepare_configuration(vault: Path) -> dict[str, Any]:
    """Safely fast-forward the configured Vault before show/plan."""
    vault = vault.expanduser().resolve()
    repository = DEFAULT_CONFIG["repository"]
    try:
        with run_guardian.hold_vault_writer_lock(vault):
            return active_run_guard.prepare_standalone_vault(
                vault,
                repository_url=str(repository["url"]),
                remote=str(repository["remote"]),
                branch=str(repository["branch"]),
            )
    except (active_run_guard.GuardError, run_guardian.GuardianError) as exc:
        raise ConfigError(str(exc)) from exc


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("prepare")
    subparsers.add_parser("resume")
    subparsers.add_parser("show")
    subparsers.add_parser("validate")
    for command in ("plan", "apply"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--patch", type=Path, required=True)

    args = parser.parse_args()

    try:
        vault = args.vault.expanduser().resolve()
        config_path = resolve_config_path(vault, args.config)
        if args.command == "prepare":
            _print_json(prepare_configuration(vault))
        elif args.command == "resume":
            _print_json(resume_publication(vault, config_path))
        elif args.command == "show":
            effective = validate_config(config_path)
            _print_json(
                {
                    "config_path": str(config_path),
                    "daily_papers": effective["daily_papers"],
                    "automation": {
                        field: effective["automation"][field]
                        for field in sorted(EDITABLE_AUTOMATION_FIELDS)
                    },
                }
            )
        elif args.command == "validate":
            validate_config(config_path)
            _print_json({"status": "valid", "config_path": str(config_path)})
        elif args.command == "plan":
            _print_json(build_plan(config_path, args.patch))
        elif args.command == "apply":
            _print_json(apply_plan(vault, config_path, args.patch))
    except ActiveRunError as exc:
        print(
            json.dumps(
                {
                    "version": 1,
                    "status": "blocked",
                    "code": "active-run",
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    except (ConfigError, OSError) as exc:
        print(
            json.dumps(
                {
                    "version": 1,
                    "status": "blocked",
                    "code": "invalid-configuration",
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
