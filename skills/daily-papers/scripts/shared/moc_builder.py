#!/usr/bin/env python3

from __future__ import annotations

from collections import deque
import hashlib
import stat
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Iterable

from safe_io import SafeIOError, atomic_write_bytes, read_regular_bytes


GENERATED_MARKER = "generated_by: dailypaper-skills"
DEFAULT_EXCLUDED_DIR_NAMES = frozenset({"assets"})
MAX_MOC_BYTES = 16 * 1024 * 1024
MAX_MOC_DIRECTORIES = 100_000
MAX_MOC_NOTES = 500_000
MAX_DIRECTORY_ENTRIES = 100_000


class MOCConflictError(RuntimeError):
    """A generated page would escape the Vault or overwrite user-owned data."""


class MOCApplyError(MOCConflictError):
    """Applying a validated plan stopped after a known set of durable writes."""

    def __init__(self, message: str, changed_paths: Iterable[str]) -> None:
        super().__init__(message)
        self.changed_paths = tuple(changed_paths)


@dataclass
class MOCSummary:
    root_dir: Path
    total_directories: int = 0
    created_files: int = 0
    updated_files: int = 0
    unchanged_files: int = 0
    indexed_notes: int = 0
    changed_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "root_dir": str(self.root_dir),
            "total_directories": self.total_directories,
            "created_files": self.created_files,
            "updated_files": self.updated_files,
            "unchanged_files": self.unchanged_files,
            "indexed_notes": self.indexed_notes,
            "changed_paths": list(self.changed_paths),
        }


@dataclass(frozen=True)
class MOCWrite:
    path: Path
    relative_path: str
    content: str
    expected_sha256: str | None
    mode: int


@dataclass
class MOCPlan:
    summary: MOCSummary
    writes: tuple[MOCWrite, ...]


@dataclass(frozen=True)
class DirectorySnapshot:
    """One immutable directory projection used by the complete MOC plan."""

    path: Path
    subdirs: tuple[Path, ...]
    notes: tuple[Path, ...]


def _content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def plan_tree_mocs(
    *,
    vault_root: Path,
    root_dir: Path,
    title_prefix: str,
    intro: str,
    exclude_dir_names: Iterable[str] = (),
    protected_paths: Iterable[str] = (),
) -> MOCPlan:
    vault_root = vault_root.expanduser().resolve()
    root_dir = root_dir.expanduser().resolve()
    try:
        root_dir.relative_to(vault_root)
    except ValueError as exc:
        raise MOCConflictError("MOC root must remain inside the Vault") from exc
    root_dir.mkdir(parents=True, exist_ok=True)
    summary = MOCSummary(root_dir=root_dir)
    writes: list[MOCWrite] = []
    excluded = DEFAULT_EXCLUDED_DIR_NAMES | set(exclude_dir_names)
    protected = set(protected_paths)

    snapshots = _scan_tree(vault_root, root_dir, excluded)
    snapshot_by_path = {snapshot.path: snapshot for snapshot in snapshots}

    for snapshot in snapshots:
        directory = snapshot.path
        summary.total_directories += 1
        summary.indexed_notes += len(snapshot.notes)
        content = _build_moc_content(
            vault_root=vault_root,
            root_dir=root_dir,
            snapshot=snapshot,
            snapshot_by_path=snapshot_by_path,
            title_prefix=title_prefix,
            intro=intro,
        )
        encoded_content = content.encode("utf-8")
        if len(encoded_content) > MAX_MOC_BYTES:
            raise MOCConflictError(
                f"Generated MOC exceeds the {MAX_MOC_BYTES}-byte safety limit: "
                f"{directory.relative_to(vault_root).as_posix()}"
            )
        moc_path = directory / f"{directory.name}.md"
        relative_moc = moc_path.relative_to(vault_root).as_posix()
        try:
            previous_bytes = read_regular_bytes(
                moc_path,
                max_bytes=MAX_MOC_BYTES,
                required=False,
                label="MOC target",
            )
        except SafeIOError as exc:
            raise MOCConflictError(
                f"MOC target must be a bounded regular non-symlink file: "
                f"{relative_moc}"
            ) from exc
        if previous_bytes is None:
            if relative_moc in protected:
                raise MOCConflictError(
                    f"Refusing to replace a dirty MOC path: {relative_moc}"
                )
            summary.created_files += 1
            summary.changed_paths.append(relative_moc)
            writes.append(
                MOCWrite(
                    path=moc_path,
                    relative_path=relative_moc,
                    content=content,
                    expected_sha256=None,
                    mode=0o644,
                )
            )
            continue
        try:
            previous = previous_bytes.decode("utf-8")
            metadata = moc_path.stat(follow_symlinks=False)
        except UnicodeDecodeError as exc:
            raise MOCConflictError(
                f"MOC target is not valid UTF-8: {relative_moc}"
            ) from exc
        except OSError as exc:
            raise MOCConflictError(
                f"MOC target changed while planning: {relative_moc}"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise MOCConflictError(
                f"MOC target must remain a regular file: {relative_moc}"
            )
        if previous == content:
            summary.unchanged_files += 1
            continue
        if GENERATED_MARKER not in previous:
            raise MOCConflictError(
                f"Refusing to overwrite user-owned Markdown: {relative_moc}"
            )
        if relative_moc in protected:
            raise MOCConflictError(
                f"Refusing to replace a dirty MOC path: {relative_moc}"
            )
        summary.updated_files += 1
        summary.changed_paths.append(relative_moc)
        writes.append(
            MOCWrite(
                path=moc_path,
                relative_path=relative_moc,
                content=content,
                expected_sha256=_content_sha256(previous.encode("utf-8")),
                mode=stat.S_IMODE(metadata.st_mode),
            )
        )

    return MOCPlan(summary=summary, writes=tuple(writes))


def _verify_moc_write(write: MOCWrite) -> None:
    path = write.path
    try:
        current = read_regular_bytes(
            path,
            max_bytes=MAX_MOC_BYTES,
            required=write.expected_sha256 is not None,
            label="MOC target",
        )
    except SafeIOError as exc:
        raise MOCConflictError(
            f"Could not safely verify MOC target: {write.relative_path}"
        ) from exc
    if write.expected_sha256 is None:
        if current is not None:
            raise MOCConflictError(
                f"MOC target appeared after planning: {write.relative_path}"
            )
        return
    if current is None:
        raise MOCConflictError(
            f"MOC target disappeared after planning: {write.relative_path}"
        )
    if _content_sha256(current) != write.expected_sha256:
        raise MOCConflictError(
            f"MOC target changed after planning: {write.relative_path}"
        )


def apply_moc_plans(plans: Iterable[MOCPlan]) -> list[MOCSummary]:
    """Validate every target before replacing any file, then apply with CAS."""
    materialized = list(plans)
    writes = [write for plan in materialized for write in plan.writes]
    for write in writes:
        _verify_moc_write(write)

    changed: list[str] = []
    for write in writes:
        try:
            # Recheck immediately before replace to close the plan/apply window.
            _verify_moc_write(write)
            _atomic_write_text(write.path, write.content, mode=write.mode)
        except (MOCConflictError, OSError) as exc:
            raise MOCApplyError(
                f"Could not apply MOC plan at {write.relative_path}: {exc}",
                changed,
            ) from exc
        changed.append(write.relative_path)
    return [plan.summary for plan in materialized]


def build_tree_mocs(
    *,
    vault_root: Path,
    root_dir: Path,
    title_prefix: str,
    intro: str,
    exclude_dir_names: Iterable[str] = (),
    protected_paths: Iterable[str] = (),
) -> MOCSummary:
    """Compatibility wrapper around the plan/apply deep interface."""
    plan = plan_tree_mocs(
        vault_root=vault_root,
        root_dir=root_dir,
        title_prefix=title_prefix,
        intro=intro,
        exclude_dir_names=exclude_dir_names,
        protected_paths=protected_paths,
    )
    return apply_moc_plans([plan])[0]


def _atomic_write_text(path: Path, content: str, *, mode: int | None = None) -> None:
    """Durably replace one MOC without exposing a truncated intermediate file."""
    target_mode = (
        mode
        if mode is not None
        else (stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644)
    )
    try:
        atomic_write_bytes(
            path,
            content.encode("utf-8"),
            mode=target_mode,
            label="MOC",
        )
    except SafeIOError as exc:
        raise MOCConflictError(str(exc)) from exc


def _directory_entries(directory: Path) -> tuple[Path, ...]:
    try:
        entries = list(
            islice(directory.iterdir(), MAX_DIRECTORY_ENTRIES + 1)
        )
    except OSError as exc:
        raise MOCConflictError(f"Could not scan MOC directory: {directory}") from exc
    if len(entries) > MAX_DIRECTORY_ENTRIES:
        raise MOCConflictError(
            f"MOC directory exceeds the {MAX_DIRECTORY_ENTRIES}-entry safety limit: "
            f"{directory}"
        )
    return tuple(sorted(entries, key=lambda child: child.name))


def _scan_tree(
    vault_root: Path,
    root_dir: Path,
    exclude_dir_names: set[str],
) -> tuple[DirectorySnapshot, ...]:
    """Snapshot the tree once so planning never rescans parents or children."""
    result: list[DirectorySnapshot] = []
    queue = deque([root_dir])
    try:
        root_stat = root_dir.stat()
    except OSError as exc:
        raise MOCConflictError(f"Could not inspect MOC root: {root_dir}") from exc
    visited = {(root_stat.st_dev, root_stat.st_ino)}
    note_count = 0

    while queue:
        current = queue.popleft()
        subdirs: list[Path] = []
        notes: list[Path] = []
        moc_name = f"{current.name}.md"
        for path in _directory_entries(current):
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise MOCConflictError(
                    f"MOC tree entry changed while scanning: {path}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if path.name.startswith(".") or path.name in exclude_dir_names:
                    continue
                resolved = path.resolve()
                try:
                    resolved.relative_to(vault_root)
                except ValueError as exc:
                    raise MOCConflictError(
                        f"Directory escapes the Vault: {path}"
                    ) from exc
                identity = (metadata.st_dev, metadata.st_ino)
                if identity in visited:
                    continue
                visited.add(identity)
                subdirs.append(path)
                queue.append(path)
                continue
            if (
                stat.S_ISREG(metadata.st_mode)
                and path.suffix == ".md"
                and not path.name.startswith(".")
                and path.name != moc_name
            ):
                notes.append(path)

        note_count += len(notes)
        if note_count > MAX_MOC_NOTES:
            raise MOCConflictError(
                f"MOC tree exceeds the {MAX_MOC_NOTES}-note safety limit"
            )
        result.append(
            DirectorySnapshot(
                path=current,
                subdirs=tuple(subdirs),
                notes=tuple(notes),
            )
        )
        if len(result) + len(queue) > MAX_MOC_DIRECTORIES:
            raise MOCConflictError(
                f"MOC tree exceeds the {MAX_MOC_DIRECTORIES}-directory safety limit"
            )

    return tuple(result)


def _build_moc_content(
    *,
    vault_root: Path,
    root_dir: Path,
    snapshot: DirectorySnapshot,
    snapshot_by_path: dict[Path, DirectorySnapshot],
    title_prefix: str,
    intro: str,
) -> str:
    directory = snapshot.path
    relative_dir = directory.relative_to(root_dir)
    display_name = _display_name(root_dir, directory)

    frontmatter = "\n".join(
        [
            "---",
            "tags: [MOC, auto-generated]",
            "generated_by: dailypaper-skills",
            "---",
            "",
        ]
    )

    lines = [
        f"# {title_prefix}：{display_name}",
        "",
        intro,
        "",
    ]

    if directory == root_dir:
        lines.append(
            f"- 根目录：`{root_dir.relative_to(vault_root).as_posix()}`"
        )
    else:
        lines.append(f"- 当前目录：`{relative_dir.as_posix()}`")
    lines.append("")

    subdirs = snapshot.subdirs
    notes = snapshot.notes

    if subdirs:
        lines.extend(["## 子目录", ""])
        for subdir in subdirs:
            child = snapshot_by_path[subdir]
            note_count = len(child.notes)
            child_count = len(child.subdirs)
            lines.append(
                f"- [[{_wikilink(subdir / f'{subdir.name}.md', vault_root)}|{subdir.name}]]"
                f" · {note_count} 篇笔记 · {child_count} 个子目录"
            )
        lines.append("")

    if notes:
        lines.extend(["## 当前目录笔记", ""])
        for note in notes:
            lines.append(f"- [[{_wikilink(note, vault_root)}|{note.stem}]]")
        lines.append("")

    if not subdirs and not notes:
        lines.extend(["## 当前目录笔记", "", "- 暂无内容", ""])

    lines.extend(
        [
            "## 说明",
            "",
            "- 这个目录页由脚本自动生成。",
            "- 你手动新增、移动或重命名笔记后，可以再运行一次“更新索引”。",
            "",
        ]
    )

    return frontmatter + "\n".join(lines)


def _display_name(root_dir: Path, directory: Path) -> str:
    if directory == root_dir and directory.name.startswith("_"):
        return directory.name.lstrip("_") or directory.name
    return directory.name


def _wikilink(path: Path, vault_root: Path) -> str:
    return path.relative_to(vault_root).with_suffix("").as_posix()
