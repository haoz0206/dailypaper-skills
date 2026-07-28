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

from user_config import daily_papers_dir

DAYS_TO_KEEP = 30


def history_file_path() -> Path:
    return daily_papers_dir() / ".history.json"


def load_history(history_file: Path | None = None) -> list:
    """Load existing history or return empty list."""
    history_file = history_file or history_file_path()
    if not history_file.exists():
        return []
    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def save_history(history: list, history_file: Path | None = None):
    """Save history to file."""
    history_file = history_file or history_file_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)
    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def extract_arxiv_id_from_url(url: str) -> str:
    """Extract arXiv ID from URL."""
    m = re.search(r'arxiv\.org/abs/(\d+\.\d+)', url)
    return m.group(1) if m else ""


def load_from_enriched(path: str) -> list:
    """Load papers from enriched JSON file."""
    with open(path, 'r', encoding='utf-8') as f:
        papers = json.load(f)

    entries = []
    for p in papers:
        arxiv_id = p.get('arxiv_id', '')
        if not arxiv_id:
            url = p.get('url', '')
            arxiv_id = extract_arxiv_id_from_url(url)

        if arxiv_id:
            entries.append({
                'id': arxiv_id,
                'title': p.get('title', '')[:200],
                'score': p.get('score', 0),
            })
    return entries


def load_from_recommendation(path: str) -> list:
    """Load papers from recommendation markdown file."""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Extract arXiv IDs from links
    arxiv_ids = re.findall(r'arxiv\.org/abs/(\d+\.\d+)', content)

    # Extract paper titles (### N. Title pattern)
    titles = {}
    for m in re.finditer(r'^### \d+\. (.+)$', content, re.MULTILINE):
        title = m.group(1).strip()
        # Extract arXiv ID from nearby lines
        idx = len(titles)
        titles[idx] = title

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


def main():
    parser = argparse.ArgumentParser(description='Update recommendation history')
    parser.add_argument('--arxiv-ids', nargs='+', help='arXiv IDs to add')
    parser.add_argument('--from-enriched', help='Path to enriched JSON file')
    parser.add_argument('--from-recommendation', help='Path to recommendation markdown file')
    parser.add_argument('--date', required=True, help='Date (YYYY-MM-DD)')
    parser.add_argument('--history-file', type=Path, help='Override history output path')

    args = parser.parse_args()

    entries = []

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
        sys.exit(1)

    added = update_history(entries, args.date, history_file=args.history_file)
    print(f"Added {added} new entries to history")


if __name__ == '__main__':
    main()
