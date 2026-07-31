#!/usr/bin/env python3
"""Coordinate daily-paper runs through atomic Git branch updates."""

from __future__ import annotations

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import os
import socket
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config_schema
import task_state
from run_lifecycle import (
    DAILY_WORKFLOW_CONTRACT,
    MAX_ARTIFACT_BYTES,
    MAX_MANIFEST_BYTES,
    ConfigurationMismatch,
    ContractMismatch,
    Interruption,
    LifecycleError,
    RunLifecycle,
)
from safe_io import (
    DocumentTooLargeError,
    SafeIOError,
    atomic_write_bytes,
    encode_json_value,
    load_json_object,
    parse_json_object,
    read_regular_bytes,
)
from safe_git import (
    GitCommandResult,
    SafeGitError,
    inspect_repository,
    read_git_blob,
    repository_dirty_paths,
    run_git_command,
    run_git_program,
    verify_index_versions,
)
from safe_path import SafePathError, relative_posix_path
from user_config import (
    DEFAULT_CONFIG,
    clear_config_cache,
    daily_papers_dir,
    load_user_config,
    obsidian_vault_path,
    repository_config,
)


STATE_VERSION = task_state.STATE_VERSION
TASK_NAME = task_state.TASK_NAME
FIXED_VAULT_URL = "git@github.com:haoz0206/dailypaper-vault.git"
FIXED_REMOTE = "origin"
FIXED_BRANCH = "main"
FIXED_TASK_STATE_FILE = ".dailypaper/tasks/daily-papers.json"
FIXED_TIMEZONE = "Asia/Shanghai"
COMMIT_NAME = "dailypaper automation"
COMMIT_EMAIL = "dailypaper@localhost"
BOOTSTRAP_JOURNAL_VERSION = 1
BOOTSTRAP_JOURNAL_RELATIVE = "dailypaper/bootstrap-v1.json"
BOOTSTRAP_COMMIT_MESSAGE = "dailypaper: bootstrap vault"
BOOTSTRAP_COMMIT_DATE = "2000-01-01T00:00:00+00:00"
MAX_BOOTSTRAP_FILE_BYTES = config_schema.MAX_CONFIG_BYTES
MAX_BOOTSTRAP_JOURNAL_BYTES = 3 * 1024 * 1024


class CoordinationError(RuntimeError):
    """A safe, expected coordination failure."""

    def __init__(self, status: str, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.status = status
        self.exit_code = exit_code

    def as_dict(self) -> dict:
        return {"status": self.status, "message": str(self)}


def _git(
    vault: Path,
    *args: str,
    check: bool = True,
) -> GitCommandResult:
    try:
        result = run_git_command(vault, *args)
    except SafeGitError as exc:
        raise CoordinationError("git-error", str(exc)) from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CoordinationError(
            "git-error",
            f"git {' '.join(args)} failed: {detail}",
        )
    return result


def _git_output(vault: Path, *args: str) -> str:
    return _git(vault, *args).stdout.strip()


def _safe_relative_path(value: str) -> Path:
    try:
        candidate = relative_posix_path(
            value,
            label="Vault coordination path",
        )
    except SafePathError as exc:
        raise CoordinationError(
            "invalid-config",
            f"Vault coordination path must be relative: {value}",
        ) from exc
    return Path(*candidate.parts)


def _task_relative_path() -> Path:
    configured = str(repository_config()["task_state_file"])
    if configured != FIXED_TASK_STATE_FILE:
        raise CoordinationError(
            "invalid-config",
            f"Task state file must remain fixed to {FIXED_TASK_STATE_FILE}",
        )
    return _safe_relative_path(FIXED_TASK_STATE_FILE)


def _fixed_task_relative_path() -> Path:
    """Return the coordination path without consulting shared configuration."""
    return _safe_relative_path(FIXED_TASK_STATE_FILE)


def configuration_fingerprint() -> str:
    """Return the output-affecting configuration identity for a DailyPaper Run."""
    return config_schema.configuration_fingerprint(load_user_config())


# Compatibility for existing callers while the public name is adopted.
_config_fingerprint = configuration_fingerprint


def _read_state(path: Path) -> dict | None:
    try:
        return task_state.read_task_state_file(path)
    except task_state.TaskStateError as exc:
        raise CoordinationError(
            "invalid-state",
            str(exc),
        ) from exc


def _validate_state(data: dict, source: str) -> dict:
    try:
        return task_state.validate_task_state(data, source=source)
    except task_state.TaskStateError as exc:
        raise CoordinationError(
            "invalid-state",
            str(exc),
        ) from exc


def _state_at_ref(vault: Path, ref: str, task_relative: Path) -> dict | None:
    object_name = f"{ref}:{task_relative.as_posix()}"
    try:
        raw_state = read_git_blob(
            vault,
            object_name,
            max_bytes=task_state.MAX_TASK_STATE_BYTES,
        )
        if raw_state is None:
            return None
        return task_state.parse_task_state(
            raw_state,
            source=object_name,
        )
    except SafeGitError as exc:
        raise CoordinationError("git-error", str(exc)) from exc
    except task_state.TaskStateError as exc:
        raise CoordinationError(
            "invalid-state",
            str(exc),
        ) from exc


def _write_state(path: Path, state: dict) -> None:
    try:
        task_state.write_task_state_file(path, state)
    except task_state.TaskStateError as exc:
        raise CoordinationError(
            "invalid-state",
            str(exc),
        ) from exc


def _manifest_data(path: Path) -> dict:
    try:
        data = load_json_object(
            path,
            max_bytes=MAX_MANIFEST_BYTES,
            label="Run Manifest",
        )
        if data is None:
            raise SafeIOError(f"Run Manifest file does not exist: {path}")
    except SafeIOError as exc:
        raise CoordinationError(
            "invalid-manifest",
            f"Could not read Run Manifest {path}: {exc}",
        ) from exc
    return data


def _manifest_window_days(manifest: dict) -> int:
    """Return the frozen Run window, normalizing pre-release one-day Runs."""
    value = manifest.get("window_days", 1)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 31:
        raise CoordinationError(
            "invalid-manifest",
            "Run Manifest window_days must be an integer from 1 to 31.",
        )
    return value


def _open_run(manifest_path: Path) -> tuple[dict, RunLifecycle]:
    """Open the canonical v2 Run Manifest."""
    raw = _manifest_data(manifest_path)
    if raw.get("version") != 2:
        raise CoordinationError(
            "invalid-manifest",
            f"Unsupported Run Manifest version: {raw.get('version')}",
        )

    try:
        lifecycle = RunLifecycle.open(
            manifest_path,
            contract=DAILY_WORKFLOW_CONTRACT,
            configuration_fingerprint=configuration_fingerprint(),
            expected_vault=obsidian_vault_path().expanduser().resolve(),
            expected_run_id=manifest_path.parent.name,
        )
    except ConfigurationMismatch as exc:
        raise CoordinationError("config-conflict", str(exc)) from exc
    except ContractMismatch as exc:
        raise CoordinationError("contract-conflict", str(exc)) from exc
    except (TypeError, ValueError, LifecycleError) as exc:
        raise CoordinationError(
            "invalid-manifest",
            f"Could not open v2 Run Manifest: {exc}",
        ) from exc
    return lifecycle.snapshot().as_dict(), lifecycle


def _record_acquisition(
    lifecycle: RunLifecycle,
    *,
    lock_commit: str,
    remote: str,
    branch: str,
) -> None:
    lifecycle.record_acquisition(
        acquisition_commit=lock_commit,
        remote=remote,
        branch=branch,
    )


def _bootstrap_config() -> dict:
    effective = copy.deepcopy(DEFAULT_CONFIG)
    effective["runtime"]["timezone"] = FIXED_TIMEZONE
    effective["repository"]["url"] = FIXED_VAULT_URL
    effective["repository"]["remote"] = FIXED_REMOTE
    effective["repository"]["branch"] = FIXED_BRANCH
    effective["repository"]["task_state_file"] = FIXED_TASK_STATE_FILE
    return config_schema.materialize_shared_config(effective, effective)


def _bootstrap_git_dir(vault: Path) -> Path:
    value = Path(
        _git_output(vault, "rev-parse", "--git-path", "dailypaper")
    )
    candidate = value if value.is_absolute() else vault / value
    if candidate.is_symlink():
        raise CoordinationError(
            "invalid-bootstrap-journal",
            f"Bootstrap state directory must not be a symlink: {candidate}",
        )
    return candidate.parent.resolve() / candidate.name


@contextmanager
def _bootstrap_lock(vault: Path):
    state_dir = _bootstrap_git_dir(vault)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CoordinationError(
            "invalid-bootstrap-journal",
            f"Bootstrap state directory cannot be created safely: {state_dir}",
        ) from exc
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise CoordinationError(
            "invalid-bootstrap-journal",
            f"Bootstrap state path is not a regular directory: {state_dir}",
        )
    lock_path = state_dir / "bootstrap.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise CoordinationError(
            "invalid-bootstrap-journal",
            f"Bootstrap lock cannot be opened safely: {lock_path}",
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CoordinationError(
                "invalid-bootstrap-journal",
                f"Bootstrap lock is not a regular file: {lock_path}",
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield state_dir
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bootstrap_write(path: Path, content: bytes, mode: int) -> None:
    if path.is_symlink():
        raise CoordinationError(
            "bootstrap-path-conflict",
            f"Bootstrap path must not be a symlink: {path}",
        )
    try:
        atomic_write_bytes(
            path,
            content,
            mode=mode,
            label="Vault bootstrap file",
        )
    except SafeIOError as exc:
        raise CoordinationError("bootstrap-path-conflict", str(exc)) from exc


def _bounded_bootstrap_read(path: Path) -> tuple[bytes, int] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CoordinationError(
            "bootstrap-path-conflict",
            f"Cannot safely open bootstrap path: {path}",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CoordinationError(
                "bootstrap-path-conflict",
                f"Bootstrap path is not a regular file: {path}",
            )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            content = handle.read(MAX_BOOTSTRAP_FILE_BYTES + 1)
    except OSError as exc:
        raise CoordinationError(
            "bootstrap-path-conflict",
            f"Cannot read bootstrap path: {path}",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(content) > MAX_BOOTSTRAP_FILE_BYTES:
        raise CoordinationError(
            "bootstrap-path-conflict",
            f"Bootstrap path exceeds the safety limit: {path}",
        )
    return content, stat.S_IMODE(metadata.st_mode)


def _bootstrap_before_record(value: tuple[bytes, int] | None) -> dict:
    if value is None:
        return {"exists": False, "mode": None, "content": None}
    content, mode = value
    return {
        "exists": True,
        "mode": mode,
        "content": base64.b64encode(content).decode("ascii"),
    }


def _bootstrap_record_bytes(record: dict, *, label: str) -> tuple[bytes, int] | None:
    if set(record) != {"exists", "mode", "content"}:
        raise CoordinationError(
            "invalid-bootstrap-journal",
            f"{label} journal record has unsupported fields.",
        )
    exists = record.get("exists")
    mode = record.get("mode")
    encoded = record.get("content")
    if not isinstance(exists, bool):
        raise CoordinationError(
            "invalid-bootstrap-journal",
            f"{label}.exists must be boolean.",
        )
    if not exists:
        if mode is not None or encoded is not None:
            raise CoordinationError(
                "invalid-bootstrap-journal",
                f"{label} absent record must have null mode/content.",
            )
        return None
    if (
        isinstance(mode, bool)
        or not isinstance(mode, int)
        or mode < 0
        or mode > 0o777
        or not isinstance(encoded, str)
    ):
        raise CoordinationError(
            "invalid-bootstrap-journal",
            f"{label} has invalid mode/content.",
        )
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise CoordinationError(
            "invalid-bootstrap-journal",
            f"{label} content is not canonical base64.",
        ) from exc
    if len(content) > MAX_BOOTSTRAP_FILE_BYTES:
        raise CoordinationError(
            "invalid-bootstrap-journal",
            f"{label} content exceeds the safety limit.",
        )
    return content, mode


def _load_bootstrap_journal(path: Path) -> dict | None:
    try:
        raw = read_regular_bytes(
            path,
            max_bytes=MAX_BOOTSTRAP_JOURNAL_BYTES,
            required=False,
            label="Bootstrap journal",
        )
        if raw is None:
            return None
        value = parse_json_object(
            raw,
            max_bytes=MAX_BOOTSTRAP_JOURNAL_BYTES,
            label="Bootstrap journal",
        )
    except SafeIOError as exc:
        raise CoordinationError(
            "invalid-bootstrap-journal",
            str(exc),
        ) from exc
    required = {"version", "base_head", "before"}
    if set(value) != required:
        raise CoordinationError(
            "invalid-bootstrap-journal",
            "Bootstrap journal has unsupported fields.",
        )
    if value.get("version") != BOOTSTRAP_JOURNAL_VERSION:
        raise CoordinationError(
            "invalid-bootstrap-journal",
            "Unsupported Bootstrap journal version.",
        )
    base_head = value.get("base_head")
    if base_head is not None and (
        not isinstance(base_head, str)
        or len(base_head) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in base_head)
    ):
        raise CoordinationError(
            "invalid-bootstrap-journal",
            "Bootstrap journal base_head is invalid.",
        )
    before = value.get("before")
    if not isinstance(before, dict) or set(before) != {
        ".gitignore",
        ".dailypaper/config.json",
    }:
        raise CoordinationError(
            "invalid-bootstrap-journal",
            "Bootstrap journal before map is invalid.",
        )
    _bootstrap_record_bytes(before[".gitignore"], label=".gitignore")
    _bootstrap_record_bytes(
        before[".dailypaper/config.json"],
        label=".dailypaper/config.json",
    )
    return value


def _write_bootstrap_journal(path: Path, value: dict) -> None:
    try:
        encoded = encode_json_value(
            value,
            max_bytes=MAX_BOOTSTRAP_JOURNAL_BYTES,
            label="Bootstrap journal",
        )
    except DocumentTooLargeError as exc:
        raise CoordinationError(
            "invalid-bootstrap-journal",
            "Bootstrap journal exceeds the safety limit.",
        ) from exc
    except SafeIOError as exc:
        raise CoordinationError(
            "invalid-bootstrap-journal",
            str(exc),
        ) from exc
    _atomic_bootstrap_write(path, encoded, 0o600)


def _clear_bootstrap_journal(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise CoordinationError(
            "bootstrap-journal-cleanup-failed",
            f"Bootstrap succeeded but journal cleanup failed: {path}",
            exit_code=3,
        ) from exc


def _validate_bootstrap_config_bytes(content: bytes) -> None:
    try:
        config = config_schema.parse_json_object(
            content,
            label="Bootstrap Vault configuration",
        )
        defaults = copy.deepcopy(DEFAULT_CONFIG)
        defaults["repository"]["url"] = FIXED_VAULT_URL
        config_schema.validate_shared_config(
            config,
            defaults,
            defaults,
            allow_legacy=True,
        )
    except config_schema.ConfigurationError as exc:
        raise CoordinationError("invalid-config", str(exc)) from exc
    repository = config.get("repository", {})
    if not isinstance(repository, dict):
        raise CoordinationError(
            "invalid-config",
            "Existing Vault config repository must be an object.",
        )
    expected = _bootstrap_config()["repository"]
    for key in (
        "url",
        "remote",
        "branch",
        "task_state_file",
        "pull_before_run",
        "require_clean",
        "coordination_enabled",
        "same_day_policy",
    ):
        if key in repository and repository[key] != expected[key]:
            raise CoordinationError(
                "invalid-config",
                f"Existing Vault config has incompatible repository.{key}",
            )


def _bootstrap_after_bytes(
    before: dict,
) -> dict[str, tuple[bytes, int]]:
    gitignore_record = _bootstrap_record_bytes(
        before[".gitignore"],
        label=".gitignore",
    )
    if gitignore_record is None:
        gitignore_before = b""
        gitignore_mode = 0o644
    else:
        gitignore_before, gitignore_mode = gitignore_record
    try:
        gitignore_text = gitignore_before.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoordinationError(
            "bootstrap-path-conflict",
            ".gitignore must be valid UTF-8.",
        ) from exc
    if ".dailypaper/runs/" not in gitignore_text.splitlines():
        separator = (
            b""
            if not gitignore_before or gitignore_before.endswith(b"\n")
            else b"\n"
        )
        gitignore_after = (
            gitignore_before
            + separator
            + b"# Local daily-paper run state\n.dailypaper/runs/\n"
        )
    else:
        gitignore_after = gitignore_before

    config_record = _bootstrap_record_bytes(
        before[".dailypaper/config.json"],
        label=".dailypaper/config.json",
    )
    if config_record is None:
        config_after = (
            json.dumps(
                _bootstrap_config(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        config_mode = 0o644
    else:
        config_after, config_mode = config_record
    _validate_bootstrap_config_bytes(config_after)
    return {
        ".gitignore": (gitignore_after, gitignore_mode),
        ".dailypaper/config.json": (config_after, config_mode),
    }


def _bootstrap_head(vault: Path) -> str | None:
    result = _git(vault, "rev-parse", "--verify", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _remote_branch_head(vault: Path) -> str | None:
    result = _git(
        vault,
        "ls-remote",
        "--heads",
        FIXED_REMOTE,
        f"refs/heads/{FIXED_BRANCH}",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CoordinationError(
            "git-error",
            f"Could not inspect remote branch: {detail}",
            exit_code=3,
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) != 1:
        raise CoordinationError(
            "git-error",
            "Remote branch inspection returned ambiguous results.",
        )
    object_id, separator, ref = lines[0].partition("\t")
    if not separator or ref != f"refs/heads/{FIXED_BRANCH}":
        raise CoordinationError(
            "git-error",
            "Remote branch inspection returned an invalid ref.",
        )
    return object_id


def _managed_index_flags(vault: Path, relative: str) -> None:
    result = _git(
        vault,
        "ls-files",
        "-v",
        "--",
        relative,
        check=False,
    )
    for line in result.stdout.splitlines():
        if line and (line[0].islower() or line[0] == "S"):
            raise CoordinationError(
                "bootstrap-path-conflict",
                f"Bootstrap path has hidden index flags: {relative}",
            )


def _bootstrap_commit_object(
    vault: Path,
    *,
    tree: str,
    base_head: str | None,
) -> str:
    command = ["commit-tree", tree]
    if base_head is not None:
        command.extend(["-p", base_head])
    command.extend(["-m", BOOTSTRAP_COMMIT_MESSAGE])
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": COMMIT_NAME,
            "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
            "GIT_AUTHOR_DATE": BOOTSTRAP_COMMIT_DATE,
            "GIT_COMMITTER_NAME": COMMIT_NAME,
            "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
            "GIT_COMMITTER_DATE": BOOTSTRAP_COMMIT_DATE,
        }
    )
    try:
        result = run_git_command(
            vault,
            *command,
            environment=environment,
        )
    except SafeGitError as exc:
        raise CoordinationError("git-error", str(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CoordinationError(
            "git-error",
            f"Could not create deterministic bootstrap commit: {detail}",
        )
    return result.stdout.strip()


def _bootstrap_commit(
    vault: Path,
    *,
    base_head: str | None,
) -> str:
    tree = _git_output(vault, "write-tree")
    commit = _bootstrap_commit_object(
        vault,
        tree=tree,
        base_head=base_head,
    )
    expected = base_head or ("0" * len(commit))
    update = _git(
        vault,
        "update-ref",
        f"refs/heads/{FIXED_BRANCH}",
        commit,
        expected,
        check=False,
    )
    if update.returncode != 0:
        detail = update.stderr.strip() or update.stdout.strip()
        raise CoordinationError(
            "bootstrap-local-raced",
            f"Local branch changed during bootstrap: {detail}",
            exit_code=3,
        )
    return commit


def _validate_bootstrap_commit(
    vault: Path,
    *,
    commit: str,
    base_head: str | None,
    expected_paths: set[str],
    after: dict[str, tuple[bytes, int]],
) -> None:
    parents = _git_output(vault, "rev-list", "--parents", "-n", "1", commit).split()
    expected_parents = [commit] + ([base_head] if base_head else [])
    if parents != expected_parents:
        raise CoordinationError(
            "bootstrap-commit-conflict",
            "Local HEAD is not the recorded bootstrap candidate.",
        )
    changed = set(
        _git_output(
            vault,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            commit,
        ).splitlines()
    )
    if changed != expected_paths:
        raise CoordinationError(
            "bootstrap-commit-conflict",
            "Bootstrap candidate changed unexpected paths.",
        )
    for relative in expected_paths:
        expected_content = after[relative][0]
        try:
            blob = read_git_blob(
                vault,
                f"{commit}:{relative}",
                max_bytes=max(len(expected_content), 1),
                missing_ok=False,
            )
        except SafeGitError as exc:
            raise CoordinationError("git-error", str(exc)) from exc
        if blob is None:
            raise CoordinationError(
                "bootstrap-commit-conflict",
                f"Bootstrap candidate is missing {relative}.",
            )
        if blob != expected_content:
            raise CoordinationError(
                "bootstrap-commit-conflict",
            f"Bootstrap candidate content differs for {relative}.",
            )
        tree_entry = _git_output(
            vault,
            "ls-tree",
            commit,
            "--",
            relative,
        )
        expected_mode = "100755" if after[relative][1] & 0o111 else "100644"
        if not tree_entry.startswith(f"{expected_mode} blob "):
            raise CoordinationError(
                "bootstrap-commit-conflict",
                f"Bootstrap candidate mode differs for {relative}.",
            )
    tree = _git_output(vault, "rev-parse", f"{commit}^{{tree}}")
    deterministic = _bootstrap_commit_object(
        vault,
        tree=tree,
        base_head=base_head,
    )
    if deterministic != commit:
        raise CoordinationError(
            "bootstrap-commit-conflict",
            "Bootstrap candidate metadata is not deterministic.",
        )


def _bootstrap_failpoint(_name: str) -> None:
    """Private fault-injection seam used by crash recovery tests."""


def _validate_bootstrap_parents(vault: Path) -> None:
    config_parent = vault / ".dailypaper"
    if config_parent.is_symlink():
        raise CoordinationError(
            "bootstrap-path-conflict",
            f"Bootstrap directory must not be a symlink: {config_parent}",
        )
    if config_parent.exists() and not config_parent.is_dir():
        raise CoordinationError(
            "bootstrap-path-conflict",
            f"Bootstrap directory is not a directory: {config_parent}",
        )


def _bootstrap_record_matches(
    current: tuple[bytes, int] | None,
    expected: tuple[bytes, int] | None,
) -> bool:
    return current == expected


def _bootstrap_changed_paths(
    before: dict,
    after: dict[str, tuple[bytes, int]],
) -> set[str]:
    return {
        relative
        for relative, target in after.items()
        if not _bootstrap_record_matches(
            _bootstrap_record_bytes(before[relative], label=relative),
            target,
        )
    }


def _bootstrap_verify_managed_worktree(
    vault: Path,
    *,
    before: dict,
    after: dict[str, tuple[bytes, int]],
) -> None:
    for relative, target in after.items():
        current = _bounded_bootstrap_read(vault / relative)
        original = _bootstrap_record_bytes(before[relative], label=relative)
        if not (
            _bootstrap_record_matches(current, original)
            or _bootstrap_record_matches(current, target)
        ):
            raise CoordinationError(
                "bootstrap-path-conflict",
                (
                    f"Bootstrap path changed outside the recoverable "
                    f"transaction: {relative}"
                ),
            )


def _bootstrap_verify_no_unrelated_changes(
    vault: Path,
    *,
    allowed: set[str],
) -> None:
    unexpected = dirty_paths(vault) - allowed
    if unexpected:
        raise CoordinationError(
            "unexpected-changes",
            "Vault contains changes outside bootstrap: "
            + ", ".join(sorted(unexpected)),
        )


def _bootstrap_fresh_base(vault: Path, *, allowed: set[str]) -> str | None:
    """Synchronize a journal-free clone and return its immutable base HEAD."""
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
    if staged:
        raise CoordinationError(
            "dirty-worktree",
            "Vault index must be clean before starting bootstrap.",
        )

    tracked_dirty = set(
        _git_output(
            vault,
            "-c",
            "core.quotePath=false",
            "diff",
            "--name-only",
        ).splitlines()
    )
    if tracked_dirty:
        raise CoordinationError(
            "dirty-worktree",
            "Tracked Vault files must be clean before starting bootstrap.",
        )
    _bootstrap_verify_no_unrelated_changes(vault, allowed=allowed)

    local_head = _bootstrap_head(vault)
    remote_head = _remote_branch_head(vault)
    if remote_head is not None and local_head != remote_head:
        pull = _git(
            vault,
            "pull",
            "--ff-only",
            FIXED_REMOTE,
            FIXED_BRANCH,
            check=False,
        )
        if pull.returncode != 0:
            detail = pull.stderr.strip() or pull.stdout.strip()
            raise CoordinationError(
                "bootstrap-sync-failed",
                f"Could not fast-forward Vault before bootstrap: {detail}",
                exit_code=3,
            )
        local_head = _bootstrap_head(vault)
        if local_head != remote_head:
            raise CoordinationError(
                "bootstrap-sync-failed",
                "Vault HEAD does not match the inspected remote after pull.",
                exit_code=3,
            )
        _bootstrap_verify_no_unrelated_changes(vault, allowed=allowed)
    elif remote_head is None and local_head is not None:
        # An empty remote may legitimately be initialized from a local Vault.
        pass
    return local_head


def _bootstrap_postconditions(
    vault: Path,
    *,
    expected_head: str | None,
) -> None:
    local_head = _bootstrap_head(vault)
    remote_head = _remote_branch_head(vault)
    if local_head != expected_head or remote_head != expected_head:
        raise CoordinationError(
            "bootstrap-verification-failed",
            (
                "Bootstrap did not converge local and remote HEAD "
                f"(local={local_head}, remote={remote_head}, "
                f"expected={expected_head})."
            ),
            exit_code=3,
        )
    _ensure_clean(vault)
    config = _bounded_bootstrap_read(vault / ".dailypaper/config.json")
    if config is None:
        raise CoordinationError(
            "bootstrap-verification-failed",
            "Bootstrap configuration is missing after publication.",
            exit_code=3,
        )
    _validate_bootstrap_config_bytes(config[0])


def bootstrap_vault(vault: Path) -> dict:
    """Ensure the portable Vault bootstrap exists, resuming interrupted work."""
    vault = vault.expanduser().resolve()
    _fixed_repository_identity(vault)
    allowed = {".gitignore", ".dailypaper/config.json"}

    with _bootstrap_lock(vault) as state_dir:
        _validate_bootstrap_parents(vault)
        journal_path = state_dir / Path(BOOTSTRAP_JOURNAL_RELATIVE).name
        journal = _load_bootstrap_journal(journal_path)
        if journal is None:
            base_head = _bootstrap_fresh_base(vault, allowed=allowed)
            before = {
                relative: _bootstrap_before_record(
                    _bounded_bootstrap_read(vault / relative)
                )
                for relative in sorted(allowed)
            }
            journal = {
                "version": BOOTSTRAP_JOURNAL_VERSION,
                "base_head": base_head,
                "before": before,
            }
            # The journal becomes durable before any worktree or index mutation.
            _write_bootstrap_journal(journal_path, journal)
            _bootstrap_failpoint("after-journal")

        base_head = journal["base_head"]
        before = journal["before"]
        after = _bootstrap_after_bytes(before)
        changed_paths = _bootstrap_changed_paths(before, after)
        _bootstrap_verify_managed_worktree(
            vault,
            before=before,
            after=after,
        )
        _bootstrap_verify_no_unrelated_changes(vault, allowed=allowed)
        for relative in allowed:
            _managed_index_flags(vault, relative)

        local_head = _bootstrap_head(vault)
        if not changed_paths:
            if local_head != base_head:
                raise CoordinationError(
                    "bootstrap-local-raced",
                    "Local HEAD changed while resuming a no-op bootstrap.",
                    exit_code=3,
                )
            remote_head = _remote_branch_head(vault)
            if remote_head != base_head:
                raise CoordinationError(
                    "bootstrap-remote-raced",
                    "Remote HEAD changed while resuming bootstrap.",
                    exit_code=3,
                )
            _ensure_clean(vault)
            _clear_bootstrap_journal(journal_path)
            _bootstrap_failpoint("after-journal-delete")
            return {
                "status": "already-bootstrapped",
                "vault": str(vault),
                "branch": FIXED_BRANCH,
            }

        candidate: str
        if local_head == base_head:
            for relative in sorted(changed_paths):
                _validate_bootstrap_parents(vault)
                target, mode = after[relative]
                _atomic_bootstrap_write(vault / relative, target, mode)
                _bootstrap_failpoint(
                    "after-gitignore-replace"
                    if relative == ".gitignore"
                    else "after-config-replace"
                )
            _bootstrap_verify_no_unrelated_changes(vault, allowed=allowed)
            _git(vault, "add", "--", ".gitignore")
            _git(
                vault,
                "add",
                "--force",
                "--",
                ".dailypaper/config.json",
            )
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
            if staged != changed_paths:
                raise CoordinationError(
                    "bootstrap-index-conflict",
                    (
                        "Bootstrap index differs from its recoverable change "
                        f"set: expected={sorted(changed_paths)}, "
                        f"actual={sorted(staged)}"
                    ),
                )
            _bootstrap_failpoint("after-stage")
            candidate = _bootstrap_commit(vault, base_head=base_head)
            _bootstrap_failpoint("after-commit")
        else:
            if local_head is None:
                raise CoordinationError(
                    "bootstrap-local-raced",
                    "Local branch disappeared while resuming bootstrap.",
                    exit_code=3,
                )
            candidate = local_head

        _validate_bootstrap_commit(
            vault,
            commit=candidate,
            base_head=base_head,
            expected_paths=changed_paths,
            after=after,
        )
        _bootstrap_verify_managed_worktree(
            vault,
            before=before,
            after=after,
        )
        _ensure_clean(vault)

        remote_head = _remote_branch_head(vault)
        if remote_head not in {base_head, candidate}:
            raise CoordinationError(
                "bootstrap-remote-raced",
                (
                    "Remote HEAD changed outside the bootstrap transaction: "
                    f"{remote_head}"
                ),
                exit_code=3,
            )
        push = None
        if remote_head != candidate:
            push = _git(
                vault,
                "push",
                "--set-upstream",
                FIXED_REMOTE,
                f"{candidate}:refs/heads/{FIXED_BRANCH}",
                check=False,
            )
            _bootstrap_failpoint("after-push-call-before-verify")
        observed_remote = _remote_branch_head(vault)
        if observed_remote != candidate:
            detail = ""
            if push is not None:
                detail = push.stderr.strip() or push.stdout.strip()
            raise CoordinationError(
                "bootstrap-push-failed",
                (
                    "Bootstrap commit was preserved locally; remote did not "
                    f"accept it{f': {detail}' if detail else '.'}"
                ),
                exit_code=3,
            )
        _bootstrap_failpoint("after-remote-verify")
        _bootstrap_postconditions(vault, expected_head=candidate)
        _clear_bootstrap_journal(journal_path)
        _bootstrap_failpoint("after-journal-delete")
        return {
            "status": "bootstrapped",
            "vault": str(vault),
            "branch": FIXED_BRANCH,
            "bootstrap_commit": candidate,
        }


def _repository_identity(vault: Path) -> tuple[dict, str]:
    config = repository_config()
    if not config.get("coordination_enabled", True):
        raise CoordinationError(
            "coordination-disabled",
            "Vault coordination is disabled in repository configuration.",
        )

    configured_url = str(config["url"]).rstrip("/")
    if configured_url != FIXED_VAULT_URL:
        raise CoordinationError(
            "invalid-config",
            f"Repository URL must remain fixed to {FIXED_VAULT_URL}",
        )
    remote = str(config.get("remote", FIXED_REMOTE))
    branch = str(config.get("branch", FIXED_BRANCH))
    if remote != FIXED_REMOTE or branch != FIXED_BRANCH:
        raise CoordinationError(
            "invalid-config",
            f"Repository target must remain {FIXED_REMOTE}/{FIXED_BRANCH}",
        )
    if not config.get("pull_before_run", True):
        raise CoordinationError(
            "invalid-config",
            "pull_before_run must remain enabled for coordinated runs.",
        )
    if not config.get("require_clean", True):
        raise CoordinationError(
            "invalid-config",
            "require_clean must remain enabled for coordinated runs.",
        )
    if config.get("same_day_policy", "skip") != "skip":
        raise CoordinationError(
            "invalid-config",
            "same_day_policy must remain 'skip' for idempotent daily runs.",
        )

    _fixed_repository_identity(vault)
    return config, remote


def _fixed_repository_identity(vault: Path) -> tuple[str, str]:
    """Validate the immutable Git endpoint without reading shared config."""
    vault = vault.expanduser().resolve()
    try:
        snapshot = inspect_repository(vault, remote=FIXED_REMOTE)
    except (SafeGitError, ValueError) as exc:
        raise CoordinationError("git-error", str(exc)) from exc
    if snapshot.root != vault:
        raise CoordinationError(
            "wrong-vault",
            f"Configured Vault is not the Git root: {vault}",
        )

    actual_url = snapshot.remote_url.rstrip("/")
    if actual_url != FIXED_VAULT_URL:
        raise CoordinationError(
            "wrong-remote",
            f"Expected {FIXED_REMOTE}={FIXED_VAULT_URL}, found {actual_url}",
        )

    current_branch = snapshot.branch
    if current_branch != FIXED_BRANCH:
        raise CoordinationError(
            "wrong-branch",
            (
                f"Expected branch {FIXED_BRANCH}, "
                f"found {current_branch or 'detached HEAD'}"
            ),
        )
    return FIXED_REMOTE, FIXED_BRANCH


def _ensure_clean(vault: Path) -> None:
    if dirty_paths(vault):
        raise CoordinationError(
            "dirty-worktree",
            "Vault must be clean before acquiring the daily task.",
        )


def _sync_before_run(vault: Path, config: dict, remote: str) -> str:
    branch = str(config["branch"])
    if config.get("require_clean", True):
        _ensure_clean(vault)
    if config.get("pull_before_run", True):
        _git(vault, "pull", "--ff-only", remote, branch)
    if config.get("require_clean", True):
        _ensure_clean(vault)
    return _git_output(vault, "rev-parse", "HEAD")


def inspect_task_state(vault: Path) -> dict:
    """Fetch and inspect the remote task state without changing the worktree."""
    vault = vault.expanduser().resolve()
    remote, branch = _fixed_repository_identity(vault)
    remote_head = _remote_branch_head(vault)
    if remote_head is None:
        return {
            "status": "inspected",
            "vault": str(vault),
            "remote": remote,
            "branch": branch,
            "remote_head": None,
            "task_state": None,
        }
    _git(vault, "fetch", remote, branch)
    remote_ref = f"refs/remotes/{remote}/{branch}"
    fetched_head = _git_output(vault, "rev-parse", remote_ref)
    if fetched_head != remote_head:
        raise CoordinationError(
            "remote-advanced",
            "Remote branch changed while inspecting Task State.",
            exit_code=3,
        )
    task_relative = _fixed_task_relative_path()
    return {
        "status": "inspected",
        "vault": str(vault),
        "remote": remote,
        "branch": branch,
        "remote_head": remote_head,
        "task_state": _state_at_ref(vault, remote_ref, task_relative),
    }


def prepare_cancel(vault: Path, expected_run_id: str) -> dict:
    """Bind a user cancellation decision to one remote head and running Run."""
    if not expected_run_id.strip():
        raise ValueError("expected_run_id must not be empty")
    inspected = inspect_task_state(vault)
    state = inspected["task_state"]
    if state is None:
        raise CoordinationError("no-task", "No remote DailyPaper task exists.")
    if state.get("status") != "running":
        raise CoordinationError(
            "not-running",
            f"Remote DailyPaper task is {state.get('status')!r}, not running.",
        )
    if state.get("run_id") != expected_run_id:
        raise CoordinationError(
            "run-changed",
            (
                f"Expected running Run {expected_run_id}, found "
                f"{state.get('run_id')}"
            ),
            exit_code=3,
        )
    return {
        "version": 1,
        "operation": "cancel-dailypaper-run",
        "vault": inspected["vault"],
        "remote": inspected["remote"],
        "branch": inspected["branch"],
        "remote_head": inspected["remote_head"],
        "expected_run_id": expected_run_id,
        "task_state": state,
    }


def _cancellation_candidate(
    vault: Path,
    *,
    expected_url: str,
    branch: str,
    expected_head: str,
    state: dict,
    task_relative: Path,
) -> tuple[bool, str, str]:
    with tempfile.TemporaryDirectory(prefix="dailypaper-cancel-") as temp_dir:
        candidate = Path(temp_dir) / "vault"
        try:
            clone = run_git_program(
                "clone",
                "--shared",
                "--no-checkout",
                str(vault),
                str(candidate),
            )
        except SafeGitError as exc:
            raise CoordinationError("git-error", str(exc)) from exc
        if clone.returncode != 0:
            detail = clone.stderr.strip() or clone.stdout.strip()
            raise CoordinationError(
                "git-error",
                f"Could not create cancellation candidate: {detail}",
            )
        _git(candidate, "remote", "set-url", "origin", expected_url)
        _git(candidate, "fetch", "origin", branch)
        fetched_head = _git_output(
            candidate,
            "rev-parse",
            f"refs/remotes/origin/{branch}",
        )
        if fetched_head != expected_head:
            raise CoordinationError(
                "cancel-stale",
                (
                    f"Remote moved from confirmed head {expected_head} "
                    f"to {fetched_head}."
                ),
                exit_code=3,
            )
        _git(candidate, "checkout", "--detach", expected_head)
        _write_state(candidate / task_relative, state)
        _git(candidate, "add", "--", task_relative.as_posix())
        _git(
            candidate,
            "-c",
            f"user.name={COMMIT_NAME}",
            "-c",
            f"user.email={COMMIT_EMAIL}",
            "commit",
            "-m",
            f"dailypaper: cancel {state['target_date']} ({state['run_id']})",
        )
        cancellation_commit = _git_output(candidate, "rev-parse", "HEAD")
        push = _git(
            candidate,
            "push",
            "origin",
            f"HEAD:refs/heads/{branch}",
            check=False,
        )
        detail = push.stderr.strip() or push.stdout.strip()
        return push.returncode == 0, cancellation_commit, detail


def cancel(proposal: dict) -> dict:
    """CAS-cancel one explicitly confirmed remote Run without local cleanup."""
    required = {
        "version",
        "operation",
        "vault",
        "remote",
        "branch",
        "remote_head",
        "expected_run_id",
    }
    missing = sorted(required - set(proposal))
    if missing:
        raise CoordinationError(
            "invalid-proposal",
            "Cancellation proposal is missing: " + ", ".join(missing),
        )
    if (
        proposal["version"] != 1
        or proposal["operation"] != "cancel-dailypaper-run"
    ):
        raise CoordinationError(
            "invalid-proposal",
            "Unsupported cancellation proposal.",
        )

    vault = Path(str(proposal["vault"])).expanduser().resolve()
    config, remote = _repository_identity(vault)
    branch = str(config["branch"])
    if remote != proposal["remote"] or branch != proposal["branch"]:
        raise CoordinationError(
            "invalid-proposal",
            "Cancellation proposal repository identity changed.",
        )

    inspected = inspect_task_state(vault)
    if inspected["remote_head"] != proposal["remote_head"]:
        raise CoordinationError(
            "cancel-stale",
            (
                f"Remote moved from confirmed head {proposal['remote_head']} "
                f"to {inspected['remote_head']}."
            ),
            exit_code=3,
        )
    state = inspected["task_state"]
    expected_run_id = str(proposal["expected_run_id"])
    if (
        state is None
        or state.get("status") != "running"
        or state.get("run_id") != expected_run_id
    ):
        raise CoordinationError(
            "cancel-stale",
            "The confirmed Run is no longer the current running task.",
            exit_code=3,
        )

    cancelled_at = datetime.now(ZoneInfo(FIXED_TIMEZONE))
    cancelled_state = copy.deepcopy(state)
    cancelled_state.update(
        {
            "status": "cancelled",
            "updated_at": cancelled_at.isoformat(),
            "cancelled_at": cancelled_at.isoformat(),
        }
    )
    cancelled_state.pop("lease_until", None)
    task_relative = _task_relative_path()
    pushed, cancellation_commit, detail = _cancellation_candidate(
        vault,
        expected_url=str(config["url"]),
        branch=branch,
        expected_head=str(proposal["remote_head"]),
        state=cancelled_state,
        task_relative=task_relative,
    )
    if not pushed:
        raise CoordinationError(
            "cancel-raced",
            f"Cancellation lost a remote race: {detail}",
            exit_code=3,
        )
    return {
        "status": "cancelled",
        "run_id": expected_run_id,
        "cancellation_commit": cancellation_commit,
        "remote_head": cancellation_commit,
    }


def _now(manifest: dict, value: datetime | None = None) -> datetime:
    zone = ZoneInfo(str(manifest["timezone"]))
    if value is None:
        return datetime.now(zone)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _daily_output_relative(manifest: dict) -> Path:
    vault = Path(manifest["paths"]["vault"]).resolve()
    target = daily_papers_dir() / (
        f"{manifest['target_date']}-论文推荐.md"
    )
    try:
        return target.resolve().relative_to(vault)
    except ValueError as exc:
        raise CoordinationError(
            "invalid-config",
            f"Daily output is outside the Vault: {target}",
        ) from exc


def _daily_output_relative_from_context(
    manifest: dict,
    context: dict,
) -> Path:
    vault = Path(manifest["paths"]["vault"]).resolve()
    target = Path(str(context["paths"]["daily_papers"])).resolve() / (
        f"{manifest['target_date']}-论文推荐.md"
    )
    try:
        return target.relative_to(vault)
    except ValueError as exc:
        raise CoordinationError(
            "invalid-config",
            f"Daily output is outside the Vault: {target}",
        ) from exc


def _coordinator_repository(
    vault: Path,
    manifest: dict,
    context: dict,
) -> tuple[dict, str]:
    """Validate a coordinator-supplied immutable runtime snapshot."""
    repository = context.get("repository")
    paths = context.get("paths")
    if not isinstance(repository, dict) or not isinstance(paths, dict):
        raise CoordinationError(
            "invalid-runtime-context",
            "Coordinator runtime context is missing repository or paths.",
        )
    context_vault = Path(str(paths.get("vault", ""))).expanduser().resolve()
    if context_vault != vault:
        raise CoordinationError(
            "invalid-runtime-context",
            "Coordinator runtime context points at a different Vault.",
        )
    if context.get("configuration_fingerprint") != manifest.get(
        "configuration_fingerprint"
    ):
        raise CoordinationError(
            "config-conflict",
            "Coordinator runtime fingerprint differs from the Run Manifest.",
        )
    expected = {
        "url": FIXED_VAULT_URL,
        "remote": FIXED_REMOTE,
        "branch": FIXED_BRANCH,
        "task_state_file": FIXED_TASK_STATE_FILE,
        "pull_before_run": True,
        "require_clean": True,
        "coordination_enabled": True,
        "same_day_policy": "skip",
    }
    for key, value in expected.items():
        if repository.get(key) != value:
            raise CoordinationError(
                "invalid-config",
                f"Coordinator runtime repository.{key} must be {value!r}.",
            )
    remote, _branch = _fixed_repository_identity(vault)
    return repository, remote


def _lock_state(
    manifest: dict,
    *,
    harness: str,
    owner: str,
    base_commit: str,
    now: datetime,
    lease_hours: int,
) -> dict:
    daily_output = _daily_output_relative(manifest).as_posix()
    return {
        "version": STATE_VERSION,
        "task": TASK_NAME,
        "target_date": manifest["target_date"],
        "window_days": _manifest_window_days(manifest),
        "status": "running",
        "run_id": manifest["run_id"],
        "harness": harness,
        "owner": owner,
        "started_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "lease_until": (now + timedelta(hours=lease_hours)).isoformat(),
        "base_commit": base_commit,
        "config_sha256": manifest["configuration_fingerprint"],
        "outputs": {
            "daily_note": daily_output,
        },
    }


def _push_lock_commit(
    vault: Path,
    *,
    state: dict,
    task_relative: Path,
    expected_url: str,
    branch: str,
    expected_head: str,
) -> tuple[bool, str, str]:
    with tempfile.TemporaryDirectory(prefix="dailypaper-lock-") as temp_dir:
        candidate = Path(temp_dir) / "vault"
        try:
            clone = run_git_program(
                "clone",
                "--shared",
                "--no-checkout",
                str(vault),
                str(candidate),
            )
        except SafeGitError as exc:
            raise CoordinationError("git-error", str(exc)) from exc
        if clone.returncode != 0:
            detail = clone.stderr.strip() or clone.stdout.strip()
            raise CoordinationError(
                "git-error",
                f"Could not create lock candidate: {detail}",
            )

        _git(candidate, "remote", "set-url", "origin", expected_url)
        _git(candidate, "fetch", "origin", branch)
        fetched_head = _git_output(
            candidate,
            "rev-parse",
            f"refs/remotes/origin/{branch}",
        )
        if fetched_head != expected_head:
            raise CoordinationError(
                "lock-stale",
                (
                    f"Remote moved from inspected head {expected_head} "
                    f"to {fetched_head}."
                ),
                exit_code=3,
            )
        _git(candidate, "checkout", "--detach", expected_head)
        _write_state(candidate / task_relative, state)
        _git(candidate, "add", "--", task_relative.as_posix())
        _git(
            candidate,
            "-c",
            f"user.name={COMMIT_NAME}",
            "-c",
            f"user.email={COMMIT_EMAIL}",
            "commit",
            "-m",
            f"dailypaper: acquire {state['target_date']} ({state['run_id']})",
        )
        lock_commit = _git_output(candidate, "rev-parse", "HEAD")
        push = _git(
            candidate,
            "push",
            "origin",
            f"HEAD:refs/heads/{branch}",
            check=False,
        )
        detail = push.stderr.strip() or push.stdout.strip()
        return push.returncode == 0, lock_commit, detail


def acquire(
    manifest_path: Path,
    *,
    harness: str,
    owner: str | None = None,
    now: datetime | None = None,
    expected_remote_head: str | None = None,
    runtime_context: dict | None = None,
    record_manifest: bool = True,
) -> dict:
    manifest = _manifest_data(manifest_path)
    if manifest.get("timezone") != FIXED_TIMEZONE:
        raise CoordinationError(
            "invalid-config",
            f"Coordinated runs must use timezone {FIXED_TIMEZONE}",
        )
    vault = Path(manifest["paths"]["vault"]).resolve()
    if runtime_context is None:
        config, remote = _repository_identity(vault)
        base_commit = _sync_before_run(vault, config, remote)
        clear_config_cache()
        config, remote = _repository_identity(vault)
    else:
        config, remote = _coordinator_repository(
            vault,
            manifest,
            runtime_context,
        )
        branch = str(config["branch"])
        _ensure_clean(vault)
        _git(vault, "fetch", remote, branch)
        fetched_head = _git_output(
            vault,
            "rev-parse",
            f"refs/remotes/{remote}/{branch}",
        )
        if expected_remote_head is None:
            raise CoordinationError(
                "missing-remote-head",
                "Coordinator acquisition requires an inspected remote HEAD.",
            )
        if fetched_head != expected_remote_head:
            raise CoordinationError(
                "lock-stale",
                (
                    f"Remote moved from inspected head {expected_remote_head} "
                    f"to {fetched_head}."
                ),
                exit_code=3,
            )
        current_head = _git_output(vault, "rev-parse", "HEAD")
        if current_head != fetched_head:
            _git(vault, "merge", "--ff-only", fetched_head)
        _ensure_clean(vault)
        base_commit = fetched_head

    lifecycle = None
    if record_manifest:
        manifest, lifecycle = _open_run(manifest_path)

    task_relative = _task_relative_path()
    task_path = vault / task_relative
    daily_relative = (
        _daily_output_relative(manifest)
        if runtime_context is None
        else _daily_output_relative_from_context(manifest, runtime_context)
    )
    daily_output = vault / daily_relative
    state = _read_state(task_path)
    manifest_window_days = _manifest_window_days(manifest)

    if (
        state
        and state.get("target_date") == manifest["target_date"]
        and state.get("status") in {"running", "success", "published"}
        and int(state.get("window_days", 1)) != manifest_window_days
    ):
        raise CoordinationError(
            "intent-conflict",
            (
                f"Existing Run {state.get('run_id')} for {manifest['target_date']} "
                f"uses window_days={state.get('window_days', 1)}, but the requested "
                f"Run uses window_days={manifest_window_days}."
            ),
            exit_code=3,
        )

    if daily_output.exists():
        return {
            "status": "already-completed",
            "target_date": manifest["target_date"],
            "window_days": manifest_window_days,
            "daily_output": str(daily_output),
            "base_commit": base_commit,
        }

    if state and state.get("status") == "running":
        if state.get("run_id") == manifest["run_id"]:
            if int(state.get("window_days", 1)) != manifest_window_days:
                raise CoordinationError(
                    "intent-conflict",
                    "Remote ownership uses a different frozen acquisition window.",
                )
            if state.get("config_sha256") != manifest.get(
                "configuration_fingerprint"
            ):
                raise CoordinationError(
                    "config-conflict",
                    "Remote ownership uses a different configuration fingerprint.",
                )
            result = {
                "status": "acquired",
                "target_date": manifest["target_date"],
                "window_days": manifest_window_days,
                "run_id": manifest["run_id"],
                "lock_commit": base_commit,
                "resumed": True,
                "remote": remote,
                "branch": str(config["branch"]),
            }
            if lifecycle is not None:
                _record_acquisition(
                    lifecycle,
                    lock_commit=base_commit,
                    remote=remote,
                    branch=str(config["branch"]),
                )
            return result

        lease_until = str(state.get("lease_until", "unknown"))
        raise CoordinationError(
            "locked",
            (
                f"{TASK_NAME} is owned by {state.get('harness')}/"
                f"{state.get('owner')} ({state.get('run_id')}) until {lease_until}"
            ),
            exit_code=3,
        )

    acquired_at = _now(manifest, now)
    new_state = _lock_state(
        manifest,
        harness=harness,
        owner=owner or socket.gethostname(),
        base_commit=base_commit,
        now=acquired_at,
        lease_hours=int(config.get("lease_hours", 24)),
    )
    pushed, lock_commit, push_detail = _push_lock_commit(
        vault,
        state=new_state,
        task_relative=task_relative,
        expected_url=str(config["url"]),
        branch=str(config["branch"]),
        expected_head=base_commit,
    )
    if not pushed:
        _git(vault, "fetch", remote, str(config["branch"]), check=False)
        raise CoordinationError(
            "lock-raced",
            f"Another runner updated the Vault first: {push_detail}",
            exit_code=3,
        )

    _git(vault, "pull", "--ff-only", remote, str(config["branch"]))
    result = {
        "status": "acquired",
        "target_date": manifest["target_date"],
        "window_days": manifest_window_days,
        "run_id": manifest["run_id"],
        "lock_commit": lock_commit,
        "resumed": False,
        "remote": remote,
        "branch": str(config["branch"]),
    }
    if lifecycle is not None:
        _record_acquisition(
            lifecycle,
            lock_commit=lock_commit,
            remote=remote,
            branch=str(config["branch"]),
        )
    return result


def dirty_paths(vault: Path) -> set[str]:
    """Return exact dirty Vault paths without modifying the worktree."""
    try:
        return repository_dirty_paths(vault)
    except SafeGitError as exc:
        raise CoordinationError("git-error", str(exc)) from exc


# Compatibility for existing callers while the public name is adopted.
_dirty_paths = dirty_paths


def _verify_owner(
    manifest: dict,
    *,
    vault: Path,
    config: dict,
    remote: str,
) -> tuple[dict, Path, str]:
    acquisition_commit = manifest.get("publication", {}).get(
        "acquisition_commit"
    )
    if not acquisition_commit:
        raise CoordinationError(
            "not-owner",
            "Run Manifest has not recorded Vault task acquisition.",
        )
    lock_commit = str(acquisition_commit)

    task_relative = _task_relative_path()
    state = _read_state(vault / task_relative)
    if not state or state.get("run_id") != manifest["run_id"]:
        raise CoordinationError(
            "not-owner",
            "Vault task state is not owned by this run.",
        )
    if (
        state.get("target_date") != manifest.get("target_date")
        or int(state.get("window_days", 1)) != _manifest_window_days(manifest)
        or manifest.get("timezone") != FIXED_TIMEZONE
    ):
        raise CoordinationError(
            "state-conflict",
            "Run date, window, or timezone no longer matches the acquired task.",
        )
    if state.get("config_sha256") != configuration_fingerprint():
        raise CoordinationError(
            "config-conflict",
            "Effective configuration changed after the task was acquired.",
        )

    branch = str(config["branch"])
    _git(vault, "fetch", remote, branch)
    remote_head = _git_output(
        vault,
        "rev-parse",
        f"refs/remotes/{remote}/{branch}",
    )
    if remote_head != lock_commit:
        raise CoordinationError(
            "remote-advanced",
            (
                f"Remote moved from lock commit {lock_commit} to {remote_head}; "
                "preserving local outputs without publishing."
            ),
            exit_code=3,
        )
    current_head = _git_output(vault, "rev-parse", "HEAD")
    if current_head != lock_commit:
        raise CoordinationError(
            "local-head-changed",
            f"Local HEAD moved from lock commit {lock_commit} to {current_head}.",
        )
    return state, task_relative, lock_commit


def _remote_head(vault: Path, remote: str, branch: str) -> str:
    _git(vault, "fetch", remote, branch)
    return _git_output(vault, "rev-parse", f"refs/remotes/{remote}/{branch}")


def _mark_publication_interrupted(
    lifecycle: RunLifecycle,
    *,
    message: str,
    attention_required: bool,
) -> None:
    try:
        lifecycle.interrupt(
            Interruption(
                message=message,
                attention_required=attention_required,
            )
        )
    except LifecycleError:
        # The publication metadata remains sufficient for a later safe resume.
        pass


def _finish_v2_publication(
    lifecycle: RunLifecycle,
    manifest: dict,
    *,
    content_commit: str,
    changed_paths: list[str],
) -> dict:
    lifecycle.finish("published", content_commit=content_commit)
    return {
        "status": "success",
        "target_date": manifest["target_date"],
        "window_days": _manifest_window_days(manifest),
        "run_id": manifest["run_id"],
        "content_commit": content_commit,
        "changed_paths": changed_paths,
    }


def _verify_publication_index(
    vault: Path,
    manifest: dict,
    *,
    changed_paths: list[str],
    task_relative: Path,
    completed_state: dict,
) -> None:
    """Preserve third-party staged versions before exact Run staging."""
    expected: dict[str, str | None] = {
        relative: None
        for relative in changed_paths
    }
    for artifact in manifest["artifacts"].values():
        if artifact["scope"] != "vault" or artifact["path"] not in expected:
            continue
        relative = artifact["path"]
        previous = expected[relative]
        if previous is not None and previous != artifact["sha256"]:
            raise CoordinationError(
                "artifact-conflict",
                f"Registered artifacts disagree for Vault path: {relative}",
            )
        expected[relative] = artifact["sha256"]
    try:
        task_payload = task_state.encode_task_state(
            completed_state,
            source=str(vault / task_relative),
        )
    except task_state.TaskStateError as exc:
        raise CoordinationError("invalid-state", str(exc)) from exc
    expected[task_relative.as_posix()] = hashlib.sha256(task_payload).hexdigest()
    try:
        verify_index_versions(
            vault,
            base_commit=str(manifest["publication"]["acquisition_commit"]),
            expected_sha256_by_path=expected,
            max_blob_bytes=MAX_ARTIFACT_BYTES,
        )
    except SafeGitError as exc:
        raise CoordinationError("index-conflict", str(exc)) from exc


def _complete_v2(
    manifest_path: Path,
    manifest: dict,
    lifecycle: RunLifecycle,
    *,
    now: datetime | None = None,
) -> dict:
    if manifest.get("phase") != "publishing" or manifest.get("outcome") is not None:
        raise CoordinationError(
            "not-validated",
            "A v2 Run must be in publishing phase without an Outcome.",
        )
    manifest = lifecycle.verify_publication_inputs().as_dict()

    vault = Path(manifest["paths"]["vault"]).resolve()
    config, remote = _repository_identity(vault)
    branch = str(config["branch"])
    publication = manifest["publication"]
    acquisition_commit = str(publication.get("acquisition_commit") or "")
    if not acquisition_commit:
        raise CoordinationError(
            "not-owner",
            "Run Manifest has no acquisition commit.",
        )

    task_relative = _task_relative_path()
    current_remote = _remote_head(vault, remote, branch)
    content_commit = publication.get("content_commit")
    allowed_remote_heads = {acquisition_commit}
    if content_commit:
        allowed_remote_heads.add(str(content_commit))
    if current_remote not in allowed_remote_heads:
        message = (
            f"Remote moved from Run commits {sorted(allowed_remote_heads)} "
            f"to {current_remote}; preserving local outputs."
        )
        _mark_publication_interrupted(
            lifecycle,
            message=message,
            attention_required=True,
        )
        raise CoordinationError("remote-advanced", message, exit_code=3)

    remote_ref = f"refs/remotes/{remote}/{branch}"
    remote_state = _state_at_ref(vault, remote_ref, task_relative)
    if (
        remote_state is None
        or remote_state.get("run_id") != manifest["run_id"]
    ):
        raise CoordinationError(
            "not-owner",
            "Vault Task State is not owned by this Run.",
        )
    if (
        remote_state.get("target_date") != manifest.get("target_date")
        or int(remote_state.get("window_days", 1))
        != _manifest_window_days(manifest)
    ):
        raise CoordinationError(
            "state-conflict",
            "Remote Task State intent no longer matches the Run Manifest.",
        )
    if remote_state.get("config_sha256") != configuration_fingerprint():
        raise CoordinationError(
            "config-conflict",
            "Effective configuration changed after the task was acquired.",
        )

    changed_paths = list(dict.fromkeys(manifest.get("run_change_set", [])))
    daily_output = _daily_output_relative(manifest).as_posix()
    if daily_output not in changed_paths or not (vault / daily_output).exists():
        raise CoordinationError(
            "missing-output",
            f"Validated Run did not register its daily output: {daily_output}",
        )

    if content_commit:
        content_commit = str(content_commit)
        if current_remote == content_commit:
            return _finish_v2_publication(
                lifecycle,
                manifest,
                content_commit=content_commit,
                changed_paths=changed_paths,
            )
        if _git(
            vault,
            "cat-file",
            "-e",
            f"{content_commit}^{{commit}}",
            check=False,
        ).returncode != 0:
            raise CoordinationError(
                "missing-content-commit",
                f"Recorded content commit is unavailable locally: {content_commit}",
                exit_code=3,
            )
        push = _git(
            vault,
            "push",
            remote,
            f"{content_commit}:refs/heads/{branch}",
            check=False,
        )
        observed_remote = _remote_head(vault, remote, branch)
        if observed_remote == content_commit:
            return _finish_v2_publication(
                lifecycle,
                manifest,
                content_commit=content_commit,
                changed_paths=changed_paths,
            )
        detail = push.stderr.strip() or push.stdout.strip()
        if observed_remote != acquisition_commit:
            message = (
                f"Remote moved to {observed_remote} while retrying publication."
            )
            _mark_publication_interrupted(
                lifecycle,
                message=message,
                attention_required=True,
            )
            raise CoordinationError("remote-advanced", message, exit_code=3)
        _mark_publication_interrupted(
            lifecycle,
            message=f"Content push remains pending: {detail}",
            attention_required=False,
        )
        raise CoordinationError(
            "publish-failed",
            f"Content commit was preserved locally; push failed: {detail}",
            exit_code=3,
        )

    local_head = _git_output(vault, "rev-parse", "HEAD")
    if local_head != acquisition_commit:
        raise CoordinationError(
            "local-head-changed",
            f"Local HEAD moved from acquisition commit {acquisition_commit} to {local_head}.",
        )
    if remote_state.get("status") != "running":
        raise CoordinationError(
            "not-owner",
            f"Vault Task State is {remote_state.get('status')!r}, not running.",
        )

    completed_at = _now(manifest, now)
    completed_state = copy.deepcopy(remote_state)
    completed_state.update(
        {
            "status": "success",
            "updated_at": completed_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "changed_paths": changed_paths,
        }
    )
    completed_state.pop("lease_until", None)
    _verify_publication_index(
        vault,
        manifest,
        changed_paths=changed_paths,
        task_relative=task_relative,
        completed_state=completed_state,
    )
    _write_state(vault / task_relative, completed_state)

    allowed = set(changed_paths)
    allowed.add(task_relative.as_posix())
    unexpected = dirty_paths(vault) - allowed
    if unexpected:
        raise CoordinationError(
            "unexpected-changes",
            "Vault contains changes outside this Run: "
            + ", ".join(sorted(unexpected)),
        )
    _git(vault, "add", "--", *sorted(allowed))
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
    if not staged:
        raise CoordinationError(
            "nothing-to-publish",
            "No validated Vault changes were staged.",
        )
    if not staged.issubset(allowed):
        raise CoordinationError(
            "index-conflict",
            "Git index contains paths outside this Run: "
            + ", ".join(sorted(staged - allowed)),
        )
    _git(
        vault,
        "-c",
        f"user.name={COMMIT_NAME}",
        "-c",
        f"user.email={COMMIT_EMAIL}",
        "commit",
        "-m",
        f"daily papers: {manifest['target_date']}",
    )
    content_commit = _git_output(vault, "rev-parse", "HEAD")
    lifecycle.record_content_commit(content_commit)

    push = _git(
        vault,
        "push",
        remote,
        f"{content_commit}:refs/heads/{branch}",
        check=False,
    )
    observed_remote = _remote_head(vault, remote, branch)
    if observed_remote == content_commit:
        return _finish_v2_publication(
            lifecycle,
            manifest,
            content_commit=content_commit,
            changed_paths=changed_paths,
        )
    detail = push.stderr.strip() or push.stdout.strip()
    if observed_remote != acquisition_commit:
        message = (
            f"Remote moved to {observed_remote} while publishing "
            f"{content_commit}."
        )
        _mark_publication_interrupted(
            lifecycle,
            message=message,
            attention_required=True,
        )
        raise CoordinationError("remote-advanced", message, exit_code=3)
    _mark_publication_interrupted(
        lifecycle,
        message=f"Content push remains pending: {detail}",
        attention_required=False,
    )
    raise CoordinationError(
        "publish-failed",
        f"Content commit was preserved locally; push failed: {detail}",
        exit_code=3,
    )


def complete(
    manifest_path: Path,
    *,
    now: datetime | None = None,
) -> dict:
    manifest, lifecycle = _open_run(manifest_path)
    return _complete_v2(
        manifest_path,
        manifest,
        lifecycle,
        now=now,
    )


def _fail_v2(
    manifest_path: Path,
    manifest: dict,
    lifecycle: RunLifecycle,
    *,
    message: str,
    now: datetime | None = None,
) -> dict:
    vault = Path(manifest["paths"]["vault"]).resolve()
    config, remote = _repository_identity(vault)
    state, task_relative, _ = _verify_owner(
        manifest,
        vault=vault,
        config=config,
        remote=remote,
    )
    failed_at = _now(manifest, now)
    state.update(
        {
            "status": "failed",
            "updated_at": failed_at.isoformat(),
            "failed_at": failed_at.isoformat(),
            "message": message,
        }
    )
    state.pop("lease_until", None)
    _write_state(vault / task_relative, state)
    _git(vault, "add", "--", task_relative.as_posix())
    _git(
        vault,
        "-c",
        f"user.name={COMMIT_NAME}",
        "-c",
        f"user.email={COMMIT_EMAIL}",
        "commit",
        "-m",
        f"dailypaper: fail {manifest['target_date']} ({manifest['run_id']})",
    )
    failure_commit = _git_output(vault, "rev-parse", "HEAD")
    push = _git(
        vault,
        "push",
        remote,
        f"HEAD:refs/heads/{config['branch']}",
        check=False,
    )
    if push.returncode != 0:
        detail = push.stderr.strip() or push.stdout.strip()
        raise CoordinationError(
            "release-failed",
            f"Failure state is local but could not be pushed: {detail}",
            exit_code=3,
        )
    lifecycle.finish("failed", reason=message)
    return {
        "status": "failed",
        "target_date": manifest["target_date"],
        "run_id": manifest["run_id"],
        "failure_commit": failure_commit,
    }


def fail(
    manifest_path: Path,
    *,
    message: str,
    now: datetime | None = None,
) -> dict:
    manifest, lifecycle = _open_run(manifest_path)
    return _fail_v2(
        manifest_path,
        manifest,
        lifecycle,
        message=message,
        now=now,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser("bootstrap")
    bootstrap_parser.add_argument("--vault", required=True, type=Path)

    acquire_parser = subparsers.add_parser("acquire")
    acquire_parser.add_argument("manifest", type=Path)
    acquire_parser.add_argument(
        "--harness",
        required=True,
        choices=("claude-code", "codex"),
    )
    acquire_parser.add_argument("--owner")

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("manifest", type=Path)

    fail_parser = subparsers.add_parser("fail")
    fail_parser.add_argument("manifest", type=Path)
    fail_parser.add_argument("--message", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--vault", required=True, type=Path)

    prepare_cancel_parser = subparsers.add_parser("prepare-cancel")
    prepare_cancel_parser.add_argument("--vault", required=True, type=Path)
    prepare_cancel_parser.add_argument("--run-id", required=True)

    cancel_parser = subparsers.add_parser("cancel")
    cancel_parser.add_argument("proposal", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "bootstrap":
            result = bootstrap_vault(args.vault)
        elif args.command == "acquire":
            result = acquire(
                args.manifest,
                harness=args.harness,
                owner=args.owner,
            )
        elif args.command == "complete":
            result = complete(args.manifest)
        elif args.command == "fail":
            result = fail(args.manifest, message=args.message)
        elif args.command == "inspect":
            result = inspect_task_state(args.vault)
        elif args.command == "prepare-cancel":
            result = prepare_cancel(args.vault, args.run_id)
        else:
            try:
                proposal = load_json_object(
                    args.proposal,
                    max_bytes=config_schema.MAX_CONFIG_BYTES,
                    label="Cancellation proposal",
                )
                if proposal is None:
                    raise SafeIOError(
                        f"Cancellation proposal file does not exist: {args.proposal}"
                    )
            except SafeIOError as exc:
                raise CoordinationError(
                    "invalid-proposal",
                    f"Could not read cancellation proposal: {exc}",
                ) from exc
            result = cancel(proposal)
    except CoordinationError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        raise SystemExit(exc.exit_code) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
