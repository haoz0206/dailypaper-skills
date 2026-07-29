#!/usr/bin/env python3
"""Small, strict file codecs shared by DailyPaper runtime boundaries.

Callers keep ownership of domain validation and error translation.  This
module owns the lower-level invariants that should not be reimplemented by
each workflow: no symlink following, regular-file checks, bounded reads,
strict UTF-8 JSON, duplicate-key rejection, and non-finite-number rejection.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4


class SafeIOError(ValueError):
    """A local file or serialized document failed a safety invariant."""


class JSONEncodingError(SafeIOError):
    """A Python value cannot be represented as strict UTF-8 JSON."""


class DocumentTooLargeError(SafeIOError):
    """A serialized or on-disk document exceeds its byte budget."""


@dataclass(frozen=True)
class RegularFileSnapshot:
    """One descriptor-pinned projection of a regular file."""

    size: int
    sha256: str
    prefix: bytes


def anchored_file_path(path: Path, *, label: str = "File") -> Path:
    """Resolve only a file path's parent, preserving its final component.

    Passing ``Path.resolve()`` output to a nofollow file primitive silently
    dereferences the final symlink before that primitive can reject it.  This
    helper gives callers a stable absolute parent while retaining the lexical
    filename for the eventual descriptor-level check.
    """
    candidate = path.expanduser()
    if not candidate.name or candidate.name in {".", ".."}:
        raise SafeIOError(f"{label} path has no safe file name: {candidate}")
    try:
        parent = candidate.parent.resolve()
    except (OSError, RuntimeError) as exc:
        raise SafeIOError(f"{label} parent cannot be resolved: {candidate.parent}") from exc
    return parent / candidate.name


def _open_regular_descriptor(
    path: Path,
    *,
    required: bool,
    label: str,
) -> int | None:
    """Open one regular file without following its final path component."""
    candidate = path.expanduser()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(candidate, flags)
    except FileNotFoundError:
        if required:
            raise SafeIOError(f"{label} file does not exist: {candidate}")
        return None
    except OSError as exc:
        raise SafeIOError(
            f"{label} is not a readable regular file: {candidate}"
        ) from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SafeIOError(f"{label} is not a regular file: {candidate}")
    except (OSError, SafeIOError):
        os.close(descriptor)
        raise
    return descriptor


def read_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
    required: bool = True,
    label: str = "File",
) -> bytes | None:
    """Race-safely read one bounded regular file without following a symlink."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    candidate = path.expanduser()
    descriptor = _open_regular_descriptor(
        candidate,
        required=required,
        label=label,
    )
    if descriptor is None:
        return None

    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > max_bytes:
            raise SafeIOError(
                f"{label} exceeds the {max_bytes}-byte safety limit"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise SafeIOError(f"{label} cannot be read: {candidate}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(raw) > max_bytes:
        raise SafeIOError(f"{label} exceeds the {max_bytes}-byte safety limit")
    return raw


def read_regular_prefix(
    path: Path,
    *,
    max_bytes: int,
    required: bool = True,
    label: str = "File",
) -> bytes | None:
    """Read at most one prefix from a regular file without following a symlink.

    Unlike ``read_regular_bytes``, this function intentionally permits a file
    larger than ``max_bytes``.  It is for bounded header inspection only.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    candidate = path.expanduser()
    descriptor = _open_regular_descriptor(
        candidate,
        required=required,
        label=label,
    )
    if descriptor is None:
        return None
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            return stream.read(max_bytes)
    except OSError as exc:
        raise SafeIOError(f"{label} cannot be read: {candidate}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _stream_regular_file(
    path: Path,
    *,
    max_bytes: int | None = None,
    prefix_bytes: int = 0,
    destination: BinaryIO | None = None,
    label: str = "File",
) -> RegularFileSnapshot:
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("max_bytes must be positive when provided")
    if (
        isinstance(prefix_bytes, bool)
        or not isinstance(prefix_bytes, int)
        or prefix_bytes < 0
    ):
        raise ValueError("prefix_bytes must be a non-negative integer")

    candidate = path.expanduser()
    descriptor = _open_regular_descriptor(
        candidate,
        required=True,
        label=label,
    )
    assert descriptor is not None
    digest = hashlib.sha256()
    prefix = bytearray()
    total = 0
    try:
        before = os.fstat(descriptor)
        if max_bytes is not None and before.st_size > max_bytes:
            raise DocumentTooLargeError(
                f"{label} exceeds the {max_bytes}-byte safety limit"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise DocumentTooLargeError(
                        f"{label} exceeds the {max_bytes}-byte safety limit"
                    )
                if len(prefix) < prefix_bytes:
                    prefix.extend(chunk[: prefix_bytes - len(prefix)])
                digest.update(chunk)
                if destination is not None:
                    written = destination.write(chunk)
                    if written is not None and written != len(chunk):
                        raise OSError("short destination write")
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise SafeIOError(f"{label} cannot be streamed safely: {candidate}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_after != identity_before or total != after.st_size:
        raise SafeIOError(f"{label} changed while it was being read: {candidate}")
    return RegularFileSnapshot(
        size=total,
        sha256=digest.hexdigest(),
        prefix=bytes(prefix),
    )


def inspect_regular_file(
    path: Path,
    *,
    max_bytes: int | None = None,
    prefix_bytes: int = 0,
    label: str = "File",
) -> RegularFileSnapshot:
    """Inspect one nofollow regular file through a single pinned descriptor."""
    return _stream_regular_file(
        path,
        max_bytes=max_bytes,
        prefix_bytes=prefix_bytes,
        label=label,
    )


def copy_regular_file(
    path: Path,
    destination: BinaryIO,
    *,
    max_bytes: int,
    prefix_bytes: int = 0,
    label: str = "File",
) -> RegularFileSnapshot:
    """Copy and inspect the exact same source bytes through one descriptor."""
    if not hasattr(destination, "write"):
        raise TypeError("destination must be a writable binary stream")
    return _stream_regular_file(
        path,
        max_bytes=max_bytes,
        prefix_bytes=prefix_bytes,
        destination=destination,
        label=label,
    )


def sha256_regular_file(
    path: Path,
    *,
    max_bytes: int | None = None,
    label: str = "File",
) -> str:
    """Hash one regular file through the shared descriptor snapshot seam."""
    return inspect_regular_file(
        path,
        max_bytes=max_bytes,
        label=label,
    ).sha256


def parse_json_value(
    raw: bytes,
    *,
    max_bytes: int,
    label: str = "JSON document",
) -> Any:
    """Parse one bounded, strict UTF-8 JSON value from immutable bytes."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if len(raw) > max_bytes:
        raise SafeIOError(f"{label} exceeds the {max_bytes}-byte safety limit")

    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SafeIOError(f"{label} contains duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise SafeIOError(
            f"{label} contains non-standard JSON value: {value}"
        )

    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=strict_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SafeIOError(
            f"{label} is not valid bounded UTF-8 JSON: {exc}"
        ) from exc
    return value


def parse_json_object(
    raw: bytes,
    *,
    max_bytes: int,
    label: str = "JSON document",
) -> dict[str, Any]:
    """Parse one bounded, strict UTF-8 JSON object from immutable bytes."""
    value = parse_json_value(raw, max_bytes=max_bytes, label=label)
    if not isinstance(value, dict):
        raise SafeIOError(f"{label} root must be a JSON object")
    return value


def load_json_object(
    path: Path,
    *,
    max_bytes: int,
    required: bool = True,
    label: str = "JSON document",
) -> dict[str, Any] | None:
    """Load one strict JSON object through the shared safe file boundary."""
    raw = read_regular_bytes(
        path,
        max_bytes=max_bytes,
        required=required,
        label=label,
    )
    if raw is None:
        return None
    return parse_json_object(raw, max_bytes=max_bytes, label=label)


def encode_json_value(
    value: Any,
    *,
    max_bytes: int,
    label: str = "JSON document",
) -> bytes:
    """Encode one deterministic strict UTF-8 JSON value within a byte budget."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (
        TypeError,
        ValueError,
        UnicodeEncodeError,
        RecursionError,
        OverflowError,
    ) as exc:
        raise JSONEncodingError(
            f"{label} cannot be encoded as strict UTF-8 JSON"
        ) from exc
    if len(encoded) > max_bytes:
        raise DocumentTooLargeError(
            f"{label} exceeds the {max_bytes}-byte safety limit"
        )
    return encoded


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    preserve_existing_mode: bool = False,
    create_parents: bool = True,
    label: str = "File",
) -> None:
    """Durably replace one regular file through an anchored directory handle.

    This is an atomic replacement primitive, not a compare-and-set operation.
    Callers remain responsible for their domain lock or pre-write CAS.
    """
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if (
        isinstance(mode, bool)
        or not isinstance(mode, int)
        or mode < 0
        or mode & ~0o777
    ):
        raise ValueError("mode must contain only ordinary rwx permission bits")

    candidate = path.expanduser()
    if not candidate.name or candidate.name in {".", ".."}:
        raise SafeIOError(f"{label} target has no safe file name: {candidate}")
    try:
        if create_parents:
            candidate.parent.mkdir(parents=True, exist_ok=True)
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise SafeIOError(
            f"{label} parent directory cannot be prepared: {candidate.parent}"
        ) from exc

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(parent, directory_flags)
    except OSError as exc:
        raise SafeIOError(
            f"{label} parent is not a safe directory: {parent}"
        ) from exc

    descriptor = -1
    temporary_name = f".{candidate.name}.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        try:
            existing = os.stat(
                candidate.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise SafeIOError(
                f"{label} target must be a regular non-symlink file: {candidate}"
            )
        target_mode = (
            stat.S_IMODE(existing.st_mode)
            if preserve_existing_mode and existing is not None
            else mode
        )

        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(
            temporary_name,
            flags,
            target_mode,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, target_mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary_name,
            candidate.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except SafeIOError:
        raise
    except OSError as exc:
        raise SafeIOError(
            f"{label} cannot be written atomically: {candidate}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory_fd)


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    max_bytes: int,
    mode: int,
    preserve_existing_mode: bool = False,
    create_parents: bool = True,
    label: str = "JSON document",
) -> None:
    """Encode and durably replace one bounded deterministic JSON document."""
    encoded = encode_json_value(value, max_bytes=max_bytes, label=label)
    atomic_write_bytes(
        path,
        encoded,
        mode=mode,
        preserve_existing_mode=preserve_existing_mode,
        create_parents=create_parents,
        label=label,
    )
