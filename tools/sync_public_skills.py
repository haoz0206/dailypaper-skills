#!/usr/bin/env python3
"""Materialize self-contained public Skills from the canonical DailyPaper suite."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_ROOT = REPO_ROOT / "skills" / "daily-papers"
SKILLS_ROOT = REPO_ROOT / "skills"
SHARED_ROOT = SUITE_ROOT / "scripts" / "shared"
if str(SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(SHARED_ROOT))

from safe_io import SafeIOError, atomic_write_bytes, read_regular_bytes


MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
MAX_GENERATED_TREE_ENTRIES = 4_096
MAX_GENERATED_TREE_DEPTH = 16


class SyncError(RuntimeError):
    """The public Skill tree cannot be inspected or materialized safely."""


@dataclass(frozen=True)
class PublicSkill:
    name: str
    description: str
    workflow: str
    resources: tuple[str, ...]


@dataclass(frozen=True)
class GeneratedTree:
    files: frozenset[Path]
    directories: frozenset[Path]
    protected_directories: frozenset[Path]


COMMON_CONFIG = (
    "scripts/shared/safe_io.py",
    "scripts/shared/safe_path.py",
    "scripts/shared/config_schema.py",
    "scripts/shared/machine_config.py",
    "scripts/shared/user_config.py",
    "scripts/shared/defaults.json",
)
RUNTIME_PREFLIGHT = (
    "scripts/shared/task_state.py",
    "scripts/shared/active_run_guard.py",
    "scripts/shared/runtime_context.py",
)
NETWORK_RUNTIME = (
    "scripts/shared/safe_http.py",
)
GIT_OBJECT_RUNTIME = (
    "scripts/shared/safe_process.py",
    "scripts/shared/safe_git.py",
)
MOC_RESOURCES = (
    "scripts/shared/moc_builder.py",
    "scripts/shared/refresh_mocs.py",
)
STANDALONE_SESSION = (
    *GIT_OBJECT_RUNTIME,
    "scripts/shared/run_guardian.py",
    "scripts/shared/standalone_coordinator.py",
    "references/standalone-session.md",
)
CONFIG_GUARD = (
    *GIT_OBJECT_RUNTIME,
    "scripts/shared/task_state.py",
    "scripts/shared/active_run_guard.py",
    "scripts/configure/config_manager.py",
    "scripts/configure/shared-config-v0-defaults.json",
)

PUBLIC_SKILLS = (
    PublicSkill(
        name="paper-reader",
        description=(
            "Read, analyze, summarize, and save academic papers from arXiv, "
            "local PDF files, or explicit Zotero inputs into an Obsidian Vault. "
            "Use for paper reading, critical analysis, formula extraction, "
            "paper notes, Zotero collection reading, “读一下这篇论文”, "
            "“快速看一下这篇论文”, or “批判性分析这篇论文”."
        ),
        workflow="workflows/paper-reader.md",
        resources=COMMON_CONFIG
        + RUNTIME_PREFLIGHT
        + NETWORK_RUNTIME
        + MOC_RESOURCES
        + STANDALONE_SESSION
        + (
            "scripts/shared/paper_identity.py",
            "assets/paper-note-template.md",
            "references/paper-reader/concept-categories.md",
            "references/paper-reader/cv-dl-terminology.md",
            "references/paper-reader/image-troubleshooting.md",
            "references/paper-reader/quality-standards.md",
            "references/paper-reader/reading-core.md",
            "references/paper-reader/zotero-guide.md",
            "scripts/daily/download_note_images.py",
            "scripts/paper-reader/validate_paper_note.py",
            "scripts/paper-reader/zotero_helper.py",
        ),
    ),
    PublicSkill(
        name="generate-mocs",
        description=(
            "Regenerate deterministic Obsidian Map of Content pages for "
            "DailyPaper paper notes and concept notes. Use when the user asks "
            "to update indexes, refresh navigation pages, or rebuild MOCs after "
            "moving, adding, or renaming notes, including “更新索引”, "
            "“刷新论文目录”, and “更新 MOC”."
        ),
        workflow="workflows/generate-mocs.md",
        resources=(
            COMMON_CONFIG
            + RUNTIME_PREFLIGHT
            + MOC_RESOURCES
            + STANDALONE_SESSION
        ),
    ),
    PublicSkill(
        name="configure-dailypaper",
        description=(
            "Perform first-run DailyPaper onboarding and safely inspect or "
            "migrate or update configuration. Use immediately after installation "
            "to set the per-machine Vault path, initialize or validate the fixed "
            "Vault repository, configure optional Zotero paths, or change research "
            "keywords, arXiv categories, thresholds, and index automation. Use "
            "for “配置每日论文”, “查看当前每日论文配置”, or first-run setup."
        ),
        workflow="workflows/configure.md",
        resources=COMMON_CONFIG
        + CONFIG_GUARD
        + (
            "scripts/configure/onboard.py",
            "scripts/shared/run_guardian.py",
            "scripts/shared/run_lifecycle.py",
            "scripts/shared/vault_coordination.py",
            "references/configuration-migration.md",
        ),
    ),
)


def _source_bytes(relative: Path) -> bytes:
    if (
        relative.is_absolute()
        or relative == Path(".")
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise SyncError(f"Unsafe canonical resource path: {relative}")
    try:
        resolved_root = SUITE_ROOT.resolve(strict=True)
        resolved_parent = (resolved_root / relative.parent).resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SyncError(
            f"Canonical resource path escapes the suite: {relative}"
        ) from exc
    source = resolved_parent / relative.name
    try:
        payload = read_regular_bytes(
            source,
            max_bytes=MAX_SOURCE_FILE_BYTES,
            label="Canonical Skill resource",
        )
    except SafeIOError as exc:
        raise SyncError(str(exc)) from exc
    if payload is None:
        raise SyncError(f"Canonical Skill resource does not exist: {relative}")
    return payload


def _skill_markdown(skill: PublicSkill) -> bytes:
    try:
        workflow = _source_bytes(Path(skill.workflow)).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SyncError(
            f"Canonical Skill workflow is not valid UTF-8: {skill.workflow}"
        ) from exc
    header = (
        "---\n"
        f"name: {skill.name}\n"
        "description: |\n"
        + "\n".join(
            f"  {line}" for line in _wrap_description(skill.description)
        )
        + "\n---\n\n"
        "<!-- Generated by tools/sync_public_skills.py; edit the canonical "
        "workflow in skills/daily-papers. -->\n\n"
    )
    return (header + workflow).encode("utf-8")


def _wrap_description(value: str, width: int = 78) -> list[str]:
    words = value.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _expected_files(skill: PublicSkill) -> dict[Path, bytes]:
    expected = {Path("SKILL.md"): _skill_markdown(skill)}
    for relative in skill.resources:
        resource = Path(relative)
        if resource in expected:
            raise SyncError(
                f"Duplicate public resource for {skill.name}: {relative}"
            )
        expected[resource] = _source_bytes(resource)
    return expected


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _scan_generated_tree(root: Path) -> GeneratedTree:
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return GeneratedTree(frozenset(), frozenset(), frozenset())
    except OSError as exc:
        raise SyncError(f"Cannot inspect generated Skill root: {root}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise SyncError(
            f"Generated Skill root is not a regular directory: {root}"
        )

    files: set[Path] = set()
    directories: set[Path] = set()
    protected_directories: set[Path] = set()
    stack = [(root, Path("."), 0)]
    entry_count = 0
    while stack:
        directory, relative_root, depth = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    if entry.name == "__pycache__":
                        protected = relative_root
                        while protected != Path("."):
                            protected_directories.add(protected)
                            protected = protected.parent
                        continue
                    entry_count += 1
                    if entry_count > MAX_GENERATED_TREE_ENTRIES:
                        raise SyncError(
                            f"Generated Skill tree exceeds the "
                            f"{MAX_GENERATED_TREE_ENTRIES}-entry limit: {root}"
                        )
                    entries.append(entry)
        except SyncError:
            raise
        except OSError as exc:
            raise SyncError(
                f"Cannot scan generated Skill directory: {directory}"
            ) from exc

        entries.sort(key=lambda entry: entry.name)
        for entry in reversed(entries):
            relative = relative_root / entry.name
            try:
                if entry.is_symlink():
                    raise SyncError(
                        f"Generated Skill tree contains a symlink: "
                        f"{root / relative}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    if depth >= MAX_GENERATED_TREE_DEPTH:
                        raise SyncError(
                            f"Generated Skill tree exceeds the "
                            f"{MAX_GENERATED_TREE_DEPTH}-level depth limit: "
                            f"{root / relative}"
                        )
                    directories.add(relative)
                    stack.append((root / relative, relative, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    files.add(relative)
                else:
                    raise SyncError(
                        f"Generated Skill tree contains a special file: "
                        f"{root / relative}"
                    )
            except OSError as exc:
                raise SyncError(
                    f"Cannot classify generated Skill entry: {root / relative}"
                ) from exc
    return GeneratedTree(
        files=frozenset(files),
        directories=frozenset(directories),
        protected_directories=frozenset(protected_directories),
    )


def _expected_directories(files: set[Path]) -> set[Path]:
    directories: set[Path] = set()
    for relative in files:
        parent = relative.parent
        while parent != Path("."):
            directories.add(parent)
            parent = parent.parent
    return directories


def _matches_generated_file(path: Path, expected: bytes) -> bool:
    try:
        current = read_regular_bytes(
            path,
            max_bytes=max(len(expected), 1),
            label="Generated Skill resource",
        )
    except SafeIOError:
        return False
    return current == expected


def sync(*, check: bool) -> list[str]:
    problems: list[str] = []
    for skill in PUBLIC_SKILLS:
        target_root = SKILLS_ROOT / skill.name
        expected = _expected_files(skill)
        try:
            actual = _scan_generated_tree(target_root)
        except SyncError as exc:
            if check:
                problems.append(f"unsafe: {exc}")
                continue
            raise

        for relative, content in expected.items():
            target = target_root / relative
            matches = relative in actual.files and _matches_generated_file(
                target,
                content,
            )
            if check:
                if relative not in actual.files:
                    problems.append(f"missing: {_display_path(target)}")
                elif not matches:
                    problems.append(f"stale: {_display_path(target)}")
                continue
            if matches:
                continue
            try:
                atomic_write_bytes(
                    target,
                    content,
                    mode=0o644,
                    preserve_existing_mode=True,
                    label="Generated Skill resource",
                )
            except SafeIOError as exc:
                raise SyncError(str(exc)) from exc

        expected_paths = set(expected)
        extra_files = set(actual.files) - expected_paths
        extra_directories = (
            set(actual.directories)
            - _expected_directories(expected_paths)
            - set(actual.protected_directories)
        )
        if check:
            problems.extend(
                f"unexpected: {_display_path(target_root / path)}"
                for path in sorted(extra_files)
            )
            problems.extend(
                f"unexpected directory: {_display_path(target_root / path)}"
                for path in sorted(extra_directories)
            )
        else:
            for relative in sorted(extra_files):
                try:
                    (target_root / relative).unlink()
                except OSError as exc:
                    raise SyncError(
                        f"Cannot prune generated Skill resource: "
                        f"{target_root / relative}"
                    ) from exc
            for relative in sorted(
                extra_directories,
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    (target_root / relative).rmdir()
                except OSError as exc:
                    raise SyncError(
                        f"Cannot prune generated Skill directory: "
                        f"{target_root / relative}"
                    ) from exc
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        problems = sync(check=args.check)
    except SyncError as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    if not args.check:
        print(
            "Synced public skills: "
            + ", ".join(skill.name for skill in PUBLIC_SKILLS)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
