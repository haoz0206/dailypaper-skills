#!/usr/bin/env python3
"""Portable relative-path parsing and containment for DailyPaper inputs."""

from __future__ import annotations

import stat
from pathlib import Path, PurePosixPath
from typing import Any


MAX_RELATIVE_PATH_CHARS = 4096


class SafePathError(ValueError):
    """A path is not a portable, normalized relative POSIX path."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def relative_posix_path(
    value: Any,
    *,
    max_chars: int = MAX_RELATIVE_PATH_CHARS,
    label: str = "Path",
) -> PurePosixPath:
    """Parse one portable normalized relative path without filesystem access."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if not isinstance(label, str) or not label:
        raise ValueError("label must be a non-empty string")
    if not isinstance(value, str):
        raise SafePathError(
            "invalid-type",
            f"{label} must be a non-empty relative POSIX path",
        )
    if not value:
        raise SafePathError(
            "empty",
            f"{label} must be a non-empty relative POSIX path",
        )
    if len(value) > max_chars:
        raise SafePathError(
            "too-long",
            f"{label} exceeds the {max_chars}-character safety limit",
        )
    if "\\" in value:
        raise SafePathError(
            "separator",
            f"{label} must use POSIX '/' separators",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SafePathError(
            "control-character",
            f"{label} contains a control character",
        )

    path = PurePosixPath(value)
    if path.is_absolute():
        raise SafePathError(
            "absolute",
            f"{label} must be a normalized relative POSIX path without '.' or '..'"
        )
    if path == PurePosixPath("."):
        raise SafePathError(
            "root",
            f"{label} must be a normalized relative POSIX path without '.' or '..'",
        )
    if ".." in path.parts:
        raise SafePathError(
            "traversal",
            f"{label} must be a normalized relative POSIX path without '.' or '..'",
        )
    if path.as_posix() != value:
        raise SafePathError(
            "non-normalized",
            f"{label} must be a normalized relative POSIX path without '.' or '..'",
        )
    return path


def resolve_within(
    root: Path,
    value: Any,
    *,
    max_chars: int = MAX_RELATIVE_PATH_CHARS,
    label: str = "Path",
) -> Path:
    """Resolve a portable relative path and reject symlink or lexical escape."""
    relative = relative_posix_path(value, max_chars=max_chars, label=label)
    try:
        resolved_root = root.expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise SafePathError(
            "escape",
            f"{label} escapes its configured root",
        ) from exc

    candidate = resolved_root
    parts = relative.parts
    for index, part in enumerate(parts):
        candidate = candidate / part
        try:
            metadata = candidate.stat(follow_symlinks=False)
        except FileNotFoundError:
            return candidate.joinpath(*parts[index + 1 :])
        except OSError as exc:
            raise SafePathError(
                "uninspectable",
                f"{label} cannot be inspected safely",
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SafePathError(
                "symlink",
                f"{label} traverses a symlink",
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise SafePathError(
                "not-directory",
                f"{label} traverses a non-directory component",
            )
    return candidate
