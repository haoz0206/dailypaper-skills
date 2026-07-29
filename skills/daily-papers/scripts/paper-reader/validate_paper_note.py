#!/usr/bin/env python3
"""Deterministically validate the structural completeness of a paper note."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from paper_identity import (
    canonical_paper_id,
    paper_identity,
    parse_frontmatter_text,
)
from safe_io import SafeIOError, anchored_file_path, read_regular_bytes


MAX_NOTE_BYTES = 16 * 1024 * 1024
MIN_LINES = 120
MIN_FORMULAS = 2
MIN_IMAGES = 1
REQUIRED_SECTIONS = ("关键公式", "关键图表", "实验结果")
BLOCK_FORMULA = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
INLINE_FORMULA = re.compile(r"(?<!\$)\$(?!\$)(?:\\.|[^$\n])+\$(?!\$)")
SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
IMAGE = re.compile(r"!\[")


class NoteValidationError(RuntimeError):
    """The requested note cannot be read and validated."""


def validate_note(
    path: Path,
    *,
    expected_paper_id: str | None = None,
) -> dict[str, Any]:
    """Return a stable, machine-readable structural validation report."""
    try:
        note_path = anchored_file_path(path, label="Paper note")
        raw = read_regular_bytes(
            note_path,
            max_bytes=MAX_NOTE_BYTES,
            label="Paper note",
        )
        if raw is None:
            raise NoteValidationError(f"Paper note does not exist: {note_path}")
        text = raw.decode("utf-8")
    except SafeIOError as exc:
        raise NoteValidationError(str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise NoteValidationError(f"Paper note is not valid UTF-8: {note_path}") from exc

    line_count = len(text.splitlines())
    without_blocks = BLOCK_FORMULA.sub("", text)
    formula_count = len(BLOCK_FORMULA.findall(text)) + len(
        INLINE_FORMULA.findall(without_blocks)
    )
    image_count = len(IMAGE.findall(text))
    present_sections = {match.strip() for match in SECTION.findall(text)}
    missing_sections = [
        section for section in REQUIRED_SECTIONS if section not in present_sections
    ]
    metadata = parse_frontmatter_text(text)
    actual_paper_id = paper_identity(metadata)
    expected_identity = (
        canonical_paper_id(expected_paper_id)
        if expected_paper_id is not None
        else None
    )
    if expected_paper_id is not None and expected_identity is None:
        raise NoteValidationError(
            f"Expected paper identity is invalid: {expected_paper_id!r}"
        )
    raw_paper_id = metadata.get("paper_id")
    declared_paper_id = (
        canonical_paper_id(raw_paper_id)
        if isinstance(raw_paper_id, str) and ":" in raw_paper_id
        else None
    )
    identity_valid = (
        declared_paper_id == expected_identity
        if expected_identity is not None
        else raw_paper_id is None or declared_paper_id is not None
    )

    checks = {
        "line_count": {
            "actual": line_count,
            "minimum": MIN_LINES,
            "valid": line_count >= MIN_LINES,
        },
        "formula_count": {
            "actual": formula_count,
            "minimum": MIN_FORMULAS,
            "valid": formula_count >= MIN_FORMULAS,
        },
        "image_count": {
            "actual": image_count,
            "minimum": MIN_IMAGES,
            "valid": image_count >= MIN_IMAGES,
        },
        "required_sections": {
            "required": list(REQUIRED_SECTIONS),
            "missing": missing_sections,
            "valid": not missing_sections,
        },
        "paper_identity": {
            "actual": actual_paper_id,
            "declared": declared_paper_id,
            "expected": expected_identity,
            "legacy_missing": raw_paper_id is None,
            "valid": identity_valid,
        },
    }
    failures = [name for name, check in checks.items() if not check["valid"]]
    return {
        "version": 1,
        "path": str(note_path),
        "valid": not failures,
        "checks": checks,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the structural completeness of one DailyPaper note."
    )
    parser.add_argument("note", type=Path)
    parser.add_argument(
        "--expected-paper-id",
        help="Require this canonical arXiv/DOI/Zotero/PDF identity in frontmatter",
    )
    args = parser.parse_args()
    try:
        report = validate_note(
            args.note,
            expected_paper_id=args.expected_paper_id,
        )
    except NoteValidationError as exc:
        print(
            json.dumps(
                {"version": 1, "valid": False, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
