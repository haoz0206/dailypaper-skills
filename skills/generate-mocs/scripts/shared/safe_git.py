#!/usr/bin/env python3
"""Bounded reads for immutable Git blobs.

Git coordination owns repository state transitions.  This module owns the
smaller read-only boundary used to inspect one blob without first capturing an
arbitrarily large object in memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from pathlib import Path
from typing import Mapping

from safe_path import SafePathError, relative_posix_path
from safe_process import SafeProcessError, run_bounded_tool


DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_OBJECT_NAME_CHARS = 4096
MAX_SIZE_OUTPUT_BYTES = 128
MAX_ERROR_BYTES = 64 * 1024
MAX_COMMAND_STDOUT_BYTES = 8 * 1024 * 1024
MAX_COMMAND_STDERR_BYTES = 1024 * 1024
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300.0
MAX_DIRTY_PATHS = 100_000
MAX_GIT_PATH_CHARS = 4096
OID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REMOTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SafeGitError(RuntimeError):
    """A Git object could not be read within the bounded blob contract."""


@dataclass(frozen=True)
class GitCommandResult:
    """Text result compatible with the attributes used from CompletedProcess."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class RepositorySnapshot:
    """One read-only projection of repository identity."""

    root: Path
    remote: str
    remote_url: str
    branch: str


def _run_git(
    vault: Path,
    *arguments: str,
    max_stdout_bytes: int,
    max_stderr_bytes: int = MAX_ERROR_BYTES,
    timeout: float,
):
    try:
        return run_bounded_tool(
            ["git", "-C", str(vault), *arguments],
            timeout=timeout,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )
    except SafeProcessError as exc:
        raise SafeGitError(f"Bounded Git command failed: {exc}") from exc


def _detail(stdout: bytes, stderr: bytes) -> str:
    payload = stderr.strip() or stdout.strip()
    return payload.decode("utf-8", errors="replace") or "unknown Git error"


def run_git_command(
    vault: Path,
    *arguments: str,
    timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    max_stdout_bytes: int = MAX_COMMAND_STDOUT_BYTES,
    max_stderr_bytes: int = MAX_COMMAND_STDERR_BYTES,
    environment: Mapping[str, str] | None = None,
) -> GitCommandResult:
    """Run one repository command with bounded output and a wall-clock limit.

    The caller still owns command choice, return-code interpretation, and every
    repository mutation.  This function owns only process and text boundaries.
    """
    if not arguments:
        raise ValueError("at least one Git argument is required")
    return run_git_program(
        "-C",
        str(vault.expanduser().resolve()),
        *arguments,
        timeout=timeout,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        environment=environment,
    )


def run_git_program(
    *arguments: str,
    timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    max_stdout_bytes: int = MAX_COMMAND_STDOUT_BYTES,
    max_stderr_bytes: int = MAX_COMMAND_STDERR_BYTES,
    environment: Mapping[str, str] | None = None,
) -> GitCommandResult:
    """Run Git without requiring an existing worktree, for bounded clone/setup."""
    if not arguments:
        raise ValueError("at least one Git argument is required")
    try:
        result = run_bounded_tool(
            ["git", *arguments],
            timeout=timeout,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            environment=environment,
        )
    except SafeProcessError as exc:
        raise SafeGitError(f"Bounded Git command failed: {exc}") from exc
    try:
        stdout = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SafeGitError("Git stdout is not valid UTF-8") from exc
    stderr = result.stderr.decode("utf-8", errors="replace")
    return GitCommandResult(
        args=("git", *arguments),
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _validated_git_path(value: str) -> str:
    try:
        return relative_posix_path(
            value,
            max_chars=MAX_GIT_PATH_CHARS,
            label="Git status path",
        ).as_posix()
    except SafePathError as exc:
        raise SafeGitError(
            f"Git status contains an unsafe repository path: {exc}"
        ) from exc


def _parse_porcelain_v1_z(payload: str) -> set[str]:
    """Parse one complete `git status --porcelain=v1 -z` snapshot."""
    if not payload:
        return set()
    fields = payload.split("\0")
    if fields[-1] != "":
        raise SafeGitError("Git status output is missing its NUL terminator")
    fields.pop()
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            raise SafeGitError("Git status contains a malformed porcelain record")
        status = record[:2]
        path = _validated_git_path(record[3:])
        paths.add(path)
        if "R" in status or "C" in status:
            if index >= len(fields):
                raise SafeGitError(
                    "Git status rename/copy record is missing its source path"
                )
            paths.add(_validated_git_path(fields[index]))
            index += 1
        if len(paths) > MAX_DIRTY_PATHS:
            raise SafeGitError(
                f"Git status exceeds the {MAX_DIRTY_PATHS}-path safety limit"
            )
    return paths


def repository_dirty_paths(vault: Path) -> set[str]:
    """Return one validated dirty-path snapshot using a single Git command."""
    result = run_git_command(
        vault,
        "-c",
        "core.quotePath=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
        "--renames",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise SafeGitError(f"Could not inspect repository dirty paths: {detail}")
    return _parse_porcelain_v1_z(result.stdout)


def _single_git_line(result: GitCommandResult, *, label: str) -> str:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise SafeGitError(f"Could not inspect repository {label}: {detail}")
    value = result.stdout.removesuffix("\n")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise SafeGitError(f"Repository {label} is not one safe text line")
    return value


def inspect_repository(vault: Path, *, remote: str) -> RepositorySnapshot:
    """Read Git root, one remote URL, and current branch without mutation."""
    if not isinstance(remote, str) or not REMOTE_NAME_PATTERN.fullmatch(remote):
        raise ValueError("remote must be one safe Git remote name")
    vault_path = vault.expanduser().resolve()
    root_text = _single_git_line(
        run_git_command(vault_path, "rev-parse", "--show-toplevel"),
        label="root",
    )
    remote_url = _single_git_line(
        run_git_command(vault_path, "remote", "get-url", remote),
        label=f"remote {remote!r}",
    )
    branch = _single_git_line(
        run_git_command(vault_path, "branch", "--show-current"),
        label="branch",
    )
    if not root_text:
        raise SafeGitError("Repository root is empty")
    return RepositorySnapshot(
        root=Path(root_text).expanduser().resolve(),
        remote=remote,
        remote_url=remote_url,
        branch=branch,
    )


def read_git_blob(
    vault: Path,
    object_name: str,
    *,
    max_bytes: int,
    missing_ok: bool = True,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes | None:
    """Read one Git blob after an immutable object-size preflight.

    ``object_name`` may be an object ID, ``<commit>:<path>``, or an index
    expression such as ``:<path>``.  Git receives it as one argument; no shell
    is involved.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be positive")
    if (
        not isinstance(object_name, str)
        or not object_name
        or len(object_name) > MAX_OBJECT_NAME_CHARS
        or object_name.startswith("-")
        or "\x00" in object_name
    ):
        raise ValueError("object_name must be a bounded non-empty Git object expression")

    vault_path = vault.expanduser().resolve()
    resolved_result = _run_git(
        vault_path,
        "rev-parse",
        "--verify",
        "--quiet",
        object_name,
        max_stdout_bytes=MAX_SIZE_OUTPUT_BYTES,
        timeout=timeout,
    )
    if resolved_result.returncode != 0:
        if resolved_result.returncode == 1 and missing_ok:
            return None
        raise SafeGitError(
            f"Could not resolve Git blob {object_name}: "
            f"{_detail(resolved_result.stdout, resolved_result.stderr)}"
        )
    try:
        object_id = resolved_result.stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise SafeGitError(
            f"Git object {object_name} resolved to an invalid object ID"
        ) from exc
    if not OID_PATTERN.fullmatch(object_id):
        raise SafeGitError(
            f"Git object {object_name} resolved to an invalid object ID"
        )

    size_result = _run_git(
        vault_path,
        "cat-file",
        "-s",
        object_id,
        max_stdout_bytes=MAX_SIZE_OUTPUT_BYTES,
        timeout=timeout,
    )
    if size_result.returncode != 0:
        raise SafeGitError(
            f"Could not inspect Git object {object_name}: "
            f"{_detail(size_result.stdout, size_result.stderr)}"
        )

    try:
        size_text = size_result.stdout.decode("ascii").strip()
        if not size_text or not size_text.isdecimal():
            raise ValueError
        size = int(size_text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SafeGitError(
            f"Git object {object_name} reported an invalid byte size"
        ) from exc
    if size > max_bytes:
        raise SafeGitError(
            f"Git object {object_name} exceeds the {max_bytes}-byte safety limit"
        )

    blob_result = _run_git(
        vault_path,
        "cat-file",
        "blob",
        object_id,
        max_stdout_bytes=max_bytes,
        timeout=timeout,
    )
    if blob_result.returncode != 0:
        raise SafeGitError(
            f"Could not read Git object {object_name}: "
            f"{_detail(blob_result.stdout, blob_result.stderr)}"
        )
    if len(blob_result.stdout) != size:
        raise SafeGitError(
            f"Git object {object_name} changed size while it was read"
        )
    return blob_result.stdout


def verify_index_versions(
    vault: Path,
    *,
    base_commit: str,
    expected_sha256_by_path: Mapping[str, str | None],
    max_blob_bytes: int,
) -> None:
    """Reject index versions other than the base or registered content."""
    if not isinstance(base_commit, str) or not OID_PATTERN.fullmatch(base_commit):
        raise ValueError("base_commit must be one lowercase Git commit ID")
    if (
        isinstance(max_blob_bytes, bool)
        or not isinstance(max_blob_bytes, int)
        or max_blob_bytes <= 0
    ):
        raise ValueError("max_blob_bytes must be a positive integer")
    if len(expected_sha256_by_path) > MAX_DIRTY_PATHS:
        raise SafeGitError(
            f"Index verification exceeds the {MAX_DIRTY_PATHS}-path safety limit"
        )

    for raw_path, expected_digest in sorted(expected_sha256_by_path.items()):
        try:
            relative = relative_posix_path(
                raw_path,
                max_chars=MAX_GIT_PATH_CHARS,
                label="Index verification path",
            ).as_posix()
        except SafePathError as exc:
            raise SafeGitError(str(exc)) from exc
        if expected_digest is not None and (
            not isinstance(expected_digest, str)
            or not SHA256_PATTERN.fullmatch(expected_digest)
        ):
            raise ValueError(
                f"Expected digest for {relative} must be lowercase SHA-256 or null"
            )

        base_content = read_git_blob(
            vault,
            f"{base_commit}:{relative}",
            max_bytes=max_blob_bytes,
        )
        index_content = read_git_blob(
            vault,
            f":{relative}",
            max_bytes=max_blob_bytes,
        )
        base_digest = (
            hashlib.sha256(base_content).hexdigest()
            if base_content is not None
            else None
        )
        index_digest = (
            hashlib.sha256(index_content).hexdigest()
            if index_content is not None
            else None
        )
        allowed = {expected_digest}
        if base_digest is not None:
            allowed.add(base_digest)
        elif index_content is None:
            allowed.add(None)
        if index_digest not in allowed:
            raise SafeGitError(
                f"Git index contains an unregistered version: {relative}"
            )
