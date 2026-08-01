#!/usr/bin/env python3
"""Verify clean and previous-release upgrades through the pinned Skills CLI."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_CLI = "skills@1.5.20"
PREVIOUS_RELEASE_SOURCE = (
    "https://github.com/haoz0206/dailypaper-skills.git#v1.2.0"
)
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
HARNESS_ROOTS = (Path(".agents/skills"), Path(".claude/skills"))


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


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment_overrides: dict[str, str] | None = None,
) -> None:
    environment = {**os.environ, "CI": "1", "NO_COLOR": "1"}
    if environment_overrides:
        environment.update(environment_overrides)
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


def _install(
    source: str,
    *,
    install_root: Path,
    environment: dict[str, str],
    copy_mode: bool,
) -> None:
    command = [
        "npx",
        "--yes",
        SKILLS_CLI,
        "add",
        source,
        "--skill",
        *PUBLIC_SKILLS,
        "--agent",
        "claude-code",
        "codex",
        "--yes",
    ]
    if copy_mode:
        command.append("--copy")
    _run(
        command,
        cwd=install_root,
        environment_overrides=environment,
    )


def _describe_tree_difference(
    expected: dict[str, tuple[int, str]],
    installed: dict[str, tuple[int, str]],
) -> str:
    missing = sorted(set(expected) - set(installed))
    extra = sorted(set(installed) - set(expected))
    changed = sorted(
        path
        for path in set(expected) & set(installed)
        if expected[path] != installed[path]
    )
    return f"missing={missing}, extra={extra}, changed={changed}"


def _verify_claude_link(install_root: Path, skill_name: str) -> None:
    canonical = install_root / HARNESS_ROOTS[0] / skill_name
    claude = install_root / HARNESS_ROOTS[1] / skill_name
    try:
        metadata = claude.lstat()
        target = os.readlink(claude)
    except OSError as exc:
        raise InstallerSmokeError(
            f"Claude Code link is missing for {skill_name}: {claude}"
        ) from exc
    if not stat.S_ISLNK(metadata.st_mode):
        raise InstallerSmokeError(
            f"Claude Code entry is not a symlink for {skill_name}: {claude}"
        )
    expected_target = os.path.relpath(canonical, start=claude.parent)
    if target != expected_target:
        raise InstallerSmokeError(
            f"Claude Code link for {skill_name} targets {target!r}, "
            f"expected {expected_target!r}"
        )


def _verify_installation(
    install_root: Path,
    *,
    phase: str,
    copy_mode: bool,
) -> None:
    for skill_name in PUBLIC_SKILLS:
        expected = _snapshot_tree(REPO_ROOT / "skills" / skill_name)
        codex = _snapshot_tree(install_root / HARNESS_ROOTS[0] / skill_name)
        if codex != expected:
            raise InstallerSmokeError(
                f"{skill_name} differs from the checkout after {phase}; "
                f"{_describe_tree_difference(expected, codex)}"
            )
        if copy_mode:
            claude = _snapshot_tree(
                install_root / HARNESS_ROOTS[1] / skill_name
            )
            if codex != claude:
                raise InstallerSmokeError(
                    f"{skill_name} differs between Codex and Claude Code after "
                    f"{phase}; {_describe_tree_difference(codex, claude)}"
                )
        else:
            _verify_claude_link(install_root, skill_name)


def _write_config_sentinels(state_root: Path) -> tuple[Path, Path]:
    vault = state_root / "vault"
    machine_config = state_root / "machine" / "config.json"
    vault_config = vault / ".dailypaper" / "config.json"
    machine_config.parent.mkdir(parents=True)
    vault_config.parent.mkdir(parents=True)
    machine_config.write_text(
        json.dumps(
            {"version": 1, "vault_path": str(vault)},
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    vault_config.write_text(
        '{"schema_version":1,"sentinel":"preserve-user-vault-config"}\n',
        encoding="utf-8",
    )
    return machine_config, vault


def _seed_stale_install_artifacts(
    install_root: Path,
    *,
    copy_mode: bool,
) -> None:
    harness_roots = HARNESS_ROOTS if copy_mode else HARNESS_ROOTS[:1]
    for harness_root in harness_roots:
        for skill_name in PUBLIC_SKILLS:
            marker = install_root / harness_root / skill_name / ".upgrade-stale"
            try:
                marker.write_text(
                    "must be replaced by the upgrade\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                raise InstallerSmokeError(
                    "Could not prepare the previous-release installation for "
                    f"upgrade: {marker}"
                ) from exc


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="dailypaper-installer-smoke-") as temp:
            temp_root = Path(temp)
            clean_root = temp_root / "clean-install"
            copy_upgrade_root = temp_root / "copy-upgrade"
            link_upgrade_root = temp_root / "link-upgrade"
            clean_root.mkdir()
            copy_upgrade_root.mkdir()
            link_upgrade_root.mkdir()
            machine_config, vault = _write_config_sentinels(temp_root / "state")
            state_before = _snapshot_tree(temp_root / "state")
            environment = {
                "npm_config_cache": str(temp_root / "npm-cache"),
                "DAILYPAPER_MACHINE_CONFIG": str(machine_config),
                "DAILYPAPER_VAULT": str(vault),
            }

            _install(
                str(REPO_ROOT),
                install_root=clean_root,
                environment=environment,
                copy_mode=True,
            )
            _verify_installation(
                clean_root,
                phase="clean copy install",
                copy_mode=True,
            )

            for upgrade_root, copy_mode in (
                (copy_upgrade_root, True),
                (link_upgrade_root, False),
            ):
                mode = "copy" if copy_mode else "link"
                _install(
                    PREVIOUS_RELEASE_SOURCE,
                    install_root=upgrade_root,
                    environment=environment,
                    copy_mode=copy_mode,
                )
                _seed_stale_install_artifacts(
                    upgrade_root,
                    copy_mode=copy_mode,
                )
                _install(
                    str(REPO_ROOT),
                    install_root=upgrade_root,
                    environment=environment,
                    copy_mode=copy_mode,
                )
                _verify_installation(
                    upgrade_root,
                    phase=f"previous-release {mode} upgrade",
                    copy_mode=copy_mode,
                )

            state_after = _snapshot_tree(temp_root / "state")
            if state_after != state_before:
                raise InstallerSmokeError(
                    "Machine or Vault configuration changed during installation; "
                    + _describe_tree_difference(state_before, state_after)
                )
            _run(
                [
                    sys.executable,
                    "-m",
                    "compileall",
                    "-q",
                    *(
                        str(root / harness_root)
                        for root in (
                            clean_root,
                            copy_upgrade_root,
                            link_upgrade_root,
                        )
                        for harness_root in HARNESS_ROOTS
                    ),
                ],
                cwd=temp_root,
                environment_overrides=environment,
            )
    except InstallerSmokeError as exc:
        print(f"Installer smoke test failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Installer smoke test passed for a clean copy install plus "
        f"previous-release copy and link upgrades of {len(PUBLIC_SKILLS)} "
        f"Skills via {SKILLS_CLI}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
