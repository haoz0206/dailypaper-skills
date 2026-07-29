#!/usr/bin/env python3
"""Read-only guard against standalone writes during a coordinated daily run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import task_state
from safe_git import (
    GitCommandResult,
    SafeGitError,
    inspect_repository,
    read_git_blob,
    repository_dirty_paths,
    run_git_command,
)
from safe_path import SafePathError, resolve_within


DEFAULT_TASK_STATE = ".dailypaper/tasks/daily-papers.json"
MAX_TASK_STATE_BYTES = task_state.MAX_TASK_STATE_BYTES
TERMINAL_STATUSES = frozenset({"success", "published", "failed", "cancelled"})


class GuardError(RuntimeError):
    """The Vault task state is malformed or unsafe."""


class ActiveRunError(GuardError):
    """A coordinated daily run currently owns the Vault."""

    def __init__(self, message: str, state: dict[str, Any]) -> None:
        super().__init__(message)
        self.state = state


def _task_state_path(vault: Path, relative: str) -> Path:
    try:
        return resolve_within(vault, relative, label="task_state_file")
    except SafePathError as exc:
        raise GuardError(str(exc)) from exc


def _parse_state(raw: bytes, source: str) -> dict[str, Any]:
    try:
        return task_state.parse_task_state(raw, source=source)
    except task_state.TaskStateError as exc:
        raise GuardError(str(exc)) from exc


def _evaluate_state(state: dict[str, Any], source: str) -> dict[str, Any]:
    status = state.get("status")
    if status == "running":
        raise ActiveRunError(
            "DailyPaper run is active: "
            f"{state.get('harness', 'unknown')}/{state.get('owner', 'unknown')} "
            f"({state.get('run_id', 'unknown')}) until "
            f"{state.get('lease_until', 'unknown')}",
            state,
        )
    if status not in TERMINAL_STATUSES:
        raise GuardError(f"Unexpected task status {status!r} at {source}")
    return {
        "status": "safe",
        "task_state": status,
        "run_id": state.get("run_id"),
        "state_source": source,
    }


def _git(vault: Path, *args: str) -> GitCommandResult:
    try:
        return run_git_command(vault, *args)
    except SafeGitError as exc:
        raise GuardError("Git is required to inspect remote task ownership") from exc


def _git_output(vault: Path, *args: str) -> str:
    result = _git(vault, *args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GuardError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _validate_repository(
    vault: Path,
    *,
    repository_url: str,
    remote: str,
    branch: str,
) -> None:
    try:
        snapshot = inspect_repository(vault, remote=remote)
    except (SafeGitError, ValueError) as exc:
        raise GuardError(str(exc)) from exc
    if snapshot.root != vault:
        raise GuardError(f"Configured Vault is not the Git root: {vault}")
    if snapshot.remote_url != repository_url:
        raise GuardError(
            f"Git remote {remote!r} does not match configured repository URL"
        )
    if snapshot.branch != branch:
        raise GuardError(
            f"Vault must be checked out on configured branch {branch!r}"
        )


def guard_active_run(
    vault: Path,
    *,
    task_state_file: str = DEFAULT_TASK_STATE,
) -> dict[str, Any]:
    """Return safe local task state or raise when a daily run owns the Vault."""
    vault_path = vault.expanduser().resolve()
    if not vault_path.is_dir():
        raise GuardError(f"Configured Vault is not a directory: {vault_path}")
    state_path = _task_state_path(vault_path, task_state_file)
    source = str(state_path)
    try:
        state = task_state.read_task_state_file(state_path)
    except task_state.TaskStateError as exc:
        raise GuardError(str(exc)) from exc
    if state is None:
        return {
            "status": "safe",
            "task_state": "absent",
            "state_path": str(state_path),
            "state_source": source,
        }
    return {
        **_evaluate_state(state, source),
        "state_path": str(state_path),
    }


def guard_remote_active_run(
    vault: Path,
    *,
    repository_url: str,
    remote: str,
    branch: str,
    task_state_file: str = DEFAULT_TASK_STATE,
    fetch_remote: bool = True,
) -> dict[str, Any]:
    """Fetch and inspect task ownership at the configured remote branch."""
    vault_path = vault.expanduser().resolve()
    if not vault_path.is_dir():
        raise GuardError(f"Configured Vault is not a directory: {vault_path}")
    relative = _task_state_path(vault_path, task_state_file).relative_to(vault_path)
    if not all(isinstance(value, str) and value for value in (repository_url, remote, branch)):
        raise GuardError("Repository URL, remote, and branch must be non-empty strings")
    _validate_repository(
        vault_path,
        repository_url=repository_url,
        remote=remote,
        branch=branch,
    )

    if fetch_remote:
        fetch = _git(vault_path, "fetch", remote, branch)
        if fetch.returncode != 0:
            detail = fetch.stderr.strip() or fetch.stdout.strip()
            raise GuardError(f"git fetch {remote} {branch} failed: {detail}")
    remote_ref = f"refs/remotes/{remote}/{branch}"
    remote_head = _git_output(vault_path, "rev-parse", "--verify", remote_ref)
    source = f"{remote_ref}:{relative.as_posix()}"
    try:
        raw_state = read_git_blob(
            vault_path,
            source,
            max_bytes=MAX_TASK_STATE_BYTES,
        )
    except SafeGitError as exc:
        raise GuardError(str(exc)) from exc
    if raw_state is None:
        return {
            "status": "safe",
            "task_state": "absent",
            "state_source": source,
            "remote_head": remote_head,
        }
    return {
        **_evaluate_state(
            _parse_state(raw_state, source),
            source,
        ),
        "remote_head": remote_head,
    }


def prepare_standalone_vault(
    vault: Path,
    *,
    repository_url: str,
    remote: str,
    branch: str,
) -> dict[str, Any]:
    """Bring a clean clone forward, or prove a dirty clone is already current."""
    vault_path = vault.expanduser().resolve()
    if not vault_path.is_dir():
        raise GuardError(f"Configured Vault is not a directory: {vault_path}")
    _validate_repository(
        vault_path,
        repository_url=repository_url,
        remote=remote,
        branch=branch,
    )
    fetch = _git(vault_path, "fetch", remote, branch)
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip()
        raise GuardError(f"git fetch {remote} {branch} failed: {detail}")

    remote_ref = f"refs/remotes/{remote}/{branch}"
    local_head = _git_output(vault_path, "rev-parse", "--verify", "HEAD")
    remote_head = _git_output(vault_path, "rev-parse", "--verify", remote_ref)
    try:
        dirty = bool(repository_dirty_paths(vault_path))
    except SafeGitError as exc:
        raise GuardError(str(exc)) from exc
    pulled = False
    if local_head != remote_head:
        if dirty:
            raise GuardError(
                "Vault has local changes and its HEAD differs from the fetched remote; "
                "preserve the changes and synchronize manually before writing"
            )
        ancestor = _git(vault_path, "merge-base", "--is-ancestor", "HEAD", remote_ref)
        if ancestor.returncode != 0:
            raise GuardError(
                "Vault branch is ahead of or divergent from the fetched remote; "
                "automatic rebase or overwrite is forbidden"
            )
        merge = _git(vault_path, "merge", "--ff-only", remote_ref)
        if merge.returncode != 0:
            detail = merge.stderr.strip() or merge.stdout.strip()
            raise GuardError(f"git merge --ff-only {remote_ref} failed: {detail}")
        local_head = _git_output(vault_path, "rev-parse", "--verify", "HEAD")
        pulled = True
    if local_head != remote_head:
        raise GuardError("Vault HEAD does not match the fetched remote after preparation")
    return {
        "status": "prepared",
        "vault": str(vault_path),
        "dirty": dirty,
        "pulled": pulled,
        "local_head": local_head,
        "remote_head": remote_head,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reject standalone writes while a DailyPaper run owns the Vault."
    )
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--task-state-file", default=DEFAULT_TASK_STATE)
    parser.add_argument("--repository-url")
    parser.add_argument("--remote")
    parser.add_argument("--branch")
    args = parser.parse_args()
    try:
        remote_values = (args.repository_url, args.remote, args.branch)
        if any(remote_values) and not all(remote_values):
            raise GuardError(
                "--repository-url, --remote, and --branch must be provided together"
            )
        if all(remote_values):
            result = guard_remote_active_run(
                args.vault,
                repository_url=args.repository_url,
                remote=args.remote,
                branch=args.branch,
                task_state_file=args.task_state_file,
            )
        else:
            result = guard_active_run(
                args.vault,
                task_state_file=args.task_state_file,
            )
    except ActiveRunError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "code": "active-run",
                    "message": str(exc),
                    "run": exc.state,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except GuardError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "code": "invalid-task-state",
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
