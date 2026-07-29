#!/usr/bin/env python3
"""
backfill_links.py - Backfill paper note links to recommendation file.

This script is part of daily-papers-notes (Step 3).

Usage:
    python3 backfill_links.py --recommendation YYYY-MM-DD-论文推荐.md
    python3 backfill_links.py --recommendation YYYY-MM-DD-论文推荐.md --notes-dir 论文笔记

The script:
1. Scans the notes directory for existing paper notes
2. Matches papers in the recommendation file with existing notes
3. Inserts note links after the "来源" line for each matched paper
4. Updates the "分流表" section to use the correct wikilink names
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from paper_identity import NoteIndex, build_note_index, match_paper_to_note
from safe_io import SafeIOError, atomic_write_bytes, read_regular_bytes
from user_config import concepts_dir, obsidian_vault_path, paper_notes_dir

MAX_RECOMMENDATION_BYTES = 16 * 1024 * 1024


def scan_notes(
    notes_dir: Path | None = None,
    concepts_path: Path | None = None,
    vault: Path | None = None,
) -> NoteIndex:
    """Build a collision-preserving stable-identity index of paper notes."""
    notes_dir = (notes_dir or paper_notes_dir()).resolve()
    concepts_path = (concepts_path or concepts_dir()).resolve()
    return build_note_index(
        notes_dir,
        concepts_dir=concepts_path,
        vault=(vault or obsidian_vault_path()).resolve(),
    )


def extract_method_name_from_title(title: str) -> str:
    """Extract method name from paper title.

    Examples:
        "NavThinker: Action-Conditioned..." -> "NavThinker"
        "HapticVLA: Contact-Rich..." -> "HapticVLA"
        "ForceVLA2: Unleashing..." -> "ForceVLA2"
    """
    # Try to find text before colon
    if ':' in title:
        method_name = title.split(':')[0].strip()
        # Clean up common patterns
        method_name = re.sub(r'^\d+\.\s*', '', method_name)  # Remove "1. " prefix
        return method_name
    return title.split()[0] if title else ""


def match_papers_with_notes(content: str, notes_index: NoteIndex) -> list:
    """Match papers in recommendation with existing notes.

    Returns list of dicts with paper_title, method_name, note_name, section_start, source_line_end
    """
    matches = []

    # Find all paper sections (### N. Title pattern)
    for m in re.finditer(r'^### \d+\. (.+)$', content, re.MULTILINE):
        paper_title = m.group(1).strip()
        section_start = m.start()

        # Find the next section end
        next_section = re.search(r'^### (?:\d+\.|\w)', content[section_start + 1:], re.MULTILINE)
        section_end = (
            section_start + 1 + next_section.start()
            if next_section
            else len(content)
        )

        section_content = content[section_start:section_end]

        method_name = extract_method_name_from_title(paper_title)

        # Look for "来源" line
        source_match = re.search(r'^- \*\*来源\*\*:.*$', section_content, re.MULTILINE)
        if not source_match:
            continue

        source_line_end = source_match.end()

        # Check if note link already exists
        if re.search(r'- 📒 \*\*(?:已有)?笔记\*\*:', section_content):
            continue  # Already has note link

        arxiv_match = re.search(
            r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/[^\s)|]+",
            section_content,
            re.IGNORECASE,
        )
        paper = {
            "title": paper_title,
            "method_names": [method_name] if method_name else [],
            "url": arxiv_match.group(0) if arxiv_match else "",
        }
        matched = match_paper_to_note(paper, notes_index)
        if matched["status"] in {"exact", "fallback"}:
            note = matched["note"]
            matches.append({
                'paper_title': paper_title,
                'method_name': method_name,
                'note_name': note["wikilink"],
                'note_path': note["path"],
                'match_basis': matched["basis"],
                'section_start': section_start,
                'source_line_end': section_start + source_line_end,
            })

    return matches


def backfill_links(recommendation_path: Path, notes_index: NoteIndex) -> int:
    """Backfill note links to recommendation file."""
    try:
        raw = read_regular_bytes(
            recommendation_path,
            max_bytes=MAX_RECOMMENDATION_BYTES,
            label="Recommendation",
        )
        if raw is None:
            raise SafeIOError(
                f"Recommendation file does not exist: {recommendation_path}"
            )
        content = raw.decode("utf-8")
    except (SafeIOError, UnicodeDecodeError) as exc:
        raise ValueError(f"Cannot safely read recommendation: {exc}") from exc

    matches = match_papers_with_notes(content, notes_index)

    if not matches:
        print("No papers matched with existing notes")
        return 0

    # Insert note links (in reverse order to preserve positions)
    for match in reversed(matches):
        insert_text = f'\n- 📒 **笔记**: [[{match["note_name"]}]]'
        content = (
            content[:match['source_line_end']] +
            insert_text +
            content[match['source_line_end']:]
        )

    content = update_diversion_table_content(content, matches)
    _atomic_write_text(recommendation_path, content)

    return len(matches)


def update_diversion_table_content(content: str, matches: list) -> str:
    """Return content with matched diversion-table wikilinks corrected."""
    # Find 分流表 section
    table_match = re.search(r'^## 分流表$.+?(?=^##|\Z)', content, re.MULTILINE | re.DOTALL)
    if not table_match:
        return content

    table_start = table_match.start()
    table_end = table_match.end()
    table_content = content[table_start:table_end]

    # Update wikilinks for papers that have notes
    for match in matches:
        if match['method_name'].lower() != match['note_name'].lower():
            table_content = re.sub(
                rf'\[\[{re.escape(match["method_name"])}\]\]',
                f'[[{match["note_name"]}]]',
                table_content,
                count=1,
                flags=re.IGNORECASE
            )

    # Replace the table in content
    return content[:table_start] + table_content + content[table_end:]


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace one recommendation without a shared temp filename."""
    try:
        atomic_write_bytes(
            path,
            content.encode("utf-8"),
            mode=0o644,
            preserve_existing_mode=True,
            label="Recommendation",
        )
    except SafeIOError as exc:
        raise ValueError(str(exc)) from exc


def main():
    parser = argparse.ArgumentParser(description='Backfill paper note links')
    parser.add_argument('--recommendation', required=True, help='Path to recommendation file')
    parser.add_argument('--notes-dir', type=Path, help='Path to notes directory (default: from config)')
    parser.add_argument('--concepts-dir', type=Path)
    parser.add_argument('--vault', type=Path)

    args = parser.parse_args()

    recommendation_path = Path(args.recommendation)
    if not recommendation_path.exists():
        print(f"Error: Recommendation file not found: {recommendation_path}", file=sys.stderr)
        sys.exit(1)

    explicit_paths = (args.vault, args.notes_dir, args.concepts_dir)
    if any(path is not None for path in explicit_paths) and not all(
        path is not None for path in explicit_paths
    ):
        parser.error("--vault, --notes-dir and --concepts-dir must be provided together")
    vault = args.vault.resolve() if args.vault else None
    notes_dir = args.notes_dir.resolve() if args.notes_dir else None
    concepts_path = args.concepts_dir.resolve() if args.concepts_dir else None

    # Scan notes
    notes_index = scan_notes(
        notes_dir=notes_dir,
        concepts_path=concepts_path,
        vault=vault,
    )
    print(f"Found {len(notes_index.records)} paper notes")

    # Backfill links
    count = backfill_links(recommendation_path, notes_index)
    print(f"Added {count} note links to recommendation file")


if __name__ == '__main__':
    main()
