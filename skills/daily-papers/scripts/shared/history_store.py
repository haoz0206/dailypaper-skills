#!/usr/bin/env python3
"""Strict, bounded, atomic storage for DailyPaper recommendation history."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from safe_io import (
    DocumentTooLargeError,
    SafeIOError,
    anchored_file_path,
    atomic_write_json,
    parse_json_value,
    read_regular_bytes,
)


MAX_HISTORY_BYTES = 4 * 1024 * 1024
MAX_HISTORY_ENTRIES = 100_000
ARXIV_ID_PATTERN = re.compile(
    r"^(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?$"
)
ALLOWED_ENTRY_FIELDS = frozenset({"id", "date", "title"})


class HistoryError(ValueError):
    """Recommendation history is malformed, unsafe, or cannot be persisted."""


def _normalize_entry(value: Any, *, index: int) -> dict[str, str]:
    if not isinstance(value, dict):
        raise HistoryError(f"History entry {index} must be a JSON object")
    unknown = set(value) - ALLOWED_ENTRY_FIELDS
    missing = {"id", "date"} - set(value)
    if unknown:
        raise HistoryError(
            f"History entry {index} has unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    if missing:
        raise HistoryError(
            f"History entry {index} is missing fields: "
            + ", ".join(sorted(missing))
        )
    arxiv_id = value["id"]
    if not isinstance(arxiv_id, str) or not ARXIV_ID_PATTERN.fullmatch(arxiv_id):
        raise HistoryError(f"History entry {index} has invalid arXiv id")
    date_value = value["date"]
    if not isinstance(date_value, str):
        raise HistoryError(f"History entry {index} has invalid date")
    try:
        date.fromisoformat(date_value)
    except ValueError as exc:
        raise HistoryError(f"History entry {index} has invalid date") from exc
    title = value.get("title", "")
    if not isinstance(title, str) or len(title) > 1000:
        raise HistoryError(f"History entry {index} has invalid title")
    return {"id": arxiv_id, "date": date_value, "title": title}


def validate_history(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise HistoryError("History root must be a JSON array")
    if len(value) > MAX_HISTORY_ENTRIES:
        raise HistoryError(
            f"History exceeds the {MAX_HISTORY_ENTRIES}-entry safety limit"
        )
    return [_normalize_entry(entry, index=index) for index, entry in enumerate(value)]


def load_history(path: Path, *, missing_ok: bool = True) -> list[dict[str, str]]:
    try:
        path = anchored_file_path(path, label="History")
        raw = read_regular_bytes(
            path,
            max_bytes=MAX_HISTORY_BYTES,
            required=not missing_ok,
            label="History",
        )
    except SafeIOError as exc:
        raise HistoryError(str(exc)) from exc
    if raw is None:
        return []
    try:
        value = parse_json_value(
            raw,
            max_bytes=MAX_HISTORY_BYTES,
            label="History",
        )
    except SafeIOError as exc:
        raise HistoryError(str(exc)) from exc
    return validate_history(value)


def save_history(path: Path, history: list[dict[str, str]]) -> None:
    normalized = validate_history(history)
    try:
        path = anchored_file_path(path, label="History")
        atomic_write_json(
            path,
            normalized,
            max_bytes=MAX_HISTORY_BYTES,
            mode=0o644,
            preserve_existing_mode=True,
            label="History",
        )
    except DocumentTooLargeError as exc:
        raise HistoryError(
            f"Serialized history exceeds the {MAX_HISTORY_BYTES}-byte safety limit"
        ) from exc
    except SafeIOError as exc:
        raise HistoryError(str(exc)) from exc
