#!/usr/bin/env python3
"""
update_history.py - Update the recommendation history file.

This script is part of daily-papers-review (Phase 6).

Usage:
    python3 update_history.py --arxiv-ids ID1 ID2 ... --date YYYY-MM-DD
    python3 update_history.py --from-enriched /run/enriched.json --date YYYY-MM-DD
    python3 update_history.py --from-recommendation YYYY-MM-DD-论文推荐.md --date YYYY-MM-DD

The script:
1. Reads existing history from the configured daily-papers directory
2. Adds new entries for papers not already in history
3. Preserves the earliest date for papers that are re-recommended
4. Removes entries older than 30 days
5. Writes back to .history.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from history_store import (
    HistoryError,
    load_history as load_history_file,
    save_history as save_history_file,
)
from safe_io import SafeIOError, parse_json_value, read_regular_bytes
from user_config import daily_papers_dir

DAYS_TO_KEEP = 30
MAX_ENRICHED_INPUT_BYTES = 16 * 1024 * 1024
MAX_RECOMMENDATION_INPUT_BYTES = 16 * 1024 * 1024
MAX_INPUT_PAPERS = 100_000


def history_file_path() -> Path:
    return daily_papers_dir() / ".history.json"


def load_history(history_file: Path | None = None) -> list:
    """Load strict history or return empty only when the file is absent."""
    return load_history_file(history_file or history_file_path())


def save_history(history: list, history_file: Path | None = None):
    """Atomically save validated history."""
    save_history_file(history_file or history_file_path(), history)


def extract_arxiv_id_from_url(url: str) -> str:
    """Extract arXiv ID from URL."""
    m = re.search(r'arxiv\.org/abs/(\d+\.\d+)', url)
    return m.group(1) if m else ""


def _read_input(path: str | Path, *, limit: int, label: str) -> bytes:
    try:
        raw = read_regular_bytes(
            Path(path),
            max_bytes=limit,
            label=label,
        )
    except SafeIOError as exc:
        raise HistoryError(str(exc)) from exc
    if raw is None:
        raise HistoryError(f"{label} does not exist: {path}")
    return raw


def load_from_enriched(path: str | Path) -> list:
    """Load papers from enriched JSON file."""
    raw = _read_input(
        path,
        limit=MAX_ENRICHED_INPUT_BYTES,
        label="Enriched paper input",
    )
    try:
        papers = parse_json_value(
            raw,
            max_bytes=MAX_ENRICHED_INPUT_BYTES,
            label="Enriched paper input",
        )
    except SafeIOError as exc:
        raise HistoryError(str(exc)) from exc
    if not isinstance(papers, list):
        raise HistoryError("Enriched paper input root must be a JSON array")
    if len(papers) > MAX_INPUT_PAPERS:
        raise HistoryError(
            f"Enriched paper input exceeds the {MAX_INPUT_PAPERS}-paper safety limit"
        )

    entries = []
    for index, p in enumerate(papers):
        if not isinstance(p, dict):
            raise HistoryError(f"Enriched paper {index} must be a JSON object")
        arxiv_id = p.get('arxiv_id', '')
        if not isinstance(arxiv_id, str):
            raise HistoryError(f"Enriched paper {index} has invalid arxiv_id")
        if not arxiv_id:
            url = p.get('url', '')
            if not isinstance(url, str):
                raise HistoryError(f"Enriched paper {index} has invalid url")
            arxiv_id = extract_arxiv_id_from_url(url)

        if arxiv_id:
            title = p.get('title', '')
            if not isinstance(title, str):
                raise HistoryError(f"Enriched paper {index} has invalid title")
            entries.append({
                'id': arxiv_id,
                'title': title[:200],
                'score': p.get('score', 0),
            })
    return entries


def load_from_recommendation(path: str | Path) -> list:
    """Load papers from recommendation markdown file."""
    raw = _read_input(
        path,
        limit=MAX_RECOMMENDATION_INPUT_BYTES,
        label="Recommendation input",
    )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HistoryError("Recommendation input is not valid UTF-8") from exc

    # Extract arXiv IDs from links
    arxiv_ids = re.findall(r'arxiv\.org/abs/(\d+\.\d+)', content)
    if len(arxiv_ids) > MAX_INPUT_PAPERS:
        raise HistoryError(
            f"Recommendation input exceeds the {MAX_INPUT_PAPERS}-paper safety limit"
        )

    entries = []
    for arxiv_id in arxiv_ids:
        entries.append({
            'id': arxiv_id,
            'title': '',  # Would need more complex parsing to match
        })
    return entries


def update_history(
    entries: list,
    date: str,
    preserve_earliest: bool = True,
    history_file: Path | None = None,
):
    """Update history with new entries."""
    history = load_history(history_file)

    # Build index of existing IDs
    existing_ids = {h.get('id') for h in history if h.get('id')}

    # Add new entries
    added = 0
    for entry in entries:
        arxiv_id = entry.get('id', '')
        if not arxiv_id:
            continue

        if arxiv_id not in existing_ids:
            history.append({
                'id': arxiv_id,
                'date': date,
                'title': entry.get('title', ''),
            })
            existing_ids.add(arxiv_id)
            added += 1
        elif preserve_earliest:
            # Update to preserve earliest date
            for h in history:
                if h.get('id') == arxiv_id:
                    if h.get('date', '') > date:
                        h['date'] = date
                    break

    # Remove old entries (older than 30 days)
    cutoff_date = (datetime.strptime(date, '%Y-%m-%d') - timedelta(days=DAYS_TO_KEEP)).strftime('%Y-%m-%d')
    history = [h for h in history if h.get('date', '') >= cutoff_date]

    save_history(history, history_file)
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description='Update recommendation history')
    parser.add_argument('--arxiv-ids', nargs='+', help='arXiv IDs to add')
    parser.add_argument('--from-enriched', help='Path to enriched JSON file')
    parser.add_argument('--from-recommendation', help='Path to recommendation markdown file')
    parser.add_argument('--date', required=True, help='Date (YYYY-MM-DD)')
    parser.add_argument('--history-file', type=Path, help='Override history output path')

    args = parser.parse_args()

    try:
        if args.arxiv_ids:
            entries = [{'id': aid, 'title': ''} for aid in args.arxiv_ids]
        elif args.from_enriched:
            entries = load_from_enriched(args.from_enriched)
        elif args.from_recommendation:
            entries = load_from_recommendation(args.from_recommendation)
        else:
            print(
                "Error: Must specify --arxiv-ids, --from-enriched, or --from-recommendation",
                file=sys.stderr,
            )
            return 2
        added = update_history(entries, args.date, history_file=args.history_file)
    except (HistoryError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "version": 1,
                    "status": "blocked",
                    "code": "invalid-history",
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {"version": 1, "status": "updated", "added": added},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
