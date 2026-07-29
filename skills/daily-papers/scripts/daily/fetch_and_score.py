#!/usr/bin/env python3
"""
fetch_and_score.py — Phase 1+2: Fetch, score, merge, dedup, select top 30.

Replaces the LLM orchestration step with pure Python. Zero token cost.

Usage:
    python3 fetch_and_score.py --output /path/to/candidates.json
    python3 fetch_and_score.py --date 2026-02-25 --output /path/to/candidates.json
    python3 fetch_and_score.py --days 7 --output /path/to/candidates.json

Stderr: progress logs.  Stdout: JSON array of top papers (30 * days).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from itertools import islice
from pathlib import Path
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
ARXIV_RESULTS_PER_DAY = 400
MAX_ARXIV_RESULTS = 3_000
MAX_TOTAL_FETCH_BYTES = 96 * 1024 * 1024
FETCH_REQUEST_TIMEOUT_SECONDS = 60
FETCH_RUN_TIMEOUT_SECONDS = 10 * 60

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
HTTP_CLIENT = SafeHTTPClient()


class RuntimeContextError(ValueError):
    """The coordinator-frozen runtime context is missing or invalid."""


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

    # 1. Negative keywords → instant reject
    for neg in NEGATIVE_KEYWORDS:
        if neg in text:
            return -999

    score = 0

    # 2. Positive keywords
    keyword_hits = 0
    for kw in KEYWORDS:
        if kw in title_lower:
            score += 3
            keyword_hits += 1
        elif kw in text:
            score += 1
            keyword_hits += 1

    # 3. Domain boost
    domain_hits = sum(1 for kw in DOMAIN_BOOST_KEYWORDS if kw in text)
    if domain_hits >= 2:
        score += 2
    elif domain_hits == 1:
        score += 1

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

    if paper["score"] < 0:
        return None

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
) -> list[dict]:
    max_results = min(ARXIV_RESULTS_PER_DAY * days, MAX_ARXIV_RESULTS)
    cats = "+OR+".join(f"cat:{c}" for c in ARXIV_CATEGORIES)
    url = (
        f"https://export.arxiv.org/api/query?"
        f"search_query=({cats})"
        f"&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"
    )

    timeout = max(60, 30 * days)
    print(f"  Fetching arXiv (max_results={max_results}, timeout={timeout}s)...", file=sys.stderr)
    xml_text = fetch_url(
        url,
        timeout=timeout,
        client=client,
        budget=budget,
    )
    if not xml_text:
        return []

    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml_text, flags=re.IGNORECASE):
        print(
            "  [WARN] arXiv XML rejected a DTD or entity declaration",
            file=sys.stderr,
        )
        return []
    try:
        # Atom needs no DTD; declarations were rejected above before this
        # bounded standard-library parse.
        root = ET.fromstring(xml_text)  # noqa: S314
    except ET.ParseError as e:
        print(f"  [WARN] arXiv XML parse error: {e}", file=sys.stderr)
        return []

    entries = list(
        islice(
            root.iterfind("atom:entry", ATOM_NS),
            max_results + 1,
        )
    )
    if len(entries) > max_results:
        print(
            "  [WARN] arXiv response contains more entries than requested",
            file=sys.stderr,
        )
        return []

    papers = []
    filtered_by_date = 0
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
            continue

        title = " ".join(title_el.text.split())
        abstract = " ".join(summary_el.text.split())
        entry_url = (
            id_el.text.strip()
            if id_el is not None and id_el.text
            else ""
        )
        date = (
            published_el.text[:10]
            if published_el is not None and published_el.text
            else ""
        )
        arxiv_id = canonical_arxiv_id(entry_url) or ""
        if not arxiv_id:
            continue

        # Date filter: only apply in multi-day mode (days > 1)
        # In single-day mode, arXiv batches span 2-3 days, so filtering would be too strict
        if days > 1 and start_date and end_date and date:
            try:
                pub_date = datetime.strptime(date, "%Y-%m-%d").date()
                if pub_date < start_date or pub_date > end_date:
                    filtered_by_date += 1
                    continue
            except ValueError:
                pass  # keep papers with unparseable dates

        author_els = entry.findall("atom:author", ATOM_NS)
        names = []
        affiliations = set()
        for a in author_els:
            name_el = a.find("atom:name", ATOM_NS)
            if name_el is not None and name_el.text:
                names.append(name_el.text.strip())
            for aff_el in a.findall("arxiv:affiliation", ATOM_NS):
                if aff_el.text and aff_el.text.strip():
                    affiliations.add(aff_el.text.strip())

        cat_el = entry.find("arxiv:primary_category", ATOM_NS)
        category = cat_el.get("term", "") if cat_el is not None else ""

        papers.append({
            "paper_id": f"arxiv:{arxiv_id}",
            "arxiv_id": arxiv_id,
            "title": title,
            "authors": ", ".join(names),
            "affiliations": ", ".join(sorted(affiliations)) if affiliations else "",
            "abstract": abstract,
            "url": entry_url,
            "pdf": f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "",
            "date": date,
            "score": 0,
            "category": category,
            "source": "arxiv",
        })

    scored = []
    for p in papers:
        p["score"] = score_paper(p)
        if p["score"] >= 0:
            scored.append(p)

    print(
        f"  arXiv: {len(scored)} papers after scoring (from {len(papers)} parsed, {filtered_by_date} filtered by date)",
        file=sys.stderr,
    )
    return scored


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
    top_n: int = TOP_N,
) -> list[dict]:
    is_weekend = target_date.weekday() >= 5

    # ── merge by arXiv ID, keep higher score ──
    by_id: dict[str, dict] = {}
    for p in hf_papers + arxiv_papers:
        aid = extract_arxiv_id(p["url"])
        if not aid:
            continue
        if aid not in by_id or p["score"] > by_id[aid]["score"]:
            by_id[aid] = p

    print(f"  Merged: {len(by_id)} unique papers", file=sys.stderr)

    if days > 1:
        # ── multi-day mode: skip history dedup ──
        # User explicitly wants to see all N days, don't filter out previously recommended
        print(f"  Multi-day mode (days={days}): skipping history dedup", file=sys.stderr)
        candidates = [p for p in by_id.values() if p["score"] >= MIN_SCORE]
        candidates.sort(key=lambda x: x["score"], reverse=True)
        top = candidates[:top_n]
        print(f"  Final: {len(top)} papers (top_n={top_n})", file=sys.stderr)
        return top

    # ── single-day mode: history dedup as before ──
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

    # ── cross-day dedup ──
    deduped: dict[str, dict] = {}
    removed = 0
    for aid, p in by_id.items():
        if aid in history_ids:
            # Weekend: keep trending with upvotes >= 5
            if is_weekend and p.get("source") == "hf-trending" and (p.get("hf_upvotes") or 0) >= 5:
                p["is_re_recommend"] = True
                p["last_recommend_date"] = history_ids[aid]
                deduped[aid] = p
            else:
                removed += 1
        else:
            deduped[aid] = p

    # Mark any remaining that appear in history
    for aid, p in deduped.items():
        if aid in history_ids and not p.get("is_re_recommend"):
            p["is_re_recommend"] = True
            p["last_recommend_date"] = history_ids[aid]

    print(f"  After history dedup: {len(deduped)} (removed {removed})", file=sys.stderr)

    # ── filter + sort ──
    candidates = [p for p in deduped.values() if p["score"] >= MIN_SCORE]
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Back-fill from history if pool is thin
    if len(candidates) < 20 and removed > 0:
        backfill = []
        for aid, p in by_id.items():
            if aid not in deduped and p["score"] >= MIN_SCORE:
                p["is_re_recommend"] = True
                p["last_recommend_date"] = history_ids.get(aid, "unknown")
                backfill.append(p)
        backfill.sort(key=lambda x: x["score"], reverse=True)
        needed = 20 - len(candidates)
        candidates.extend(backfill[:needed])
        if backfill[:needed]:
            print(f"  Back-filled {min(needed, len(backfill))} from history", file=sys.stderr)

    top = candidates[:top_n]
    print(f"  Final: {len(top)} papers", file=sys.stderr)
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
    top_n = TOP_N * days

    is_weekend = target_date.weekday() >= 5
    print(
        f"[fetch_and_score] {target_date} ({'weekend' if is_weekend else 'weekday'})"
        + (f", days={days} [{start_date} ~ {target_date}], top_n={top_n}" if days > 1 else ""),
        file=sys.stderr,
    )

    fetch_budget = HTTP_CLIENT.new_budget(
        max_total_bytes=MAX_TOTAL_FETCH_BYTES,
        request_timeout_seconds=FETCH_REQUEST_TIMEOUT_SECONDS,
        run_timeout_seconds=FETCH_RUN_TIMEOUT_SECONDS,
    )
    hf_papers = fetch_hf_papers(
        start_date,
        target_date,
        client=HTTP_CLIENT,
        budget=fetch_budget,
    )
    arxiv_papers = fetch_arxiv_papers(
        start_date,
        target_date,
        days,
        client=HTTP_CLIENT,
        budget=fetch_budget,
    )
    top = merge_and_dedup(hf_papers, arxiv_papers, target_date, days=days, top_n=top_n)

    try:
        output = encode_json_value(
            top,
            max_bytes=MAX_OUTPUT_BYTES,
            label="Fetch output",
        )
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
