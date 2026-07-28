#!/usr/bin/env python3
"""Coordinate daily-paper runs through atomic Git branch updates."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import socket
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from run_lifecycle import (
    DAILY_WORKFLOW_CONTRACT,
    ConfigurationMismatch,
    ContractMismatch,
    Interruption,
    LifecycleError,
    RunLifecycle,
)
from user_config import (
    clear_config_cache,
    daily_papers_dir,
    load_user_config,
    obsidian_vault_path,
    repository_config,
)


STATE_VERSION = 1
TASK_NAME = "daily-papers"
FIXED_VAULT_URL = "git@github.com:haoz0206/dailypaper-vault.git"
FIXED_REMOTE = "origin"
FIXED_BRANCH = "main"
FIXED_TASK_STATE_FILE = ".dailypaper/tasks/daily-papers.json"
FIXED_TIMEZONE = "Asia/Shanghai"
COMMIT_NAME = "dailypaper automation"
COMMIT_EMAIL = "dailypaper@localhost"


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
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(vault), *args],
        text=True,
        capture_output=True,
        check=False,
    )
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
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise CoordinationError(
            "invalid-config",
            f"Vault coordination path must be relative: {value}",
        )
    return candidate


def _task_relative_path() -> Path:
    configured = str(repository_config()["task_state_file"])
    if configured != FIXED_TASK_STATE_FILE:
        raise CoordinationError(
            "invalid-config",
            f"Task state file must remain fixed to {FIXED_TASK_STATE_FILE}",
        )
    return _safe_relative_path(FIXED_TASK_STATE_FILE)


def configuration_fingerprint() -> str:
    """Return the output-affecting configuration identity for a DailyPaper Run."""
    config = copy.deepcopy(load_user_config())
    paths = config.get("paths", {})
    for machine_local_key in (
        "obsidian_vault",
        "zotero_db",
        "zotero_storage",
    ):
        paths.pop(machine_local_key, None)
    repository = config.get("repository", {})
    repository.pop("remote", None)
    payload = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# Compatibility for existing callers while the public name is adopted.
_config_fingerprint = configuration_fingerprint


def _read_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != STATE_VERSION:
        raise CoordinationError(
            "invalid-state",
            f"Unsupported task state version: {data.get('version')}",
        )
    if data.get("task") != TASK_NAME:
        raise CoordinationError(
            "invalid-state",
            f"Unexpected task name in {path}: {data.get('task')}",
        )
    return data


def _validate_state(data: dict, source: str) -> dict:
    if data.get("version") != STATE_VERSION:
        raise CoordinationError(
            "invalid-state",
            f"Unsupported task state version in {source}: {data.get('version')}",
        )
    if data.get("task") != TASK_NAME:
        raise CoordinationError(
            "invalid-state",
            f"Unexpected task name in {source}: {data.get('task')}",
        )
    return data


def _state_at_ref(vault: Path, ref: str, task_relative: Path) -> dict | None:
    object_name = f"{ref}:{task_relative.as_posix()}"
    exists = _git(vault, "cat-file", "-e", object_name, check=False)
    if exists.returncode != 0:
        return None
    raw = _git_output(vault, "show", object_name)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CoordinationError(
            "invalid-state",
            f"Remote task state is not valid JSON: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise CoordinationError(
            "invalid-state",
            "Remote task state must be a JSON object.",
        )
    return _validate_state(data, object_name)


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _manifest_data(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoordinationError(
            "invalid-manifest",
            f"Could not read Run Manifest {path}: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise CoordinationError(
            "invalid-manifest",
            f"Run Manifest must be a JSON object: {path}",
        )
    return data


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
    return {
        "paths": {
            "obsidian_vault": ".",
        },
        "runtime": {
            "timezone": FIXED_TIMEZONE,
        },
        "repository": {
            "url": FIXED_VAULT_URL,
            "remote": FIXED_REMOTE,
            "branch": FIXED_BRANCH,
            "task_state_file": FIXED_TASK_STATE_FILE,
            "pull_before_run": True,
            "require_clean": True,
            "coordination_enabled": True,
            "lease_hours": 24,
            "same_day_policy": "skip",
        },
    }


def bootstrap_vault(vault: Path) -> dict:
    """Create the first portable Vault commit without machine-local paths."""
    vault = vault.expanduser().resolve()
    top_level = Path(_git_output(vault, "rev-parse", "--show-toplevel")).resolve()
    if top_level != vault:
        raise CoordinationError(
            "wrong-vault",
            f"Bootstrap target is not the Git root: {vault}",
        )

    actual_url = _git_output(
        vault,
        "remote",
        "get-url",
        FIXED_REMOTE,
    ).rstrip("/")
    if actual_url != FIXED_VAULT_URL:
        raise CoordinationError(
            "wrong-remote",
            f"Expected {FIXED_REMOTE}={FIXED_VAULT_URL}, found {actual_url}",
        )
    current_branch = _git_output(vault, "branch", "--show-current")
    if current_branch != FIXED_BRANCH:
        raise CoordinationError(
            "wrong-branch",
            f"Expected branch {FIXED_BRANCH}, found {current_branch or 'detached HEAD'}",
        )
    _ensure_clean(vault)

    has_head = (
        _git(vault, "rev-parse", "--verify", "HEAD", check=False).returncode == 0
    )
    if has_head:
        _git(vault, "pull", "--ff-only", FIXED_REMOTE, FIXED_BRANCH)
        _ensure_clean(vault)

    gitignore_path = vault / ".gitignore"
    existing_gitignore = (
        gitignore_path.read_text(encoding="utf-8")
        if gitignore_path.exists()
        else ""
    )
    ignore_lines = existing_gitignore.splitlines()
    if ".dailypaper/runs/" not in ignore_lines:
        separator = (
            ""
            if not existing_gitignore or existing_gitignore.endswith("\n")
            else "\n"
        )
        gitignore_path.write_text(
            existing_gitignore
            + separator
            + "# Local daily-paper run state\n"
            + ".dailypaper/runs/\n",
            encoding="utf-8",
        )

    config_path = vault / ".dailypaper" / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CoordinationError(
                "invalid-config",
                f"Existing Vault config is not valid JSON: {exc}",
            ) from exc
        repository = config.get("repository", {})
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
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(_bootstrap_config(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    allowed = {
        ".gitignore",
        ".dailypaper/config.json",
    }
    unexpected = _dirty_paths(vault) - allowed
    if unexpected:
        raise CoordinationError(
            "unexpected-changes",
            "Vault contains changes outside bootstrap: "
            + ", ".join(sorted(unexpected)),
        )

    _git(vault, "add", "--", ".gitignore")
    _git(vault, "add", "--force", "--", ".dailypaper/config.json")
    staged = _git_output(
        vault,
        "-c",
        "core.quotePath=false",
        "diff",
        "--cached",
        "--name-only",
    )
    if not staged:
        return {
            "status": "already-bootstrapped",
            "vault": str(vault),
            "branch": FIXED_BRANCH,
        }

    _git(
        vault,
        "-c",
        f"user.name={COMMIT_NAME}",
        "-c",
        f"user.email={COMMIT_EMAIL}",
        "commit",
        "-m",
        "dailypaper: bootstrap vault",
    )
    bootstrap_commit = _git_output(vault, "rev-parse", "HEAD")
    push = _git(
        vault,
        "push",
        "--set-upstream",
        FIXED_REMOTE,
        FIXED_BRANCH,
        check=False,
    )
    if push.returncode != 0:
        detail = push.stderr.strip() or push.stdout.strip()
        raise CoordinationError(
            "bootstrap-push-failed",
            f"Bootstrap commit was preserved locally; push failed: {detail}",
            exit_code=3,
        )
    return {
        "status": "bootstrapped",
        "vault": str(vault),
        "branch": FIXED_BRANCH,
        "bootstrap_commit": bootstrap_commit,
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
    expected_url = FIXED_VAULT_URL
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

    top_level = Path(_git_output(vault, "rev-parse", "--show-toplevel")).resolve()
    if top_level != vault.resolve():
        raise CoordinationError(
            "wrong-vault",
            f"Configured Vault is not the Git root: {vault}",
        )

    actual_url = _git_output(vault, "remote", "get-url", remote).rstrip("/")
    if actual_url != expected_url:
        raise CoordinationError(
            "wrong-remote",
            f"Expected {remote}={expected_url}, found {actual_url}",
        )

    current_branch = _git_output(vault, "branch", "--show-current")
    if current_branch != branch:
        raise CoordinationError(
            "wrong-branch",
            f"Expected branch {branch}, found {current_branch or 'detached HEAD'}",
        )
    return config, remote


def _ensure_clean(vault: Path) -> None:
    status = _git_output(vault, "status", "--porcelain", "--untracked-files=all")
    if status:
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
    config, remote = _repository_identity(vault)
    branch = str(config["branch"])
    _git(vault, "fetch", remote, branch)
    remote_ref = f"refs/remotes/{remote}/{branch}"
    remote_head = _git_output(vault, "rev-parse", remote_ref)
    task_relative = _task_relative_path()
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
        clone = subprocess.run(
            ["git", "clone", "--shared", "--no-checkout", str(vault), str(candidate)],
            text=True,
            capture_output=True,
            check=False,
        )
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
        "status": "running",
        "run_id": manifest["run_id"],
        "harness": harness,
        "owner": owner,
        "started_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "lease_until": (now + timedelta(hours=lease_hours)).isoformat(),
        "base_commit": base_commit,
        "config_sha256": _config_fingerprint(),
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
) -> tuple[bool, str, str]:
    with tempfile.TemporaryDirectory(prefix="dailypaper-lock-") as temp_dir:
        candidate = Path(temp_dir) / "vault"
        clone = subprocess.run(
            [
                "git",
                "clone",
                "--shared",
                "--branch",
                branch,
                "--single-branch",
                str(vault),
                str(candidate),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if clone.returncode != 0:
            detail = clone.stderr.strip() or clone.stdout.strip()
            raise CoordinationError(
                "git-error",
                f"Could not create lock candidate: {detail}",
            )

        _git(candidate, "remote", "set-url", "origin", expected_url)
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
) -> dict:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = _manifest_data(manifest_path)
    if manifest.get("timezone") != FIXED_TIMEZONE:
        raise CoordinationError(
            "invalid-config",
            f"Coordinated runs must use timezone {FIXED_TIMEZONE}",
        )
    vault = Path(manifest["paths"]["vault"]).resolve()
    config, remote = _repository_identity(vault)
    base_commit = _sync_before_run(vault, config, remote)
    clear_config_cache()
    config, remote = _repository_identity(vault)
    manifest, lifecycle = _open_run(manifest_path)

    task_relative = _task_relative_path()
    task_path = vault / task_relative
    daily_output = vault / _daily_output_relative(manifest)
    state = _read_state(task_path)

    if daily_output.exists():
        return {
            "status": "already-completed",
            "target_date": manifest["target_date"],
            "daily_output": str(daily_output),
            "base_commit": base_commit,
        }

    if state and state.get("status") == "running":
        if state.get("run_id") == manifest["run_id"]:
            result = {
                "status": "acquired",
                "target_date": manifest["target_date"],
                "run_id": manifest["run_id"],
                "lock_commit": base_commit,
                "resumed": True,
            }
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
        "run_id": manifest["run_id"],
        "lock_commit": lock_commit,
        "resumed": False,
    }
    _record_acquisition(
        lifecycle,
        lock_commit=lock_commit,
        remote=remote,
        branch=str(config["branch"]),
    )
    return result


def dirty_paths(vault: Path) -> set[str]:
    """Return exact dirty Vault paths without modifying the worktree."""
    commands = (
        ("-c", "core.quotePath=false", "diff", "--name-only"),
        ("-c", "core.quotePath=false", "diff", "--cached", "--name-only"),
        (
            "-c",
            "core.quotePath=false",
            "ls-files",
            "--others",
            "--exclude-standard",
        ),
    )
    paths: set[str] = set()
    for command in commands:
        output = _git_output(vault, *command)
        paths.update(line for line in output.splitlines() if line)
    return paths


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
        or manifest.get("timezone") != FIXED_TIMEZONE
    ):
        raise CoordinationError(
            "state-conflict",
            "Run date or timezone no longer matches the acquired task.",
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
        "run_id": manifest["run_id"],
        "content_commit": content_commit,
        "changed_paths": changed_paths,
    }


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
    staged = _git_output(
        vault,
        "-c",
        "core.quotePath=false",
        "diff",
        "--cached",
        "--name-only",
    )
    if not staged:
        raise CoordinationError(
            "nothing-to-publish",
            "No validated Vault changes were staged.",
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
    manifest_path = manifest_path.expanduser().resolve()
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
    manifest_path = manifest_path.expanduser().resolve()
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
                proposal = json.loads(
                    args.proposal.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise CoordinationError(
                    "invalid-proposal",
                    f"Could not read cancellation proposal: {exc}",
                ) from exc
            if not isinstance(proposal, dict):
                raise CoordinationError(
                    "invalid-proposal",
                    "Cancellation proposal must be a JSON object.",
                )
            result = cancel(proposal)
    except CoordinationError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        raise SystemExit(exc.exit_code) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
