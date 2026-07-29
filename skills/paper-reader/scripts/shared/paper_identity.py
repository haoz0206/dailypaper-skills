#!/usr/bin/env python3
"""Canonical paper identities and deterministic Vault note matching.

This module is intentionally dependency-free so every public Skill can carry
the same identity rules. Exact stable identities always win. Legacy filename,
method-name, and title matching is allowed only when it selects one note.
"""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse

from safe_io import (
    SafeIOError,
    anchored_file_path,
    atomic_write_json,
    parse_json_value,
    read_regular_bytes,
    read_regular_prefix,
    sha256_regular_file,
)


MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_FRONTMATTER_BYTES = 256 * 1024
MAX_NOTE_INDEX_DIRECTORIES = 100_000
MAX_NOTE_INDEX_ENTRIES = 1_000_000
MAX_NOTE_INDEX_FILES = 500_000
MODERN_ARXIV = re.compile(r"^(\d{4}\.\d{4,5})(?:v\d+)?$", re.IGNORECASE)
LEGACY_ARXIV = re.compile(
    r"^([A-Za-z-]+(?:\.[A-Za-z-]+)?/\d{7})(?:v\d+)?$",
    re.IGNORECASE,
)
ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})
DOI = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ZOTERO_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
FRONTMATTER_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$")


class PaperIdentityError(RuntimeError):
    """Paper identity input or note metadata is malformed."""


@dataclass(frozen=True)
class NoteRecord:
    """One paper note projected into the deterministic matching index."""

    path: Path
    vault_relative: str
    wikilink: str
    stem: str
    title: str
    method_name: str
    paper_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.vault_relative,
            "wikilink": self.wikilink,
            "stem": self.stem,
            "title": self.title,
            "method_name": self.method_name,
            "paper_id": self.paper_id,
        }


@dataclass(frozen=True)
class NoteIndex:
    """Multi-map indexes that preserve collisions instead of overwriting them."""

    records: tuple[NoteRecord, ...]
    by_paper_id: Mapping[str, tuple[NoteRecord, ...]]
    by_method: Mapping[str, tuple[NoteRecord, ...]]
    by_title: Mapping[str, tuple[NoteRecord, ...]]


def canonical_arxiv_id(value: Any) -> str | None:
    """Return a version-free arXiv identifier from an ID or official URL."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.casefold().startswith("arxiv:"):
        text = text.split(":", 1)[1].strip()
    elif "://" in text:
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in ARXIV_HOSTS:
            return None
        path = unquote(parsed.path).strip("/")
        prefix, separator, identifier = path.partition("/")
        if not separator or prefix not in {"abs", "pdf", "html"}:
            return None
        text = identifier[:-4] if identifier.casefold().endswith(".pdf") else identifier
    modern = MODERN_ARXIV.fullmatch(text)
    if modern:
        return modern.group(1)
    legacy = LEGACY_ARXIV.fullmatch(text)
    if legacy:
        return legacy.group(1).lower()
    return None


def canonical_doi(value: Any) -> str | None:
    """Return a lowercase DOI without resolver prefixes or trailing punctuation."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    text = re.sub(r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", "", text, flags=re.I)
    match = DOI.search(text)
    if not match:
        return None
    return match.group(1).rstrip(".,;:)]}").lower()


def canonical_paper_id(value: Any) -> str | None:
    """Validate and normalize one namespaced stable paper identity."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    prefix, separator, payload = text.partition(":")
    if not separator:
        arxiv_id = canonical_arxiv_id(text)
        return f"arxiv:{arxiv_id}" if arxiv_id else None
    kind = prefix.casefold()
    payload = payload.strip()
    if kind == "arxiv":
        arxiv_id = canonical_arxiv_id(payload)
        return f"arxiv:{arxiv_id}" if arxiv_id else None
    if kind == "doi":
        doi = canonical_doi(payload)
        return f"doi:{doi}" if doi else None
    if kind == "sha256" and SHA256.fullmatch(payload.casefold()):
        return f"sha256:{payload.casefold()}"
    if kind == "zotero" and ZOTERO_KEY.fullmatch(payload):
        return f"zotero:{payload.casefold()}"
    return None


def paper_identity(metadata: Mapping[str, Any]) -> str | None:
    """Resolve the best stable identity from a paper record or note metadata."""
    explicit = canonical_paper_id(metadata.get("paper_id"))
    if explicit:
        return explicit
    for key in ("arxiv_id", "arxiv", "url", "pdf", "source_url", "arxiv_html"):
        arxiv_id = canonical_arxiv_id(metadata.get(key))
        if arxiv_id:
            return f"arxiv:{arxiv_id}"
    for key in ("doi", "doi_url"):
        doi = canonical_doi(metadata.get(key))
        if doi:
            return f"doi:{doi}"
    digest = metadata.get("local_pdf_sha256") or metadata.get("sha256")
    if isinstance(digest, str) and SHA256.fullmatch(digest.casefold()):
        return f"sha256:{digest.casefold()}"
    zotero_key = metadata.get("zotero_key")
    if isinstance(zotero_key, str) and ZOTERO_KEY.fullmatch(zotero_key.strip()):
        library = metadata.get("zotero_library_id")
        payload = (
            f"{library}:{zotero_key.strip()}"
            if isinstance(library, (str, int)) and str(library).strip()
            else zotero_key.strip()
        )
        return canonical_paper_id(f"zotero:{payload}")
    return None


def identity_metadata(value: str | Path) -> dict[str, str]:
    """Build frontmatter-ready identity fields from an arXiv/DOI/PDF input."""
    path = Path(value).expanduser() if isinstance(value, (str, Path)) else None
    if path is not None:
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            metadata = None
        except OSError as exc:
            raise PaperIdentityError(f"Cannot inspect local paper: {path}") from exc
        if metadata is not None:
            if stat.S_ISLNK(metadata.st_mode):
                raise PaperIdentityError(f"Local paper must not be a symlink: {path}")
            if stat.S_ISREG(metadata.st_mode):
                digest = _sha256_file(path)
                return {
                    "paper_id": f"sha256:{digest}",
                    "local_pdf_sha256": digest,
                    "source_url": "",
                }
    text = str(value)
    arxiv_id = canonical_arxiv_id(text)
    if arxiv_id:
        return {
            "paper_id": f"arxiv:{arxiv_id}",
            "arxiv_id": arxiv_id,
            "source_url": f"https://arxiv.org/abs/{arxiv_id}",
        }
    doi = canonical_doi(text)
    if doi:
        return {
            "paper_id": f"doi:{doi}",
            "doi": doi,
            "source_url": f"https://doi.org/{doi}",
        }
    raise PaperIdentityError(
        "Input is not an existing local file, a valid arXiv identifier, or a DOI"
    )


def parse_frontmatter(path: Path) -> dict[str, str]:
    """Read simple scalar identity metadata from bounded YAML frontmatter."""
    try:
        raw = read_regular_prefix(
            path,
            max_bytes=MAX_FRONTMATTER_BYTES,
            label="Paper note",
        )
    except SafeIOError as exc:
        raise PaperIdentityError(f"Cannot read note frontmatter: {path}") from exc
    assert raw is not None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PaperIdentityError(f"Note is not valid UTF-8: {path}") from exc
    return parse_frontmatter_text(text)


def parse_frontmatter_text(text: str) -> dict[str, str]:
    """Parse simple scalar identity metadata from an immutable text snapshot."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return result
        match = FRONTMATTER_KEY.match(line)
        if not match:
            continue
        key, raw_value = match.groups()
        result[key] = _scalar_value(raw_value)
    return {}


def _note_paths(
    notes_root: Path,
    *,
    concepts_root: Path | None,
) -> tuple[Path, ...]:
    """Return one bounded, non-recursive-by-symlink Markdown tree snapshot."""
    if not notes_root.exists():
        return ()
    try:
        root_metadata = notes_root.stat()
    except OSError as exc:
        raise PaperIdentityError(f"Cannot inspect paper notes root: {notes_root}") from exc
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise PaperIdentityError(f"Paper notes root is not a directory: {notes_root}")

    queue = deque([notes_root])
    visited = {(root_metadata.st_dev, root_metadata.st_ino)}
    directory_count = 1
    entry_count = 0
    note_paths: list[Path] = []
    while queue:
        directory = queue.popleft()
        remaining_entries = MAX_NOTE_INDEX_ENTRIES - entry_count
        try:
            entries = list(
                islice(directory.iterdir(), remaining_entries + 1)
            )
        except OSError as exc:
            raise PaperIdentityError(
                f"Cannot scan paper notes directory: {directory}"
            ) from exc
        if len(entries) > remaining_entries:
            raise PaperIdentityError(
                "Paper note index exceeds the "
                f"{MAX_NOTE_INDEX_ENTRIES}-entry safety limit"
            )
        entry_count += len(entries)
        for path in entries:
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise PaperIdentityError(
                    f"Paper note tree changed while scanning: {path}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                if path.suffix == ".md":
                    note_paths.append(path)
                    if len(note_paths) > MAX_NOTE_INDEX_FILES:
                        raise PaperIdentityError(
                            "Paper note index exceeds the "
                            f"{MAX_NOTE_INDEX_FILES}-file safety limit"
                        )
                continue
            if stat.S_ISDIR(metadata.st_mode):
                resolved = path.resolve()
                if concepts_root and (
                    resolved == concepts_root or concepts_root in resolved.parents
                ):
                    continue
                identity = (metadata.st_dev, metadata.st_ino)
                if identity in visited:
                    continue
                visited.add(identity)
                directory_count += 1
                if directory_count > MAX_NOTE_INDEX_DIRECTORIES:
                    raise PaperIdentityError(
                        "Paper note index exceeds the "
                        f"{MAX_NOTE_INDEX_DIRECTORIES}-directory safety limit"
                    )
                queue.append(path)
                continue
            if stat.S_ISREG(metadata.st_mode) and path.suffix == ".md":
                note_paths.append(path)
                if len(note_paths) > MAX_NOTE_INDEX_FILES:
                    raise PaperIdentityError(
                        "Paper note index exceeds the "
                        f"{MAX_NOTE_INDEX_FILES}-file safety limit"
                    )
    return tuple(sorted(note_paths))


def build_note_index(
    notes_dir: Path,
    *,
    concepts_dir: Path | None = None,
    vault: Path | None = None,
) -> NoteIndex:
    """Index all paper notes while preserving every duplicate key."""
    notes_root = notes_dir.expanduser().resolve()
    concepts_root = concepts_dir.expanduser().resolve() if concepts_dir else None
    vault_root = vault.expanduser().resolve() if vault else notes_root.parent
    try:
        notes_root.relative_to(vault_root)
    except ValueError as exc:
        raise PaperIdentityError(
            f"Paper notes root is outside the configured Vault: {notes_root}"
        ) from exc
    records: list[NoteRecord] = []
    for note_path in _note_paths(notes_root, concepts_root=concepts_root):
        resolved = anchored_file_path(note_path, label="Paper note")
        metadata = parse_frontmatter(resolved)
        try:
            relative = resolved.relative_to(vault_root).as_posix()
        except ValueError as exc:
            raise PaperIdentityError(
                f"Paper note is outside the configured Vault: {resolved}"
            ) from exc
        records.append(
            NoteRecord(
                path=resolved,
                vault_relative=relative,
                wikilink="",  # assigned after stem collision counts are known
                stem=resolved.stem,
                title=metadata.get("title", ""),
                method_name=metadata.get("method_name", "") or resolved.stem,
                paper_id=paper_identity(metadata),
            )
        )

    stem_counts: dict[str, int] = {}
    for record in records:
        key = normalize_match_key(record.stem)
        stem_counts[key] = stem_counts.get(key, 0) + 1
    resolved_records = tuple(
        NoteRecord(
            **{
                **record.__dict__,
                "wikilink": (
                    record.stem
                    if stem_counts[normalize_match_key(record.stem)] == 1
                    else str(Path(record.vault_relative).with_suffix("")).replace(
                        os.sep, "/"
                    )
                ),
            }
        )
        for record in records
    )
    return NoteIndex(
        records=resolved_records,
        by_paper_id=_multi_index(
            (record.paper_id, record)
            for record in resolved_records
            if record.paper_id
        ),
        by_method=_multi_index(
            (key, record)
            for record in resolved_records
            for key in {
                normalize_match_key(record.stem),
                normalize_match_key(record.method_name),
            }
            if key
        ),
        by_title=_multi_index(
            (normalize_match_key(record.title), record)
            for record in resolved_records
            if normalize_match_key(record.title)
        ),
    )


def match_paper_to_note(
    paper: Mapping[str, Any],
    index: NoteIndex,
) -> dict[str, Any]:
    """Return an exact, unique fallback, ambiguous, or missing match."""
    resolved_id = paper_identity(paper)
    base = {
        "paper_id": resolved_id,
        "arxiv_id": canonical_arxiv_id(resolved_id),
        "title": str(paper.get("title", "")),
    }
    if resolved_id:
        exact = index.by_paper_id.get(resolved_id, ())
        if len(exact) == 1:
            return {**base, "status": "exact", "basis": "paper_id", "note": exact[0].as_dict()}
        if len(exact) > 1:
            return _ambiguous(base, "paper_id", exact)

    method_keys = {
        normalize_match_key(value)
        for value in _paper_method_names(paper)
        if normalize_match_key(value)
    }
    method_candidates = _unique_records(
        record
        for key in method_keys
        for record in index.by_method.get(key, ())
    )
    conflicting_methods = tuple(
        record for record in method_candidates if record.paper_id is not None
    )
    legacy_methods = tuple(
        record for record in method_candidates if record.paper_id is None
    )
    if conflicting_methods:
        return _ambiguous(base, "conflicting-paper-id", method_candidates)
    if len(legacy_methods) == 1:
        return {
            **base,
            "status": "fallback",
            "basis": "unique-method-name",
            "note": legacy_methods[0].as_dict(),
        }
    if len(legacy_methods) > 1:
        return _ambiguous(base, "method_name", legacy_methods)

    title_key = normalize_match_key(paper.get("title"))
    title_candidates = index.by_title.get(title_key, ()) if title_key else ()
    conflicting_titles = tuple(
        record for record in title_candidates if record.paper_id is not None
    )
    legacy_titles = tuple(
        record for record in title_candidates if record.paper_id is None
    )
    if conflicting_titles:
        return _ambiguous(base, "conflicting-paper-id", title_candidates)
    if len(legacy_titles) == 1:
        return {
            **base,
            "status": "fallback",
            "basis": "unique-title",
            "note": legacy_titles[0].as_dict(),
        }
    if len(legacy_titles) > 1:
        return _ambiguous(base, "title", legacy_titles)
    return {**base, "status": "missing", "basis": None, "note": None}


def match_papers(
    papers: Sequence[Mapping[str, Any]],
    index: NoteIndex,
) -> dict[str, Any]:
    """Return a compact deterministic match report for one candidate list."""
    matches = [match_paper_to_note(paper, index) for paper in papers]
    return {
        "version": 1,
        "counts": {
            "papers": len(matches),
            "notes": len(index.records),
            "exact": sum(item["status"] == "exact" for item in matches),
            "fallback": sum(item["status"] == "fallback" for item in matches),
            "ambiguous": sum(item["status"] == "ambiguous" for item in matches),
            "missing": sum(item["status"] == "missing" for item in matches),
        },
        "matches": matches,
    }


def normalize_match_key(value: Any) -> str:
    """Normalize legacy human labels for conservative equality matching."""
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


def _paper_method_names(paper: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    raw_methods = paper.get("method_names", ())
    if isinstance(raw_methods, list):
        values.extend(value for value in raw_methods if isinstance(value, str))
    method_name = paper.get("method_name")
    if isinstance(method_name, str):
        values.append(method_name)
    title = paper.get("title")
    if isinstance(title, str) and title.strip():
        prefix = re.sub(r"^\d+\.\s*", "", title.split(":", 1)[0]).strip()
        if prefix:
            values.append(prefix)
    return tuple(dict.fromkeys(values))


def _ambiguous(
    base: Mapping[str, Any],
    basis: str,
    records: Iterable[NoteRecord],
) -> dict[str, Any]:
    return {
        **base,
        "status": "ambiguous",
        "basis": basis,
        "note": None,
        "candidates": [record.as_dict() for record in _unique_records(records)],
    }


def _unique_records(records: Iterable[NoteRecord]) -> tuple[NoteRecord, ...]:
    by_path = {record.path: record for record in records}
    return tuple(by_path[path] for path in sorted(by_path))


def _multi_index(
    entries: Iterable[tuple[str | None, NoteRecord]],
) -> dict[str, tuple[NoteRecord, ...]]:
    values: dict[str, list[NoteRecord]] = {}
    for key, record in entries:
        if key:
            values.setdefault(key, []).append(record)
    return {
        key: _unique_records(records)
        for key, records in sorted(values.items())
    }


def _scalar_value(raw: str) -> str:
    value = raw.strip().split(" #", 1)[0].rstrip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return decoded if isinstance(decoded, str) else str(decoded)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def _sha256_file(path: Path) -> str:
    try:
        return sha256_regular_file(path, label="Local paper")
    except SafeIOError as exc:
        raise PaperIdentityError(str(exc)) from exc


def _load_papers(path: Path) -> list[Mapping[str, Any]]:
    try:
        raw = read_regular_bytes(
            path,
            max_bytes=MAX_JSON_BYTES,
            label="Paper JSON",
        )
        if raw is None:
            raise SafeIOError(f"Paper JSON file does not exist: {path}")
        data = parse_json_value(
            raw,
            max_bytes=MAX_JSON_BYTES,
            label="Paper JSON",
        )
    except SafeIOError as exc:
        raise PaperIdentityError(str(exc)) from exc
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise PaperIdentityError("Paper JSON must be an array of objects")
    return data


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    try:
        atomic_write_json(
            path,
            data,
            max_bytes=MAX_JSON_BYTES,
            mode=0o600,
            label="Paper identity report",
        )
    except SafeIOError as exc:
        raise PaperIdentityError(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve stable paper identities and match Vault notes."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    identify = subparsers.add_parser("identify")
    identify.add_argument("input")

    match = subparsers.add_parser("match")
    match.add_argument("--papers", type=Path, required=True)
    match.add_argument("--notes-dir", type=Path, required=True)
    match.add_argument("--concepts-dir", type=Path)
    match.add_argument("--vault", type=Path, required=True)
    match.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        if args.command == "identify":
            result: Mapping[str, Any] = {
                "version": 1,
                **identity_metadata(args.input),
            }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        index = build_note_index(
            args.notes_dir,
            concepts_dir=args.concepts_dir,
            vault=args.vault,
        )
        result = match_papers(_load_papers(args.papers), index)
        _atomic_json(args.output, result)
        print(json.dumps(result["counts"], sort_keys=True))
        return 0
    except PaperIdentityError as exc:
        print(str(exc), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
