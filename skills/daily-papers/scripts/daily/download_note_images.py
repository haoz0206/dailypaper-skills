#!/usr/bin/env python3
"""Safely validate external note images and localize only verified fallbacks.

Remote paper content is untrusted.  This module therefore validates every URL
and DNS result, follows redirects one hop at a time, pins the connection to a
validated public address, bounds time and bytes, and keeps all downloads and
PDF extraction outside the Vault until a verified artifact is published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit


_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from paper_identity import canonical_arxiv_id
import safe_http
from safe_io import (
    DocumentTooLargeError,
    RegularFileSnapshot,
    SafeIOError,
    anchored_file_path,
    copy_regular_file,
    inspect_regular_file,
    read_regular_bytes,
)
from safe_process import ProcessResult, SafeProcessError, run_bounded_tool


RESULT_VERSION = 2
MAX_REDIRECTS = 5
MAX_EXTRACTED_FILES = 512
MAX_PDFIMAGES_LIST_BYTES = 1024 * 1024
DEFAULT_IMAGE_BYTES = 16 * 1024 * 1024
DEFAULT_PDF_BYTES = 64 * 1024 * 1024
DEFAULT_TOTAL_BYTES = 96 * 1024 * 1024
DEFAULT_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_NOTE_BYTES = 16 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_RUN_TIMEOUT_SECONDS = 180.0
PDFIMAGES_TIMEOUT_SECONDS = 45.0
MAX_TOOL_LOG_BYTES = 64 * 1024
SUPPORTED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})
REMOTE_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
MODERN_ARXIV_IN_PATH = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)")


class ImageLocalizationError(RuntimeError):
    """A safe, expected image-localization failure."""

    code = "image-localization-error"


class UnsafeURLError(ImageLocalizationError):
    """A URL may access credentials, a local service, or a non-public network."""

    code = "unsafe-url"


class DownloadError(ImageLocalizationError):
    """A bounded remote fetch failed."""

    code = "download-failed"


class ResponseTooLargeError(DownloadError):
    """A response exceeded its per-file or run-wide byte budget."""

    code = "response-too-large"


class UnsupportedMediaError(DownloadError):
    """The declared and detected media types are not an allowed image/PDF."""

    code = "unsupported-media"


class AssetCollisionError(ImageLocalizationError):
    """A content-addressed target exists with different bytes."""

    code = "asset-collision"


class ConcurrentNoteChangeError(ImageLocalizationError):
    """The note changed after it was read and must not be overwritten."""

    code = "concurrent-note-change"


class CLIUsageError(ImageLocalizationError):
    """Command-line arguments are invalid."""

    code = "invalid-arguments"


@dataclass(frozen=True)
class FetchLimits:
    """Network and resource limits for one localization invocation."""

    max_image_bytes: int = DEFAULT_IMAGE_BYTES
    max_pdf_bytes: int = DEFAULT_PDF_BYTES
    max_total_bytes: int = DEFAULT_TOTAL_BYTES
    max_extracted_bytes: int = DEFAULT_EXTRACTED_BYTES
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS
    max_redirects: int = MAX_REDIRECTS

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_image_bytes,
            self.max_pdf_bytes,
            self.max_total_bytes,
            self.max_extracted_bytes,
            self.max_redirects,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_limits):
            raise ValueError("Fetch byte and redirect limits must be integers")
        if min(
            self.max_image_bytes,
            self.max_pdf_bytes,
            self.max_total_bytes,
            self.max_extracted_bytes,
        ) <= 0:
            raise ValueError("Fetch byte limits must be positive")
        if self.max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        if self.request_timeout_seconds <= 0 or self.run_timeout_seconds <= 0:
            raise ValueError("Fetch timeouts must be positive")


ValidatedURL = safe_http.ValidatedURL
PinnedHTTPTransport = safe_http.PinnedHTTPTransport


@dataclass(frozen=True)
class FetchedFile:
    """One verified bounded response stored in an external temporary directory."""

    path: Path
    final_url: str
    media_type: str
    extension: str
    bytes: int
    sha256: str


def validate_remote_url(
    value: str,
    *,
    resolver: Callable[..., Iterable[tuple[Any, ...]]] = socket.getaddrinfo,
) -> ValidatedURL:
    """Compatibility wrapper around the suite-wide remote URL policy."""
    try:
        return safe_http.validate_remote_url(value, resolver=resolver)
    except safe_http.UnsafeURLError as exc:
        raise UnsafeURLError(str(exc)) from exc


def _image_format(
    header: bytes,
    *,
    declared_media_type: str | None,
) -> tuple[str, str]:
    """Validate image magic and return its normalized media type and suffix."""
    detected: str | None = None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif header.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif header.startswith((b"GIF87a", b"GIF89a")):
        detected = "image/gif"
    elif (
        len(header) >= 12
        and header[:4] == b"RIFF"
        and header[8:12] == b"WEBP"
    ):
        detected = "image/webp"
    if detected is None:
        raise UnsupportedMediaError("Downloaded file is not a supported image")
    normalized_declared = (
        "image/jpeg"
        if declared_media_type == "image/jpg"
        else declared_media_type
    )
    if normalized_declared is not None and normalized_declared != detected:
        raise UnsupportedMediaError(
            f"Image Content-Type {normalized_declared!r} does not match "
            f"{detected!r}"
        )
    return detected, SUPPORTED_IMAGE_TYPES[detected]


def _validated_image_snapshot(
    snapshot: RegularFileSnapshot,
    *,
    declared_media_type: str | None,
) -> tuple[str, str, int, str]:
    if snapshot.size <= 0:
        raise UnsupportedMediaError("Downloaded image is empty")
    detected, extension = _image_format(
        snapshot.prefix,
        declared_media_type=declared_media_type,
    )
    return detected, extension, snapshot.size, snapshot.sha256


def inspect_image_file(
    path: Path,
    *,
    declared_media_type: str | None = None,
    max_bytes: int = DEFAULT_IMAGE_BYTES,
) -> tuple[str, str, int, str]:
    """Inspect one bounded image once and return its verified snapshot."""
    try:
        snapshot = inspect_regular_file(
            path,
            max_bytes=max_bytes,
            prefix_bytes=32,
            label="Downloaded image",
        )
    except DocumentTooLargeError as exc:
        raise ResponseTooLargeError(str(exc)) from exc
    except SafeIOError as exc:
        raise UnsupportedMediaError(f"Cannot inspect downloaded image: {path}") from exc
    return _validated_image_snapshot(
        snapshot,
        declared_media_type=declared_media_type,
    )


def _validate_pdf_header(header: bytes, *, declared_media_type: str) -> None:
    if not header.startswith(b"%PDF-"):
        raise UnsupportedMediaError("Downloaded file is not a PDF")
    if declared_media_type != "application/pdf":
        raise UnsupportedMediaError(
            "PDF Content-Type must be 'application/pdf', got "
            f"{declared_media_type!r}"
        )


class SafeFetcher:
    """Bounded, redirect-aware fetcher with pinned validated destinations."""

    def __init__(
        self,
        *,
        resolver: Callable[..., Iterable[tuple[Any, ...]]] = socket.getaddrinfo,
        transport: Any | None = None,
        limits: FetchLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits or FetchLimits()
        self.clock = clock
        self.client = safe_http.SafeHTTPClient(
            resolver=resolver,
            transport=transport,
            max_redirects=self.limits.max_redirects,
        )

    def new_budget(self) -> safe_http.FetchBudget:
        return self.client.new_budget(
            max_total_bytes=self.limits.max_total_bytes,
            request_timeout_seconds=self.limits.request_timeout_seconds,
            run_timeout_seconds=self.limits.run_timeout_seconds,
            clock=self.clock,
        )

    def fetch_to_path(
        self,
        url: str,
        destination: Path,
        *,
        kind: str,
        budget: safe_http.FetchBudget,
    ) -> FetchedFile:
        """Fetch one verified image or PDF into a new external temporary file."""
        if kind not in {"image", "pdf"}:
            raise ValueError("kind must be 'image' or 'pdf'")
        max_bytes = (
            self.limits.max_image_bytes
            if kind == "image"
            else self.limits.max_pdf_bytes
        )
        allowed_media_types = (
            set(SUPPORTED_IMAGE_TYPES)
            if kind == "image"
            else {"application/pdf"}
        )
        accept = (
            "image/png,image/jpeg,image/gif,image/webp"
            if kind == "image"
            else "application/pdf"
        )
        try:
            downloaded = self.client.fetch_file(
                url,
                destination,
                max_bytes=max_bytes,
                budget=budget,
                accept=accept,
                allowed_media_types=allowed_media_types,
            )
            if kind == "image":
                media_type, extension = _image_format(
                    downloaded.read_verified_prefix(32),
                    declared_media_type=downloaded.media_type,
                )
                size, digest = downloaded.bytes, downloaded.sha256
            else:
                if downloaded.media_type is None:
                    raise DownloadError(
                        "PDF response is missing a validated media type"
                    )
                _validate_pdf_header(
                    downloaded.read_verified_prefix(8),
                    declared_media_type=downloaded.media_type,
                )
                size, digest = downloaded.bytes, downloaded.sha256
                media_type, extension = "application/pdf", ".pdf"
        except safe_http.UnsafeURLError as exc:
            destination.unlink(missing_ok=True)
            raise UnsafeURLError(str(exc)) from exc
        except safe_http.ResponseTooLargeError as exc:
            destination.unlink(missing_ok=True)
            raise ResponseTooLargeError(str(exc)) from exc
        except safe_http.SafeHTTPError as exc:
            destination.unlink(missing_ok=True)
            raise DownloadError(str(exc)) from exc
        except ImageLocalizationError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise DownloadError(f"Remote {kind} request failed: {exc}") from exc
        return FetchedFile(
            path=downloaded.path,
            final_url=downloaded.final_url,
            media_type=media_type,
            extension=extension,
            bytes=size,
            sha256=digest,
        )


@dataclass(frozen=True)
class PDFImagePlan:
    image_count: int
    estimated_decoded_bytes: int


def parse_pdfimages_list(payload: bytes) -> PDFImagePlan:
    """Strictly parse bounded ``pdfimages -list`` output."""
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise DownloadError("pdfimages list output is not valid UTF-8") from exc
    header_index: int | None = None
    required_header = (
        "page",
        "num",
        "type",
        "width",
        "height",
        "color",
        "comp",
        "bpc",
    )
    for index, line in enumerate(lines):
        if tuple(line.split()[:8]) == required_header:
            header_index = index
            break
    if header_index is None:
        raise DownloadError("pdfimages list output is missing its table header")

    count = 0
    estimated = 0
    allowed_types = {"image", "mask", "smask", "stencil"}
    for line in lines[header_index + 1 :]:
        stripped = line.strip()
        if not stripped or set(stripped) == {"-"}:
            continue
        fields = stripped.split()
        if len(fields) < 16 or fields[2] not in allowed_types:
            raise DownloadError("pdfimages list contains a malformed image row")
        try:
            page = int(fields[0], 10)
            number = int(fields[1], 10)
            width = int(fields[3], 10)
            height = int(fields[4], 10)
            components = int(fields[6], 10)
            bits_per_component = int(fields[7], 10)
        except ValueError as exc:
            raise DownloadError(
                "pdfimages list contains non-integer image dimensions"
            ) from exc
        if (
            page <= 0
            or number < 0
            or width <= 0
            or height <= 0
            or components <= 0
            or components > 32
            or bits_per_component <= 0
            or bits_per_component > 64
        ):
            raise DownloadError(
                "pdfimages list contains unsafe image dimensions"
            )
        count += 1
        estimated += (
            width * height * components * bits_per_component + 7
        ) // 8
    return PDFImagePlan(
        image_count=count,
        estimated_decoded_bytes=estimated,
    )


def _tool_failure_detail(result: ProcessResult) -> str:
    return (
        result.stderr.decode("utf-8", errors="replace").strip()
        or result.stdout.decode("utf-8", errors="replace").strip()
        or f"exit status {result.returncode}"
    )


class PDFExtractor:
    """Download an official arXiv PDF and isolate all pdfimages output."""

    def __init__(
        self,
        fetcher: SafeFetcher,
        *,
        runner: Callable[..., ProcessResult] = run_bounded_tool,
    ) -> None:
        self.fetcher = fetcher
        self.runner = runner

    def extract(
        self,
        arxiv_id: str,
        figure_number: int,
        work_dir: Path,
        budget: safe_http.FetchBudget,
    ) -> Path:
        canonical = canonical_arxiv_id(f"arxiv:{arxiv_id}")
        if canonical is None:
            raise DownloadError(f"Invalid arXiv identifier for PDF fallback: {arxiv_id}")
        pdf_path = work_dir / f"arxiv-{canonical.replace('/', '_')}.pdf"
        if not pdf_path.exists():
            self.fetcher.fetch_to_path(
                f"https://arxiv.org/pdf/{canonical}.pdf",
                pdf_path,
                kind="pdf",
                budget=budget,
            )
        try:
            listing = self.runner(
                ["pdfimages", "-list", str(pdf_path)],
                timeout=PDFIMAGES_TIMEOUT_SECONDS,
                max_stdout_bytes=MAX_PDFIMAGES_LIST_BYTES,
                max_stderr_bytes=MAX_TOOL_LOG_BYTES,
                max_file_bytes=None,
            )
        except SafeProcessError as exc:
            raise DownloadError(f"PDF image planning failed: {exc}") from exc
        if listing.returncode != 0:
            raise DownloadError(
                f"pdfimages list failed: {_tool_failure_detail(listing)}"
            )
        plan = parse_pdfimages_list(listing.stdout)
        if plan.image_count <= 0:
            raise DownloadError("PDF contains no extractable images")
        if plan.image_count > MAX_EXTRACTED_FILES:
            raise ResponseTooLargeError(
                "PDF declares too many extractable images"
            )
        if (
            plan.estimated_decoded_bytes
            > self.fetcher.limits.max_extracted_bytes
        ):
            raise ResponseTooLargeError(
                "PDF image plan exceeds the decoded-byte limit"
            )

        extraction_dir = work_dir / (
            f"pdf-extract-{canonical.replace('/', '_')}-{figure_number}"
        )
        extraction_dir.mkdir()
        prefix = extraction_dir / "figure"
        try:
            result = self.runner(
                ["pdfimages", "-png", str(pdf_path), str(prefix)],
                timeout=PDFIMAGES_TIMEOUT_SECONDS,
                max_stdout_bytes=MAX_TOOL_LOG_BYTES,
                max_stderr_bytes=MAX_TOOL_LOG_BYTES,
                max_file_bytes=self.fetcher.limits.max_image_bytes,
            )
        except SafeProcessError as exc:
            raise DownloadError(f"PDF image extraction failed: {exc}") from exc
        if result.returncode != 0:
            raise DownloadError(
                f"pdfimages failed: {_tool_failure_detail(result)}"
            )
        extracted = list(
            islice(extraction_dir.iterdir(), MAX_EXTRACTED_FILES + 1)
        )
        if len(extracted) > MAX_EXTRACTED_FILES:
            raise DownloadError("PDF extraction produced too many image files")
        extracted.sort()
        valid: list[Path] = []
        actual_total = 0
        for candidate in extracted:
            if (
                not candidate.name.startswith(f"{prefix.name}-")
                or candidate.suffix.casefold() != ".png"
            ):
                raise DownloadError(
                    "PDF extraction produced an unexpected artifact"
                )
            try:
                snapshot = inspect_regular_file(
                    candidate,
                    max_bytes=self.fetcher.limits.max_image_bytes,
                    prefix_bytes=32,
                    label="Extracted PDF image",
                )
            except DocumentTooLargeError as exc:
                raise ResponseTooLargeError(str(exc)) from exc
            except SafeIOError as exc:
                raise DownloadError(
                    "PDF extraction produced a non-regular artifact"
                ) from exc
            actual_total += snapshot.size
            if actual_total > self.fetcher.limits.max_extracted_bytes:
                raise ResponseTooLargeError(
                    "Extracted PDF images exceed the total byte limit"
                )
            try:
                _validated_image_snapshot(
                    snapshot,
                    declared_media_type=None,
                )
            except UnsupportedMediaError:
                continue
            valid.append(candidate)
        index = figure_number - 1
        if index < 0 or index >= len(valid):
            raise DownloadError(
                f"PDF extraction did not produce Figure {figure_number}"
            )
        return valid[index]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_asset(
    source: Path,
    assets_dir: Path,
    *,
    max_bytes: int = DEFAULT_IMAGE_BYTES,
) -> dict[str, Any]:
    """Copy, verify, and publish one immutable content-addressed image."""
    source = anchored_file_path(source, label="Image source")
    assets_dir = assets_dir.expanduser()
    if assets_dir.exists():
        if assets_dir.is_symlink() or not assets_dir.is_dir():
            raise AssetCollisionError(
                f"Assets path is not a safe regular directory: {assets_dir}"
            )
    else:
        assets_dir.mkdir(mode=0o755)
    assets_dir = assets_dir.resolve()

    def existing_result() -> dict[str, Any]:
        try:
            existing_media, _extension, existing_size, existing_digest = (
                inspect_image_file(target, max_bytes=max_bytes)
            )
        except ImageLocalizationError as exc:
            raise AssetCollisionError(
                f"Content-addressed asset target is not a regular file: {target}"
            ) from exc
        if existing_digest != digest:
            raise AssetCollisionError(
                f"Content-addressed asset collision preserves existing file: {target}"
            )
        return {
            "path": str(target),
            "sha256": digest,
            "media_type": existing_media,
            "bytes": existing_size,
            "created": False,
        }

    descriptor, staging_name = tempfile.mkstemp(
        prefix=".dailypaper-asset-",
        suffix=".tmp",
        dir=assets_dir,
    )
    staging = Path(staging_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            try:
                snapshot = copy_regular_file(
                    source,
                    output,
                    max_bytes=max_bytes,
                    prefix_bytes=32,
                    label="Published image source",
                )
            except DocumentTooLargeError as exc:
                raise ResponseTooLargeError(str(exc)) from exc
            except SafeIOError as exc:
                raise UnsupportedMediaError(
                    f"Cannot inspect downloaded image: {source}"
                ) from exc
            media_type, extension, size, digest = _validated_image_snapshot(
                snapshot,
                declared_media_type=None,
            )
            output.flush()
            os.fsync(output.fileno())
        os.chmod(staging, 0o644)
        target = assets_dir / f"{digest}{extension}"
        if target.exists() or target.is_symlink():
            return existing_result()
        try:
            os.link(staging, target, follow_symlinks=False)
        except FileExistsError:
            return existing_result()
        _fsync_directory(assets_dir)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        staging.unlink(missing_ok=True)
    return {
        "path": str(target),
        "sha256": digest,
        "media_type": media_type,
        "bytes": size,
        "created": True,
    }


def _atomic_write_note(path: Path, content: str, *, expected_sha256: str) -> None:
    """Durably replace a note only while its pre-edit bytes still match."""
    try:
        current = read_regular_bytes(
            path,
            max_bytes=MAX_NOTE_BYTES,
            label="Paper note",
        )
        if current is None:
            raise ConcurrentNoteChangeError(
                f"Paper note disappeared before update: {path}"
            )
    except SafeIOError as exc:
        raise ConcurrentNoteChangeError(f"Cannot re-read note before update: {path}") from exc
    if hashlib.sha256(current).hexdigest() != expected_sha256:
        raise ConcurrentNoteChangeError(
            "Paper note changed while images were processed; preserving both versions"
        )
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ConcurrentNoteChangeError(
            f"Cannot inspect note before update: {path}"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ConcurrentNoteChangeError(
            f"Paper note is no longer a regular file: {path}"
        )
    mode = stat.S_IMODE(metadata.st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Recheck immediately before the atomic replacement.
        try:
            latest = read_regular_bytes(
                path,
                max_bytes=MAX_NOTE_BYTES,
                label="Paper note",
            )
            if latest is None:
                raise ConcurrentNoteChangeError(
                    f"Paper note disappeared before replacement: {path}"
                )
            if hashlib.sha256(latest).hexdigest() != expected_sha256:
                raise ConcurrentNoteChangeError(
                    "Paper note changed while images were processed; "
                    "preserving both versions"
                )
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except ConcurrentNoteChangeError:
            raise
        except SafeIOError as exc:
            raise ConcurrentNoteChangeError(
                f"Cannot safely re-read note before update: {path}"
            ) from exc
        except OSError as exc:
            raise ConcurrentNoteChangeError(
                f"Paper note could not be atomically updated: {path}"
            ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def parse_note(text: str) -> list[dict[str, Any]]:
    """Extract external Markdown image references and their exact spans."""
    images: list[dict[str, Any]] = []
    for match in REMOTE_IMAGE.finditer(text):
        url = match.group(2)
        if not URL_SCHEME.match(url):
            continue
        images.append(
            {
                "full_match": match.group(0),
                "alt": match.group(1),
                "url": url,
                "start": match.start(),
                "end": match.end(),
            }
        )
    return images


def update_frontmatter(text: str) -> str:
    """Update image_source from online to mixed after a successful localization."""
    return re.sub(
        r"^(image_source:\s*)online\s*$",
        r"\1mixed",
        text,
        count=1,
        flags=re.MULTILINE,
    )


def _official_arxiv_id(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").rstrip(".").casefold()
    if parsed.scheme.casefold() not in {"http", "https"} or host not in ARXIV_HOSTS:
        return None
    match = MODERN_ARXIV_IN_PATH.search(parsed.path)
    return canonical_arxiv_id(f"arxiv:{match.group(1)}") if match else None


def _result_path(path: Path, vault_root: Path | None) -> str:
    resolved = path.resolve()
    if vault_root is None:
        return str(resolved)
    try:
        return resolved.relative_to(vault_root).as_posix()
    except ValueError as exc:
        raise ImageLocalizationError(
            f"Changed path escapes the configured Vault: {resolved}"
        ) from exc


def _replace_spans(
    text: str,
    images: list[dict[str, Any]],
    replacements: dict[int, str],
) -> str:
    updated = text
    for index in sorted(replacements, reverse=True):
        image = images[index]
        updated = (
            updated[: image["start"]]
            + replacements[index]
            + updated[image["end"] :]
        )
    return updated


def process_note(
    note_path: Path,
    *,
    fetcher: Any | None = None,
    pdf_extractor: Any | None = None,
    vault_root: Path | None = None,
) -> dict[str, Any]:
    """Process one note and return exact machine-readable artifacts and changes."""
    lexical_note = note_path.expanduser()
    if lexical_note.is_symlink():
        raise ImageLocalizationError(f"Paper note must not be a symlink: {lexical_note}")
    note = lexical_note.resolve()
    if not note.is_file():
        raise ImageLocalizationError(f"Paper note does not exist as a file: {note}")
    vault = vault_root.expanduser().resolve() if vault_root is not None else None
    if vault is not None:
        try:
            note.relative_to(vault)
        except ValueError as exc:
            raise ImageLocalizationError(
                f"Paper note is outside the configured Vault: {note}"
            ) from exc
    try:
        original_bytes = read_regular_bytes(
            note,
            max_bytes=MAX_NOTE_BYTES,
            label="Paper note",
        )
        if original_bytes is None:
            raise ImageLocalizationError(f"Paper note does not exist: {note}")
        original_text = original_bytes.decode("utf-8")
    except SafeIOError as exc:
        raise ImageLocalizationError(str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise ImageLocalizationError(f"Paper note is not valid UTF-8: {note}") from exc
    except OSError as exc:
        raise ImageLocalizationError(f"Paper note cannot be read: {note}") from exc
    original_sha256 = hashlib.sha256(original_bytes).hexdigest()
    images = parse_note(original_text)
    active_fetcher = fetcher or SafeFetcher()
    budget = active_fetcher.new_budget()
    active_extractor = pdf_extractor or PDFExtractor(active_fetcher)
    assets_dir = note.parent / "assets"

    reachable = 0
    localized = 0
    failures: list[dict[str, Any]] = []
    replacements: dict[int, str] = {}
    artifacts_by_path: dict[str, dict[str, Any]] = {}
    created_paths: list[Path] = []

    # /tmp is deliberately outside a configured Vault on supported Linux/macOS hosts.
    with tempfile.TemporaryDirectory(
        prefix="dailypaper-images-",
        dir="/tmp",
    ) as temp_dir:
        work_root = Path(temp_dir).resolve()
        if vault is not None and (work_root == vault or vault in work_root.parents):
            raise ImageLocalizationError(
                "Image temporary directory must remain outside the Vault"
            )
        fetch_cache: dict[str, FetchedFile | ImageLocalizationError] = {}
        pdf_cache: dict[tuple[str, int], Path | ImageLocalizationError] = {}

        for index, image in enumerate(images):
            url = str(image["url"])
            fetched = fetch_cache.get(url)
            if fetched is None:
                destination = work_root / f"remote-image-{len(fetch_cache)}"
                try:
                    fetched = active_fetcher.fetch_to_path(
                        url,
                        destination,
                        kind="image",
                        budget=budget,
                    )
                except ImageLocalizationError as exc:
                    fetched = exc
                fetch_cache[url] = fetched
            if isinstance(fetched, FetchedFile):
                reachable += 1
                continue

            arxiv_id = _official_arxiv_id(url)
            fallback: Path | ImageLocalizationError
            if arxiv_id is None:
                fallback = fetched
            else:
                cache_key = (arxiv_id, index + 1)
                fallback = pdf_cache.get(cache_key)  # type: ignore[assignment]
                if fallback is None:
                    try:
                        fallback = active_extractor.extract(
                            arxiv_id,
                            index + 1,
                            work_root,
                            budget,
                        )
                        resolved_fallback = fallback.resolve()
                        try:
                            resolved_fallback.relative_to(work_root)
                        except ValueError as exc:
                            raise DownloadError(
                                "PDF extractor returned a path outside its isolated work directory"
                            ) from exc
                        fallback = resolved_fallback
                    except ImageLocalizationError as exc:
                        fallback = exc
                    pdf_cache[cache_key] = fallback

            if isinstance(fallback, ImageLocalizationError):
                failures.append(
                    {
                        "index": index + 1,
                        "url": url,
                        "code": fallback.code,
                        "message": str(fallback),
                    }
                )
                continue
            try:
                limits = getattr(active_fetcher, "limits", None)
                publish_limit = getattr(
                    limits,
                    "max_image_bytes",
                    DEFAULT_IMAGE_BYTES,
                )
                artifact = publish_asset(
                    fallback,
                    assets_dir,
                    max_bytes=publish_limit,
                )
            except ImageLocalizationError as exc:
                failures.append(
                    {
                        "index": index + 1,
                        "url": url,
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
                continue
            artifact_path = Path(str(artifact["path"])).resolve()
            result_artifact = {
                **artifact,
                "path": _result_path(artifact_path, vault),
            }
            previous_artifact = artifacts_by_path.get(str(artifact_path))
            if previous_artifact is not None and previous_artifact["created"]:
                result_artifact["created"] = True
            artifacts_by_path[str(artifact_path)] = result_artifact
            if artifact["created"] and artifact_path not in created_paths:
                created_paths.append(artifact_path)
            relative_asset = artifact_path.relative_to(note.parent).as_posix()
            replacements[index] = f"![[{relative_asset}|600]]"
            localized += 1

    note_changed = bool(replacements)
    if note_changed:
        updated_text = update_frontmatter(
            _replace_spans(original_text, images, replacements)
        )
        try:
            _atomic_write_note(note, updated_text, expected_sha256=original_sha256)
        except ImageLocalizationError as exc:
            for index in sorted(replacements):
                failures.append(
                    {
                        "index": index + 1,
                        "url": str(images[index]["url"]),
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
            localized = 0
            note_changed = False

    changed_paths = [_result_path(path, vault) for path in created_paths]
    if note_changed:
        changed_paths.append(_result_path(note, vault))
    return {
        "version": RESULT_VERSION,
        "status": "success" if not failures else "partial",
        "note": _result_path(note, vault),
        "total": len(images),
        "reachable": reachable,
        "localized": localized,
        "failed": len(failures),
        "failures": failures,
        "artifacts": list(artifacts_by_path.values()),
        "changed_paths": changed_paths,
        "downloaded_bytes": getattr(budget, "consumed_bytes", 0),
    }


def _print_json(value: dict[str, Any], *, stream: Any) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        file=stream,
    )


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIUsageError(message)


def main(
    argv: list[str] | None = None,
    *,
    fetcher: Any | None = None,
    pdf_extractor: Any | None = None,
) -> int:
    parser = _JSONArgumentParser(
        description="Safely validate and localize unreachable paper-note images."
    )
    parser.add_argument("note", type=Path)
    parser.add_argument(
        "--vault",
        type=Path,
        help="Optional Vault root; changed paths are Vault-relative when supplied.",
    )
    try:
        args = parser.parse_args(argv)
        result = process_note(
            args.note,
            fetcher=fetcher,
            pdf_extractor=pdf_extractor,
            vault_root=args.vault,
        )
    except (ImageLocalizationError, OSError, ValueError) as exc:
        payload = {
            "version": RESULT_VERSION,
            "status": "blocked",
            "code": getattr(exc, "code", "invalid-input"),
            "message": str(exc),
            "artifacts": [],
            "changed_paths": [],
        }
        _print_json(payload, stream=sys.stderr)
        return 2
    if result["failed"]:
        _print_json(result, stream=sys.stderr)
        return 1
    _print_json(result, stream=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
