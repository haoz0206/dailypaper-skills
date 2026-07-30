#!/usr/bin/env python3
"""
fetch_and_score.py — Acquire complete arXiv metadata and deterministic signals.

This stage deliberately does not make a relevance decision.  Every paper in
the bounded category/date query remains available for semantic approval.

Usage:
    python3 fetch_and_score.py --output /path/to/candidates.json
    python3 fetch_and_score.py --date 2026-02-25 --output /path/to/candidates.json
    python3 fetch_and_score.py --days 7 --output /path/to/candidates.json

Stderr: progress logs.  Stdout: JSON array of acquired papers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import config_schema
from paper_identity import canonical_arxiv_id
from history_store import load_history as load_history_file
from safe_http import FetchBudget, SafeHTTPClient, SafeHTTPError
from safe_io import (
    SafeIOError,
    anchored_file_path,
    atomic_write_bytes,
    encode_json_value,
    load_json_object,
    parse_json_value,
    read_regular_bytes,
)
from user_config import DEFAULT_CONFIG

# ── Configuration ──────────────────────────────────────────────────────────

_CONFIG = DEFAULT_CONFIG["daily_papers"]

KEYWORDS = _CONFIG["keywords"]
NEGATIVE_KEYWORDS = _CONFIG["negative_keywords"]
DOMAIN_BOOST_KEYWORDS = _CONFIG["domain_boost_keywords"]
ARXIV_CATEGORIES = _CONFIG["arxiv_categories"]
MIN_SCORE = _CONFIG["min_score"]
TOP_N = _CONFIG["top_n"]

DAILYPAPERS_DIR = Path(".")
HISTORY_PATH = DAILYPAPERS_DIR / ".history.json"
MAX_RUNTIME_CONTEXT_BYTES = 1024 * 1024
MAX_FETCH_BYTES = 32 * 1024 * 1024
MAX_RECOMMENDATION_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_FETCH_DAYS = 31
MAX_HF_ITEMS_PER_RESPONSE = 1_000
ARXIV_PAGE_SIZE = 500
MAX_ARXIV_RESULTS = 3_000
MAX_ACQUIRED_PAPERS = 3_200
ARXIV_PAGE_DELAY_SECONDS = 3.0
MAX_TOTAL_FETCH_BYTES = 96 * 1024 * 1024
FETCH_REQUEST_TIMEOUT_SECONDS = 60
FETCH_RUN_TIMEOUT_SECONDS = 10 * 60

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}
HTTP_CLIENT = SafeHTTPClient()


class RuntimeContextError(ValueError):
    """The coordinator-frozen runtime context is missing or invalid."""


class ArxivFetchError(RuntimeError):
    """A complete bounded arXiv metadata snapshot could not be acquired."""


def configure_runtime_context(path: Path) -> str:
    """Load the coordinator-frozen context once and configure this process."""
    global KEYWORDS, NEGATIVE_KEYWORDS, DOMAIN_BOOST_KEYWORDS
    global ARXIV_CATEGORIES, MIN_SCORE, TOP_N, DAILYPAPERS_DIR, HISTORY_PATH

    try:
        resolved = anchored_file_path(path, label="Runtime Context")
        context = load_json_object(
            resolved,
            max_bytes=MAX_RUNTIME_CONTEXT_BYTES,
            label="Runtime Context",
        )
        if context is None:
            raise SafeIOError(f"Runtime Context file does not exist: {resolved}")
    except SafeIOError as exc:
        raise RuntimeContextError(str(exc)) from exc
    if not isinstance(context, dict) or context.get("status") != "ready":
        raise RuntimeContextError("Runtime Context status must be 'ready'")
    config = context.get("daily_papers")
    paths = context.get("paths")
    runtime = context.get("runtime")
    if (
        not isinstance(config, dict)
        or not isinstance(paths, dict)
        or not isinstance(runtime, dict)
    ):
        raise RuntimeContextError(
            "Runtime Context requires daily_papers, paths, and runtime objects"
        )
    try:
        normalized_config = config_schema.normalize_daily_config(config)
    except config_schema.ConfigurationError as exc:
        raise RuntimeContextError(
            f"Runtime Context daily_papers is invalid: {exc}"
        ) from exc
    daily_path = Path(str(paths.get("daily_papers", ""))).expanduser()
    if not daily_path.is_absolute():
        raise RuntimeContextError(
            "Runtime Context paths.daily_papers must be absolute"
        )
    timezone = runtime.get("timezone")
    if not isinstance(timezone, str) or not timezone:
        raise RuntimeContextError(
            "Runtime Context runtime.timezone must be non-empty"
        )
    try:
        ZoneInfo(timezone)
    except (KeyError, ValueError) as exc:
        raise RuntimeContextError(
            f"Runtime Context timezone is invalid: {timezone}"
        ) from exc

    KEYWORDS = list(normalized_config["keywords"])
    NEGATIVE_KEYWORDS = list(normalized_config["negative_keywords"])
    DOMAIN_BOOST_KEYWORDS = list(normalized_config["domain_boost_keywords"])
    ARXIV_CATEGORIES = list(normalized_config["arxiv_categories"])
    MIN_SCORE = normalized_config["min_score"]
    TOP_N = normalized_config["top_n"]
    DAILYPAPERS_DIR = daily_path.resolve()
    HISTORY_PATH = DAILYPAPERS_DIR / ".history.json"
    return timezone

# ── Scoring ────────────────────────────────────────────────────────────────


def score_paper(paper: dict, is_trending: bool = False) -> int:
    text = (paper["title"] + " " + paper["abstract"]).lower()
    title_lower = paper["title"].lower()

    score = 0

    # 1. Positive keywords
    keyword_hits = 0
    for kw in KEYWORDS:
        if kw in title_lower:
            score += 3
            keyword_hits += 1
        elif kw in text:
            score += 1
            keyword_hits += 1

    # 2. Domain boost
    domain_hits = sum(1 for kw in DOMAIN_BOOST_KEYWORDS if kw in text)
    if domain_hits >= 2:
        score += 2
    elif domain_hits == 1:
        score += 1

    # 3. Negative keywords are a signal, never an acquisition-time veto.
    negative_hits = sum(1 for keyword in NEGATIVE_KEYWORDS if keyword in text)
    score -= min(negative_hits, 3) * 3

    # 4. Trending boost (HF sources only)
    #    GATE: only apply if paper has at least 1 keyword or domain match,
    #    to prevent irrelevant but popular papers from flooding the list
    has_relevance = keyword_hits > 0 or domain_hits > 0
    if is_trending:
        upvotes = paper.get("hf_upvotes", 0) or 0
        if has_relevance:
            # Relevant + trending → full boost
            if upvotes >= 10:
                score += 3
            elif upvotes >= 5:
                score += 2
            elif upvotes >= 2:
                score += 1
        else:
            # No relevance → minimal boost (only very popular papers get a chance)
            if upvotes >= 20:
                score += 1

    return score


# ── Fetchers ───────────────────────────────────────────────────────────────


def fetch_url(
    url: str,
    timeout: int = 30,
    *,
    client: SafeHTTPClient | None = None,
    budget: FetchBudget | None = None,
) -> str:
    """Fetch one bounded UTF-8 metadata response through the shared HTTP seam."""
    active_client = client or HTTP_CLIENT
    try:
        active_budget = budget or active_client.new_budget(
            max_total_bytes=MAX_FETCH_BYTES,
            request_timeout_seconds=timeout,
            run_timeout_seconds=timeout + 5,
        )
        response = active_client.fetch_bytes(
            url,
            max_bytes=MAX_FETCH_BYTES,
            budget=active_budget,
            accept=(
                "application/json, application/atom+xml, "
                "application/xml, text/xml;q=0.9"
            ),
        )
        return response.body.decode("utf-8")
    except (SafeHTTPError, UnicodeDecodeError, ValueError) as e:
        print(f"  [WARN] fetch failed {url}: {e}", file=sys.stderr)
        return ""


def _parse_hf_item(item: dict, source: str) -> tuple[str, dict] | None:
    """Parse a single HF API item into (arxiv_id, paper_dict). Returns None on skip."""
    if not isinstance(item, dict):
        return None
    p = item.get("paper")
    if not isinstance(p, dict):
        return None
    arxiv_id = canonical_arxiv_id(p.get("id", ""))
    if not arxiv_id:
        return None

    upvotes = p.get("upvotes", 0)
    if isinstance(upvotes, bool) or not isinstance(upvotes, (int, float)):
        upvotes = 0

    # Authors
    authors_raw = p.get("authors", [])
    if isinstance(authors_raw, list):
        names = []
        for a in authors_raw:
            if isinstance(a, dict):
                names.append(a.get("name", ""))
            elif isinstance(a, str):
                names.append(a)
        authors = ", ".join(n for n in names if n)
    elif isinstance(authors_raw, str):
        authors = authors_raw
    else:
        authors = ""

    title = p.get("title")
    summary = p.get("summary")
    published_at = p.get("publishedAt")
    title = title if isinstance(title, str) else ""
    summary = summary if isinstance(summary, str) else ""
    published_at = published_at if isinstance(published_at, str) else ""
    title = " ".join(title.split())
    summary = " ".join(summary.split())
    if not title or not summary:
        return None

    paper = {
        "paper_id": f"arxiv:{arxiv_id}",
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "affiliations": "",
        "abstract": summary,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf": f"https://arxiv.org/pdf/{arxiv_id}",
        "date": published_at[:10],
        "score": 0,
        "category": "",
        "source": source,
        "hf_upvotes": upvotes,
    }

    is_trending = source == "hf-trending"
    paper["score"] = score_paper(paper, is_trending=is_trending)

    return arxiv_id, paper


def _decode_hf_items(raw: str, *, label: str) -> list[dict]:
    """Decode one untrusted HF response into only object-shaped items."""
    if not raw:
        return []
    try:
        value = parse_json_value(
            raw.encode("utf-8"),
            max_bytes=MAX_FETCH_BYTES,
            label=label,
        )
    except SafeIOError as exc:
        print(f"  [WARN] {label} rejected: {exc}", file=sys.stderr)
        return []
    if not isinstance(value, list):
        print(f"  [WARN] {label} root is not an array", file=sys.stderr)
        return []
    if len(value) > MAX_HF_ITEMS_PER_RESPONSE:
        print(
            f"  [WARN] {label} exceeds the "
            f"{MAX_HF_ITEMS_PER_RESPONSE}-item limit",
            file=sys.stderr,
        )
        return []
    objects = [item for item in value if isinstance(item, dict)]
    skipped = len(value) - len(objects)
    if skipped:
        print(
            f"  [WARN] {label} skipped {skipped} non-object items",
            file=sys.stderr,
        )
    return objects


def _merge_hf_endpoint(
    papers: dict[str, dict],
    *,
    endpoint: str,
    source: str,
    label: str,
    client: SafeHTTPClient | None = None,
    budget: FetchBudget | None = None,
) -> None:
    """Fetch, validate, score, and deduplicate one HF endpoint."""
    for item in _decode_hf_items(
        fetch_url(endpoint, client=client, budget=budget),
        label=label,
    ):
        parsed = _parse_hf_item(item, source)
        if parsed is None:
            continue
        arxiv_id, paper = parsed
        previous = papers.get(arxiv_id)
        if previous is None or paper["score"] > previous["score"]:
            papers[arxiv_id] = paper


def fetch_hf_papers(
    start_date=None,
    end_date=None,
    *,
    client: SafeHTTPClient | None = None,
    budget: FetchBudget | None = None,
) -> list[dict]:
    papers = {}  # arxiv_id → paper

    # ── hf-daily: loop each day in range ──
    if start_date and end_date:
        d = start_date
        while d <= end_date:
            date_str = d.isoformat()
            endpoint = f"https://huggingface.co/api/daily_papers?date={date_str}&limit=100"
            print(f"  Fetching hf-daily {date_str}...", file=sys.stderr)
            _merge_hf_endpoint(
                papers,
                endpoint=endpoint,
                source="hf-daily",
                label=f"hf-daily {date_str}",
                client=client,
                budget=budget,
            )
            d += timedelta(days=1)
    else:
        # Legacy single-call (days=1 default)
        endpoint = "https://huggingface.co/api/daily_papers?limit=50"
        print(f"  Fetching hf-daily...", file=sys.stderr)
        _merge_hf_endpoint(
            papers,
            endpoint=endpoint,
            source="hf-daily",
            label="hf-daily",
            client=client,
            budget=budget,
        )

    # ── hf-trending: always single call (not date-dependent) ──
    endpoint = "https://huggingface.co/api/daily_papers?sort=trending&limit=50"
    print(f"  Fetching hf-trending...", file=sys.stderr)
    _merge_hf_endpoint(
        papers,
        endpoint=endpoint,
        source="hf-trending",
        label="hf-trending",
        client=client,
        budget=budget,
    )

    result = list(papers.values())
    print(f"  HF: {len(result)} papers after scoring", file=sys.stderr)
    return result


def fetch_arxiv_papers(
    start_date=None,
    end_date=None,
    days: int = 1,
    *,
    client: SafeHTTPClient | None = None,
    budget: FetchBudget | None = None,
    sleep=time.sleep,
    snapshot: dict | None = None,
) -> list[dict]:
    if start_date is None or end_date is None:
        end_date = date.today()
        start_date = end_date - timedelta(days=days - 1)
    category_query = " OR ".join(f"cat:{category}" for category in ARXIV_CATEGORIES)
    date_query = (
        f"submittedDate:[{start_date:%Y%m%d}0000 TO {end_date:%Y%m%d}2359]"
    )
    search_query = f"({category_query}) AND {date_query}"
    timeout = max(60, 30 * days)
    papers = []
    seen_arxiv_ids: set[str] = set()
    offset = 0
    total_results: int | None = None
    while total_results is None or offset < total_results:
        page_size = min(ARXIV_PAGE_SIZE, MAX_ARXIV_RESULTS - offset)
        if page_size <= 0:
            raise ArxivFetchError(
                f"arXiv query exceeds the {MAX_ARXIV_RESULTS}-paper safety limit"
            )
        url = "https://export.arxiv.org/api/query?" + urlencode(
            {
                "search_query": search_query,
                "start": offset,
                "max_results": page_size,
                "sortBy": "submittedDate",
                "sortOrder": "ascending",
            }
        )
        print(
            f"  Fetching arXiv page start={offset}, max_results={page_size}...",
            file=sys.stderr,
        )
        xml_text = fetch_url(
            url,
            timeout=timeout,
            client=client,
            budget=budget,
        )
        if not xml_text:
            raise ArxivFetchError(
                f"arXiv page at offset {offset} could not be fetched"
            )
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml_text, flags=re.IGNORECASE):
            raise ArxivFetchError(
                "arXiv XML contains a forbidden DTD or entity declaration"
            )
        try:
            root = ET.fromstring(xml_text)  # noqa: S314
        except ET.ParseError as exc:
            raise ArxivFetchError(f"arXiv XML parse error: {exc}") from exc
        total_element = root.find("opensearch:totalResults", ATOM_NS)
        if total_element is None or total_element.text is None:
            raise ArxivFetchError("arXiv response omitted totalResults")
        try:
            page_total = int(total_element.text.strip(), 10)
        except ValueError as exc:
            raise ArxivFetchError("arXiv totalResults is not an integer") from exc
        if page_total < 0:
            raise ArxivFetchError("arXiv totalResults must not be negative")
        if total_results is None:
            total_results = page_total
            if total_results > MAX_ARXIV_RESULTS:
                raise ArxivFetchError(
                    f"arXiv query returned {total_results} papers, exceeding "
                    f"the {MAX_ARXIV_RESULTS}-paper safety limit"
                )
        elif page_total != total_results:
            raise ArxivFetchError("arXiv totalResults changed during pagination")

        entries = list(root.findall("atom:entry", ATOM_NS))
        if len(entries) > page_size:
            raise ArxivFetchError(
                "arXiv response contains more entries than requested"
            )
        if not entries and offset < total_results:
            raise ArxivFetchError(
                f"arXiv pagination ended early at {offset} of {total_results}"
            )
        for entry in entries:
            title_el = entry.find("atom:title", ATOM_NS)
            summary_el = entry.find("atom:summary", ATOM_NS)
            published_el = entry.find("atom:published", ATOM_NS)
            id_el = entry.find("atom:id", ATOM_NS)

            if (
                title_el is None
                or not title_el.text
                or summary_el is None
                or not summary_el.text
            ):
                raise ArxivFetchError(
                    f"arXiv entry at offset {offset} omitted title or abstract"
                )

            title = " ".join(title_el.text.split())
            abstract = " ".join(summary_el.text.split())
            entry_url = (
                id_el.text.strip()
                if id_el is not None and id_el.text
                else ""
            )
            published_date = (
                published_el.text[:10]
                if published_el is not None and published_el.text
                else ""
            )
            arxiv_id = canonical_arxiv_id(entry_url) or ""
            if not arxiv_id:
                raise ArxivFetchError(
                    f"arXiv entry at offset {offset} omitted a valid paper ID"
                )
            if arxiv_id in seen_arxiv_ids:
                raise ArxivFetchError(
                    f"arXiv pagination repeated paper ID {arxiv_id}"
                )
            seen_arxiv_ids.add(arxiv_id)

            author_els = entry.findall("atom:author", ATOM_NS)
            names = []
            affiliations = set()
            for author in author_els:
                name_el = author.find("atom:name", ATOM_NS)
                if name_el is not None and name_el.text:
                    names.append(name_el.text.strip())
                for aff_el in author.findall("arxiv:affiliation", ATOM_NS):
                    if aff_el.text and aff_el.text.strip():
                        affiliations.add(aff_el.text.strip())

            cat_el = entry.find("arxiv:primary_category", ATOM_NS)
            category = cat_el.get("term", "") if cat_el is not None else ""
            categories = [
                value
                for category_element in entry.findall("atom:category", ATOM_NS)
                if (value := category_element.get("term", "").strip())
            ]

            paper = {
                "paper_id": f"arxiv:{arxiv_id}",
                "arxiv_id": arxiv_id,
                "title": title,
                "authors": ", ".join(names),
                "affiliations": (
                    ", ".join(sorted(affiliations)) if affiliations else ""
                ),
                "abstract": abstract,
                "url": entry_url,
                "pdf": f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "",
                "date": published_date,
                "score": 0,
                "category": category,
                "categories": categories,
                "source": "arxiv",
            }
            paper["score"] = score_paper(paper)
            papers.append(paper)

        offset += len(entries)
        if total_results is not None and offset < total_results:
            sleep(ARXIV_PAGE_DELAY_SECONDS)

    if snapshot is not None:
        snapshot.update(
            {
                "complete": True,
                "query_total": total_results or 0,
                "parsed": len(papers),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "categories": list(ARXIV_CATEGORIES),
            }
        )
    print(f"  arXiv: complete snapshot of {len(papers)} papers", file=sys.stderr)
    return papers


# ── Merge & Dedup ──────────────────────────────────────────────────────────


def extract_arxiv_id(url: str) -> str:
    return canonical_arxiv_id(url) or ""


def load_history() -> list[dict]:
    return load_history_file(HISTORY_PATH)


def load_fallback_ids(reference_date: date, days: int = 7) -> set[str]:
    ids: set[str] = set()
    for d in range(1, days + 1):
        previous_date = reference_date - timedelta(days=d)
        fpath = DAILYPAPERS_DIR / f"{previous_date.isoformat()}-论文推荐.md"
        try:
            raw = read_regular_bytes(
                fpath,
                max_bytes=MAX_RECOMMENDATION_BYTES,
                required=False,
                label="Recommendation history",
            )
            if raw is None:
                continue
            text = raw.decode("utf-8")
            for m in re.finditer(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", text):
                ids.add(m.group(1))
        except (SafeIOError, UnicodeDecodeError):
            pass
    return ids


def merge_and_dedup(
    hf_papers: list[dict],
    arxiv_papers: list[dict],
    target_date,
    days: int = 1,
    top_n: int | None = TOP_N,
) -> list[dict]:
    is_weekend = target_date.weekday() >= 5

    # The complete arXiv category snapshot is authoritative whenever it is
    # non-empty. HF contributes ranking signals only for matching IDs. A
    # proven-empty arXiv snapshot (normally a weekend/holiday) retains the
    # legacy bounded HF fallback.
    hf_by_id: dict[str, dict] = {}
    for paper in hf_papers:
        arxiv_id = extract_arxiv_id(paper["url"])
        if not arxiv_id:
            continue
        previous = hf_by_id.get(arxiv_id)
        if previous is None or paper["score"] > previous["score"]:
            hf_by_id[arxiv_id] = paper

    by_id: dict[str, dict] = {}
    base_papers = arxiv_papers if arxiv_papers else hf_papers
    for paper in base_papers:
        arxiv_id = extract_arxiv_id(paper["url"])
        if not arxiv_id:
            continue
        current = dict(paper)
        hf_signal = hf_by_id.get(arxiv_id)
        if arxiv_papers and hf_signal is not None:
            current["score"] = max(current["score"], hf_signal["score"])
            current["hf_upvotes"] = hf_signal.get("hf_upvotes", 0) or 0
            current["hf_source"] = hf_signal.get("source", "")
        previous = by_id.get(arxiv_id)
        if previous is None or current["score"] > previous["score"]:
            by_id[arxiv_id] = current

    if len(by_id) > MAX_ACQUIRED_PAPERS:
        raise ArxivFetchError(
            f"acquired metadata exceeds the {MAX_ACQUIRED_PAPERS}-paper safety limit"
        )
    print(
        f"  Acquired: {len(by_id)} unique papers "
        f"({'arXiv snapshot' if arxiv_papers else 'HF empty-snapshot fallback'})",
        file=sys.stderr,
    )

    if days > 1:
        # Multi-day requests intentionally remain eligible even when previously
        # recommended, but every acquired paper still reaches semantic approval.
        print(
            f"  Multi-day mode (days={days}): history does not affect selection",
            file=sys.stderr,
        )
        candidates = list(by_id.values())
        for paper in candidates:
            paper["daily_selection_eligible"] = True
        candidates.sort(key=lambda x: x["score"], reverse=True)
        top = candidates if top_n is None else candidates[:top_n]
        print(f"  Full metadata pool: {len(top)} papers", file=sys.stderr)
        return top

    # Single-day history is now a post-acquisition selection policy. Seen
    # papers remain in this full metadata pool and receive their own Markdown
    # and semantic evaluation.
    history = load_history()
    history_ids: dict[str, str] = {}  # id → earliest date
    for h in history:
        hid, hdate = h.get("id", ""), h.get("date", "")
        if hid and hdate:
            if hid not in history_ids or hdate < history_ids[hid]:
                history_ids[hid] = hdate

    if len(history) < 10:
        for fid in load_fallback_ids(target_date):
            history_ids.setdefault(fid, "unknown")

    eligible_count = 0
    seen_papers: list[dict] = []
    for aid, p in by_id.items():
        p["daily_selection_eligible"] = True
        if aid in history_ids:
            p["is_re_recommend"] = True
            p["last_recommend_date"] = history_ids[aid]
            trending_source = p.get("source") == "hf-trending" or (
                p.get("hf_source") == "hf-trending"
            )
            if not (
                is_weekend
                and trending_source
                and (p.get("hf_upvotes") or 0) >= 5
            ):
                p["daily_selection_eligible"] = False
                seen_papers.append(p)
                continue
        eligible_count += 1

    # Preserve the legacy thin-pool behaviour without omitting any paper from
    # approval: mark only the strongest seen papers as deterministic backfill.
    if eligible_count < 20 and seen_papers:
        seen_papers.sort(key=lambda paper: paper["score"], reverse=True)
        backfill = seen_papers[: 20 - eligible_count]
        for paper in backfill:
            paper["daily_selection_eligible"] = True
        if backfill:
            print(f"  Marked {len(backfill)} history papers as backfill", file=sys.stderr)

    candidates = list(by_id.values())
    candidates.sort(
        key=lambda paper: (
            not paper["daily_selection_eligible"],
            -paper["score"],
            paper["paper_id"],
        )
    )

    top = candidates if top_n is None else candidates[:top_n]
    print(
        f"  Full metadata pool: {len(top)} papers; "
        f"{sum(bool(p['daily_selection_eligible']) for p in top)} selection-eligible",
        file=sys.stderr,
    )
    return top


# ── Main ───────────────────────────────────────────────────────────────────


def resolve_target_date(date_value: str | None, timezone: str) -> date:
    if date_value:
        return datetime.strptime(date_value, "%Y-%m-%d").date()
    return datetime.now(ZoneInfo(timezone)).date()


def bounded_days(value: str) -> int:
    try:
        days = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("days must be an integer") from exc
    if not 1 <= days <= MAX_FETCH_DAYS:
        raise argparse.ArgumentTypeError(
            f"days must be from 1 to {MAX_FETCH_DAYS}"
        )
    return days


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime-context",
        required=True,
        type=Path,
        help="Coordinator-frozen runtime-context.json",
    )
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--days",
        type=bounded_days,
        default=1,
        help=f"Number of days to fetch, 1-{MAX_FETCH_DAYS} (default: 1)",
    )
    parser.add_argument("--output", type=Path, help="Write JSON to this file instead of stdout")
    parser.add_argument(
        "--summary",
        type=Path,
        help="Write the acquisition completeness summary to this file",
    )
    args = parser.parse_args()

    try:
        timezone = configure_runtime_context(args.runtime_context)
    except (OSError, RuntimeContextError) as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "code": "invalid-runtime-context",
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    target_date = resolve_target_date(args.date, timezone)
    days = args.days
    start_date = target_date - timedelta(days=days - 1)
    is_weekend = target_date.weekday() >= 5
    print(
        f"[fetch_and_score] {target_date} ({'weekend' if is_weekend else 'weekday'})"
        + (f", days={days} [{start_date} ~ {target_date}]" if days > 1 else ""),
        file=sys.stderr,
    )

    fetch_budget = HTTP_CLIENT.new_budget(
        max_total_bytes=MAX_TOTAL_FETCH_BYTES,
        request_timeout_seconds=FETCH_REQUEST_TIMEOUT_SECONDS,
        run_timeout_seconds=FETCH_RUN_TIMEOUT_SECONDS,
    )
    arxiv_snapshot: dict = {}
    try:
        arxiv_papers = fetch_arxiv_papers(
            start_date,
            target_date,
            days,
            client=HTTP_CLIENT,
            budget=fetch_budget,
            snapshot=arxiv_snapshot,
        )
    except ArxivFetchError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "code": "incomplete-arxiv-snapshot",
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    hf_papers = fetch_hf_papers(
        start_date,
        target_date,
        client=HTTP_CLIENT,
        budget=fetch_budget,
    )
    top = merge_and_dedup(
        hf_papers,
        arxiv_papers,
        target_date,
        days=days,
        top_n=None,
    )
    acquisition_summary = {
        "version": 1,
        "complete": True,
        "target_date": target_date.isoformat(),
        "window_days": days,
        "arxiv": arxiv_snapshot,
        "huggingface_count": len(hf_papers),
        "acquired_count": len(top),
        "selection_eligible_count": sum(
            bool(paper.get("daily_selection_eligible", True))
            for paper in top
        ),
    }

    try:
        output = encode_json_value(
            top,
            max_bytes=MAX_OUTPUT_BYTES,
            label="Fetch output",
        )
        acquisition_summary["acquired_sha256"] = hashlib.sha256(output).hexdigest()
        if args.output:
            atomic_write_bytes(
                args.output,
                output,
                mode=0o600,
                label="Fetch output",
            )
        else:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stdout.write(output.decode("utf-8"))
        if args.summary:
            summary_output = encode_json_value(
                acquisition_summary,
                max_bytes=MAX_OUTPUT_BYTES,
                label="Acquisition summary",
            )
            atomic_write_bytes(
                args.summary,
                summary_output,
                mode=0o600,
                label="Acquisition summary",
            )
    except SafeIOError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "code": "output-write-failed",
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
