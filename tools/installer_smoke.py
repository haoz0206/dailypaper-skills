#!/usr/bin/env python3
"""Install every public Skill through the pinned CLI and compare exact trees."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_CLI = "skills@1.5.20"
PUBLIC_SKILLS = (
    "configure-dailypaper",
    "daily-papers",
    "paper-reader",
    "generate-mocs",
)
MAX_TREE_ENTRIES = 5_000
MAX_TREE_DEPTH = 20
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TREE_BYTES = 512 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 180


class InstallerSmokeError(RuntimeError):
    """The public installation interface did not preserve the Skill packages."""


def _hash_regular_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallerSmokeError(f"Cannot open installed file safely: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise InstallerSmokeError(f"Installed entry is not a regular file: {path}")
        if metadata.st_size > MAX_FILE_BYTES:
            raise InstallerSmokeError(
                f"Installed file exceeds {MAX_FILE_BYTES} bytes: {path}"
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(descriptor, 64 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_FILE_BYTES:
                raise InstallerSmokeError(
                    f"Installed file grew beyond {MAX_FILE_BYTES} bytes: {path}"
                )
            digest.update(block)
        return total, digest.hexdigest()
    finally:
        os.close(descriptor)


def _snapshot_tree(root: Path) -> dict[str, tuple[int, str]]:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise InstallerSmokeError(f"Installed Skill root is missing: {root}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise InstallerSmokeError(
            f"Installed Skill root must be a real directory: {root}"
        )

    files: dict[str, tuple[int, str]] = {}
    stack = [(root, Path("."), 0)]
    entries_seen = 0
    total_bytes = 0
    while stack:
        directory, relative_root, depth = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError as exc:
            raise InstallerSmokeError(
                f"Cannot inspect installed Skill directory: {directory}"
            ) from exc
        with iterator:
            for entry in iterator:
                entries_seen += 1
                if entries_seen > MAX_TREE_ENTRIES:
                    raise InstallerSmokeError(
                        f"Installed Skill tree exceeds {MAX_TREE_ENTRIES} "
                        f"entries: {root}"
                    )
                relative = relative_root / entry.name
                try:
                    if entry.is_symlink():
                        raise InstallerSmokeError(
                            "Copied Skill unexpectedly contains a symlink: "
                            f"{root / relative}"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name == "__pycache__":
                            continue
                        if depth >= MAX_TREE_DEPTH:
                            raise InstallerSmokeError(
                                f"Installed Skill exceeds {MAX_TREE_DEPTH} "
                                f"levels: {root}"
                            )
                        stack.append((root / relative, relative, depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        raise InstallerSmokeError(
                            "Installed Skill contains a special file: "
                            f"{root / relative}"
                        )
                except OSError as exc:
                    raise InstallerSmokeError(
                        f"Cannot classify installed Skill entry: {root / relative}"
                    ) from exc
                if entry.name.endswith(".pyc"):
                    continue
                size, digest = _hash_regular_file(root / relative)
                total_bytes += size
                if total_bytes > MAX_TREE_BYTES:
                    raise InstallerSmokeError(
                        f"Installed Skill tree exceeds {MAX_TREE_BYTES} bytes: {root}"
                    )
                files[relative.as_posix()] = (size, digest)
    return files


def _run(command: list[str], *, cwd: Path) -> None:
    environment = {**os.environ, "CI": "1", "NO_COLOR": "1"}
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallerSmokeError(f"Could not run {' '.join(command)}: {exc}") from exc
    if result.returncode != 0:
        raise InstallerSmokeError(
            f"{' '.join(command)} failed with exit {result.returncode}"
        )


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="dailypaper-installer-smoke-") as temp:
            install_root = Path(temp)
            _run(
                [
                    "npx",
                    "--yes",
                    SKILLS_CLI,
                    "add",
                    str(REPO_ROOT),
                    "--skill",
                    *PUBLIC_SKILLS,
                    "--agent",
                    "claude-code",
                    "codex",
                    "--yes",
                    "--copy",
                ],
                cwd=install_root,
            )
            for skill_name in PUBLIC_SKILLS:
                expected = _snapshot_tree(REPO_ROOT / "skills" / skill_name)
                for harness_root in (".agents/skills", ".claude/skills"):
                    installed = _snapshot_tree(
                        install_root / harness_root / skill_name
                    )
                    if installed != expected:
                        missing = sorted(set(expected) - set(installed))
                        extra = sorted(set(installed) - set(expected))
                        changed = sorted(
                            path
                            for path in set(expected) & set(installed)
                            if expected[path] != installed[path]
                        )
                        raise InstallerSmokeError(
                            f"{skill_name} differs after install into {harness_root}; "
                            f"missing={missing}, extra={extra}, changed={changed}"
                        )
            _run(
                [
                    sys.executable,
                    "-m",
                    "compileall",
                    "-q",
                    str(install_root / ".agents" / "skills"),
                    str(install_root / ".claude" / "skills"),
                ],
                cwd=install_root,
            )
    except InstallerSmokeError as exc:
        print(f"Installer smoke test failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Installer smoke test passed for {len(PUBLIC_SKILLS)} Skills via {SKILLS_CLI}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
