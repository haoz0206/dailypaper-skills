#!/usr/bin/env python3
"""Batch-enrich arXiv papers with metadata from HTML/abs pages.

Usage:
    python3 enrich_papers.py --input /path/to/candidates.json --output /path/to/enriched.json

    # Backward-compatible positional form
    python3 enrich_papers.py input.json output.json

    # Stdin/stdout form
    cat input.json | python3 enrich_papers.py

Input:  JSON array via stdin or auto-detected file
Output: JSON array via stdout or file with enriched fields added

Architecture:
    - asyncio + one shared bounded HTTP client
    - Semaphore(10) to avoid hammering arXiv
    - Pure regex HTML parsing (no host-specific web tool / no external Python deps)
    - A bounded argument-vector subprocess only for local pdftotext
    - Time/byte limits at every remote and tool boundary
"""
from __future__ import annotations

import asyncio
import argparse
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))
_DAILY_DIR = Path(__file__).resolve().parent
if str(_DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(_DAILY_DIR))

from safe_io import (
    SafeIOError,
    atomic_write_bytes,
    encode_json_value,
    parse_json_value,
    read_regular_bytes,
)
from safe_http import (
    FetchBudget,
    ResponseTooLargeError,
    SafeHTTPClient,
    SafeHTTPError,
)
from safe_process import SafeProcessError, run_bounded_tool

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_INPUT_PAPERS = 10_000
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_PDF_TEXT_BYTES = 4 * 1024 * 1024
MAX_TOOL_LOG_BYTES = 64 * 1024
MAX_TOTAL_ENRICH_BYTES = 512 * 1024 * 1024
ENRICH_RUN_TIMEOUT_SECONDS = 30 * 60

from paper_identity import canonical_arxiv_id
from extract_affiliations import extract_affiliations

SEMAPHORE_LIMIT = 10
HTTP_TIMEOUT = 30
HTTP_CLIENT = SafeHTTPClient()

# ── Stop words for method_names extraction ──────────────────────────────────
METHOD_STOP = {
    # Section headings
    "Abstract", "Introduction", "Method", "Methods", "Methodology",
    "Results", "Conclusion", "Conclusions", "Discussion", "Experiments",
    "Experiment", "Evaluation", "Background", "Appendix", "Supplementary",
    "References", "Related", "Overview", "Preliminaries", "Framework",
    "Acknowledgements", "Acknowledgments",
    # Conferences / venues
    "CVPR", "ICCV", "ECCV", "NeurIPS", "ICML", "ICLR", "IEEE", "AAAI",
    "IJCAI", "SIGCHI", "SIGGRAPH", "ICRA", "IROS", "CoRL", "RSS",
    "WACV", "BMVC", "ACCV", "MICCAI", "ACL", "EMNLP", "NAACL",
    # Common abbreviations (not method names)
    "RGB", "GPU", "CPU", "TPU", "CNN", "MLP", "SGD", "ADAM", "GAN",
    "RNN", "LSTM", "GRU", "API", "URL", "HTML", "PDF", "JSON", "XML",
    "FPS", "IoU", "MAP", "FID", "PSNR", "SSIM", "LPIPS", "MSE", "MAE",
    "BCE", "CE", "KL", "GNN", "VAE", "ELBO", "EM",
    "SoTA", "SOTA", "TODO", "NOTE", "TBD",
    # Generic terms
    "Table", "Figure", "Section", "Eq", "Equation", "Algorithm",
    "Step", "Phase", "Stage", "Layer", "Block", "Module", "Head",
    "Loss", "Input", "Output", "Data", "Model", "Network",
    "Training", "Testing", "Inference", "Baseline", "Ablation",
    # Roman numerals
    "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII",
    # Common LaTeX / HTML artifacts
    "LaTeX", "BibTeX", "ArXiv",
}

# ── Real-world experiment keywords ──────────────────────────────────────────
REAL_WORLD_KEYWORDS = [
    "real robot", "real-world experiment", "physical robot",
    "real world evaluation", "hardware experiment", "deployed on",
    "real-world deployment", "real manipulation", "physical experiment",
    "real-world result", "real-world task", "real-world environment",
]

# ── Institution keywords for HTML affiliation extraction ────────────────────
INST_KEYWORDS = [
    "university", "universite", "università", "universität",
    "institute", "laboratory", "college", "school of",
    "center for", "centre for", "academy", "polytechnic",
    "department of", "faculty of", "research center", "research centre",
    "national lab",
    "google", "nvidia", "meta ai", "meta platforms", "microsoft",
    "deepmind", "openai", "alibaba", "tencent", "baidu", "bytedance",
    "amazon", "apple", "samsung", "huawei", "intel", "qualcomm",
    "adobe", "salesforce", "ibm research", "uber", "waymo", "toyota",
    "sony", "bosch", "damo academy",
    "mit ", "csail", "stanford", "berkeley", "cmu", "caltech",
    "eth zurich", "eth zürich", "epfl", "kaist", "inria", "mpi ",
    "fair ", "max planck", "cnrs",
    "tsinghua", "peking", "westlake", "hkust", "hku ", "fudan",
    "sjtu", "zju", "nju", "ustc", "cuhk", "shanghaitech",
    "chinese academy", "shanghai ai", "nanjing university",
    "nankai", "south china",
]


# ══════════════════════════════════════════════════════════════════════════════
# Remote and tool helpers
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_text(
    url: str,
    sem: asyncio.Semaphore,
    timeout: int = HTTP_TIMEOUT,
    retries: int = 3,
    *,
    client: SafeHTTPClient | None = None,
    budget: FetchBudget | None = None,
) -> str:
    """Fetch one bounded arXiv HTML page through the shared HTTP seam."""
    active_client = client or HTTP_CLIENT
    active_budget = budget or active_client.new_budget(
        max_total_bytes=MAX_HTML_BYTES,
        request_timeout_seconds=timeout,
        run_timeout_seconds=timeout + 5,
    )
    for attempt in range(1, retries + 1):
        async with sem:
            try:
                response = await asyncio.to_thread(
                    active_client.fetch_bytes,
                    url,
                    max_bytes=MAX_HTML_BYTES,
                    budget=active_budget,
                    accept="text/html, application/xhtml+xml",
                    allowed_media_types={
                        "text/html",
                        "application/xhtml+xml",
                    },
                )
                return response.body.decode("utf-8", errors="replace")
            except (SafeHTTPError, OSError) as e:
                print(
                    f"  [http] attempt {attempt}/{retries} failed {url}: {e}",
                    file=sys.stderr,
                )
                if isinstance(e, ResponseTooLargeError):
                    return ""
                try:
                    active_budget.remaining_seconds()
                except SafeHTTPError:
                    return ""
        if attempt < retries:
            await asyncio.sleep(3 * attempt)  # 3s, 6s
    return ""



# ══════════════════════════════════════════════════════════════════════════════
# HTML regex extractors
# ══════════════════════════════════════════════════════════════════════════════

def strip_tags(html: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", html)


def extract_figure_url(html: str, arxiv_id: str) -> str:
    """Extract the first non-icon figure image URL from HTML."""
    figures = re.findall(r"<figure[^>]*>.*?<img[^>]+src=[\"']([^\"'>]+)[\"']", html, re.DOTALL)
    skip_words = ["icon", "logo", "badge", "inline", "orcid", "creative"]
    for fig in figures:
        if any(skip in fig.lower() for skip in skip_words):
            continue
        url = fig
        if url.startswith("/"):
            url = "https://arxiv.org" + url
        elif not url.startswith("http"):
            if re.match(r"\d{4}\.\d{4,5}v\d+/", url):
                url = "https://arxiv.org/html/" + url
            else:
                url = f"https://arxiv.org/html/{arxiv_id}/" + url
        return url
    return ""


def extract_authors_html(html: str) -> list[str]:
    """Extract authors from ltx_personname spans."""
    matches = re.findall(r'class="ltx_personname"[^>]*>(.*?)</span>', html, re.DOTALL)
    authors = []
    for m in matches:
        name = strip_tags(m).strip()
        # Skip if it looks like an affiliation or footnote
        if name and len(name) < 80 and not any(kw in name.lower() for kw in ["university", "institute", "department"]):
            authors.append(name)
    return authors


def extract_affiliations_html(html: str) -> list[str]:
    """Extract affiliations from HTML paper using multiple strategies."""
    affils = set()

    # Strategy 1: structured class elements (ltx_role_affil, ltx_contact)
    # Search up to abstract or first 80k chars (some pages have long headers)
    abstract_pos = html.find("ltx_abstract")
    search_end = abstract_pos if abstract_pos > 0 else min(len(html), 80000)
    search_region = html[:search_end]
    for cls in ("ltx_role_affil", "ltx_contact"):
        for m in re.finditer(
            rf'class="[^"]*{cls}[^"]*"[^>]*>(.*?)</(?:span|div|p|td)',
            search_region, re.DOTALL
        ):
            text = strip_tags(m.group(1)).strip(" ,;.")
            if text and 3 < len(text) < 500:
                affils.add(text)

    # Strategy 2: header region plain text (between <article> and ltx_abstract)
    article_start = html.find("<article")
    abstract_start = html.find("ltx_abstract")
    if article_start >= 0 and abstract_start > article_start:
        header_text = strip_tags(html[article_start:abstract_start])
        for line in header_text.split("\n"):
            line = line.strip()
            if not line or len(line) < 5 or len(line) > 500:
                continue
            if any(kw in line.lower() for kw in INST_KEYWORDS):
                affils.add(line.strip(" ,;."))

    return list(affils)


def extract_section_headers(html: str) -> list[str]:
    """Extract h2/h3 section headers."""
    headers = []
    for m in re.finditer(r"<h[23][^>]*>(.*?)</h[23]>", html, re.DOTALL):
        text = strip_tags(m.group(1)).strip()
        text = re.sub(r"^\d+(\.\d+)*\.?\s*", "", text)  # remove "1.2.3 " prefix
        if text and len(text) < 200:
            headers.append(text)
    return headers[:25]


def extract_captions(html: str) -> list[str]:
    """Extract figure/table captions of reasonable length."""
    captions = []
    for m in re.finditer(r"<(?:figcaption|caption)[^>]*>(.*?)</(?:figcaption|caption)>", html, re.DOTALL):
        text = strip_tags(m.group(1)).strip()
        text = re.sub(r"\s+", " ", text)
        if 10 <= len(text) <= 200:
            captions.append(text)
    return captions[:8]


def extract_has_real_world(html: str) -> bool:
    """Check if HTML contains real-world experiment keywords."""
    html_lower = html.lower()
    return any(kw in html_lower for kw in REAL_WORLD_KEYWORDS)


def extract_method_names(html: str, paper_title: str) -> list[str]:
    """Extract method/model names from HTML text using CamelCase + ALLCAPS patterns."""
    text = strip_tags(html)

    # CamelCase: DreamerV3, OpenVLA, ControlNet, MuJoCo
    camel = re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]*)+(?:V?\d+)?)\b", text)
    # ALLCAPS with optional version: DDPM, SAM-2, GPT-4, RT-2
    allcaps = re.findall(r"\b([A-Z]{2,}(?:[-_]\d+)?)\b", text)
    # CamelCase with numbers: GPT4o, Llama3
    camel_num = re.findall(r"\b([A-Z][a-z]+[A-Z][a-z]*\d+[a-z]?)\b", text)
    # Hyphenated: Diffusion-Policy, Stable-Diffusion
    hyphenated = re.findall(r"\b([A-Z][a-z]+-[A-Z][a-z]+(?:-[A-Z][a-z]+)?)\b", text)

    all_names = camel + allcaps + camel_num + hyphenated
    cnt = Counter(all_names)

    # Build stop set including title words
    title_words = set(re.findall(r"\b[A-Za-z]+\b", paper_title))
    stop = METHOD_STOP | {w for w in title_words if len(w) >= 3}

    method_names = []
    seen = set()
    for name, count in cnt.most_common(40):
        if count < 2:
            continue
        if name in stop:
            continue
        if len(name) < 2:
            continue
        name_lower = name.lower()
        if name_lower in seen:
            continue
        seen.add(name_lower)
        method_names.append(name)
        if len(method_names) >= 20:
            break

    return method_names


def extract_method_summary(html: str) -> str:
    """Extract method description from Method/Approach sections (300-500 chars)."""
    # Strategy: find h2/h3 headers containing Method/Approach/Framework/Proposed,
    # then extract text until the next h2/h3.
    # Note: headers may contain inner tags like <span>, so we use .*? not [^<]*
    section_text = ""

    # Primary: find content after Method/Approach header until next header
    m = re.search(
        r"<h[23][^>]*>.*?(?:Method|Approach|Framework|Proposed).*?</h[23]>(.*?)(?:<h[23]|$)",
        html, re.DOTALL | re.IGNORECASE
    )
    if m:
        section_text = strip_tags(m.group(1))

    if not section_text:
        # Last resort: try Introduction's last paragraphs
        m = re.search(
            r"<h[23][^>]*>.*?Introduction.*?</h[23]>(.*?)(?:<h[23]|$)",
            html, re.DOTALL | re.IGNORECASE
        )
        if m:
            intro_text = strip_tags(m.group(1))
            paragraphs = [p.strip() for p in intro_text.split("\n\n") if p.strip()]
            # Take last 2 paragraphs (usually contain method overview)
            section_text = "\n".join(paragraphs[-2:]) if paragraphs else ""

    if not section_text:
        return ""

    # Clean up
    section_text = re.sub(r"\s+", " ", section_text).strip()
    # Remove citation markers like [1], [2,3]
    section_text = re.sub(r"\s*\[\d+(?:,\s*\d+)*\]", "", section_text)

    # Truncate to ~300-500 chars at sentence boundary
    if len(section_text) > 500:
        # Find sentence end near 500 chars
        end = section_text.rfind(". ", 300, 550)
        if end > 0:
            section_text = section_text[:end + 1]
        else:
            section_text = section_text[:500].rsplit(" ", 1)[0] + "..."

    return section_text if len(section_text) >= 100 else ""


# ══════════════════════════════════════════════════════════════════════════════
# Abs page fallback extractor
# ══════════════════════════════════════════════════════════════════════════════

def extract_from_abs(html: str) -> dict:
    """Extract authors and affiliations from arxiv abs page meta tags."""
    authors = re.findall(r'<meta\s+name="citation_author"\s+content="([^"]+)"', html)
    authors = [a.strip() for a in authors if a.strip()]
    affils = set()
    for m in re.findall(r'<meta\s+name="citation_author_institution"\s+content="([^"]+)"', html):
        if m.strip():
            affils.add(m.strip())
    return {"authors": authors, "affiliations": list(affils)}



# ══════════════════════════════════════════════════════════════════════════════
# PDF affiliation extraction
# ══════════════════════════════════════════════════════════════════════════════

async def extract_affiliations_pdf(arxiv_id: str, sem: asyncio.Semaphore,
                                   retries: int = 3,
                                   *,
                                   client: SafeHTTPClient | None = None,
                                   budget: FetchBudget | None = None) -> list[str]:
    """Extract affiliations through bounded HTTP and pdftotext boundaries."""
    canonical = canonical_arxiv_id(arxiv_id)
    if canonical is None:
        return []
    active_client = client or HTTP_CLIENT
    active_budget = budget or active_client.new_budget(
        max_total_bytes=MAX_PDF_BYTES,
        request_timeout_seconds=HTTP_TIMEOUT,
        run_timeout_seconds=HTTP_TIMEOUT + 5,
    )
    for attempt in range(1, retries + 1):
        async with sem:
            try:
                with tempfile.TemporaryDirectory(
                    prefix="dailypaper-affiliations-",
                    dir="/tmp",
                ) as temporary:
                    pdf_path = Path(temporary) / "paper.pdf"
                    await asyncio.to_thread(
                        active_client.fetch_file,
                        f"https://arxiv.org/pdf/{canonical}",
                        pdf_path,
                        max_bytes=MAX_PDF_BYTES,
                        budget=active_budget,
                        accept="application/pdf",
                        allowed_media_types={"application/pdf"},
                    )
                    result = await asyncio.to_thread(
                        run_bounded_tool,
                        [
                            "pdftotext",
                            "-l",
                            "2",
                            str(pdf_path),
                            "-",
                        ],
                        timeout=15,
                        max_stdout_bytes=MAX_PDF_TEXT_BYTES,
                        max_stderr_bytes=MAX_TOOL_LOG_BYTES,
                    )
                    if result.returncode == 0 and result.stdout:
                        affiliations = extract_affiliations(
                            result.stdout.decode("utf-8", errors="replace")
                        )
                        if affiliations:
                            return affiliations
            except (
                SafeProcessError,
                SafeHTTPError,
                OSError,
            ) as e:
                print(f"  [pdf] attempt {attempt}/{retries} failed {canonical}: {e}", file=sys.stderr)
                if isinstance(e, ResponseTooLargeError):
                    return []
                try:
                    active_budget.remaining_seconds()
                except SafeHTTPError:
                    return []
        if attempt < retries:
            await asyncio.sleep(3 * attempt)
    return []


# ══════════════════════════════════════════════════════════════════════════════
# Per-paper enrichment
# ══════════════════════════════════════════════════════════════════════════════

async def enrich_one(
    paper: dict,
    sem: asyncio.Semaphore,
    *,
    client: SafeHTTPClient | None = None,
    budget: FetchBudget | None = None,
) -> dict:
    """Enrich a single paper with metadata from HTML and abs pages."""
    arxiv_id = canonical_arxiv_id(paper.get("arxiv_id", ""))
    if not arxiv_id:
        arxiv_id = canonical_arxiv_id(paper.get("url", ""))
    if not arxiv_id:
        return paper

    title = paper.get("title", "")
    result = dict(paper)  # copy
    result["paper_id"] = f"arxiv:{arxiv_id}"
    result["arxiv_id"] = arxiv_id

    try:
        # Fetch HTML page
        html_url = f"https://arxiv.org/html/{arxiv_id}"
        html = await fetch_text(
            html_url,
            sem,
            client=client,
            budget=budget,
        )

        # Parse HTML if we got content
        html_authors = []
        html_affiliations = []
        figure_url = ""
        section_headers = []
        captions = []
        has_real_world = False
        method_names = []
        method_summary = ""

        if html and len(html) > 1000:
            figure_url = extract_figure_url(html, arxiv_id)
            html_authors = extract_authors_html(html)
            html_affiliations = extract_affiliations_html(html)
            section_headers = extract_section_headers(html)
            captions = extract_captions(html)
            has_real_world = extract_has_real_world(html)
            method_names = extract_method_names(html, title)
            method_summary = extract_method_summary(html)

        # Abs fallback if HTML authors OR affiliations are empty
        abs_authors = []
        abs_affiliations = []
        if not html_authors or not html_affiliations:
            abs_url = f"https://arxiv.org/abs/{arxiv_id}"
            abs_html = await fetch_text(
                abs_url,
                sem,
                client=client,
                budget=budget,
            )
            if abs_html:
                abs_data = extract_from_abs(abs_html)
                abs_authors = abs_data["authors"]
                abs_affiliations = abs_data["affiliations"]

        # PDF fallback for affiliations if still empty
        pdf_affiliations = []
        if not html_affiliations and not abs_affiliations:
            pdf_affiliations = await extract_affiliations_pdf(
                arxiv_id,
                sem,
                client=client,
                budget=budget,
            )

        # ── Merge with priority rules ──
        # Principle: new extraction > existing input, but never overwrite non-empty with empty

        # figure_url: HTML fetch > keep existing
        result["figure_url"] = figure_url or paper.get("figure_url", "")

        # affiliations: HTML > abs fallback > PDF fallback > keep existing input
        if html_affiliations:
            result["affiliations"] = ", ".join(html_affiliations)
        elif abs_affiliations:
            result["affiliations"] = ", ".join(abs_affiliations)
        elif pdf_affiliations:
            result["affiliations"] = ", ".join(pdf_affiliations)
        # else: keep whatever was in the input (supports re-enriching enriched data)

        # authors: HTML > abs fallback > keep existing input
        if html_authors:
            result["authors"] = ", ".join(html_authors)
        elif abs_authors:
            result["authors"] = ", ".join(abs_authors)
        # else: keep original

        # Other enriched fields
        result["section_headers"] = section_headers
        result["captions"] = captions
        result["has_real_world"] = has_real_world
        result["method_names"] = method_names
        result["method_summary"] = method_summary

    except Exception as e:
        print(f"  [error] {arxiv_id}: {e}", file=sys.stderr)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

async def enrich_all(
    papers: list[dict],
    *,
    client: SafeHTTPClient | None = None,
    budget: FetchBudget | None = None,
) -> list[dict]:
    """Enrich papers in bounded task batches while preserving input order."""
    active_client = client or HTTP_CLIENT
    active_budget = budget or active_client.new_budget(
        max_total_bytes=MAX_TOTAL_ENRICH_BYTES,
        request_timeout_seconds=HTTP_TIMEOUT,
        run_timeout_seconds=ENRICH_RUN_TIMEOUT_SECONDS,
    )
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    ordered: list[dict] = []
    for offset in range(0, len(papers), SEMAPHORE_LIMIT):
        try:
            active_budget.remaining_seconds()
            active_budget.ensure_available(1)
        except SafeHTTPError as exc:
            print(
                f"  [http] enrichment budget exhausted: {exc}",
                file=sys.stderr,
            )
            ordered.extend(papers[offset:])
            break
        batch = papers[offset : offset + SEMAPHORE_LIMIT]
        raw_results = await asyncio.gather(
            *(
                enrich_one(
                    paper,
                    sem,
                    client=active_client,
                    budget=active_budget,
                )
                for paper in batch
            ),
            return_exceptions=True,
        )
        for batch_index, result in enumerate(raw_results):
            index = offset + batch_index
            if isinstance(result, Exception):
                print(
                    f"  [error] paper #{index} "
                    f"({papers[index].get('arxiv_id','')}): {result}",
                    file=sys.stderr,
                )
                ordered.append(papers[index])
            else:
                ordered.append(result)

    return ordered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="Read candidate JSON from this file")
    parser.add_argument("--output", type=Path, help="Write enriched JSON to this file")
    parser.add_argument(
        "legacy_paths",
        nargs="*",
        type=Path,
        help="Deprecated positional input/output paths kept for compatibility",
    )
    args = parser.parse_args()
    if len(args.legacy_paths) > 2:
        parser.error("at most two positional paths are supported")

    input_path = args.input or (
        args.legacy_paths[0] if args.legacy_paths else None
    )
    output_path = args.output or (
        args.legacy_paths[1] if len(args.legacy_paths) == 2 else None
    )

    try:
        if input_path:
            raw = read_regular_bytes(
                input_path,
                max_bytes=MAX_INPUT_BYTES,
                label="Enrichment input",
            )
            if raw is None:
                raise SafeIOError(
                    f"Enrichment input file does not exist: {input_path}"
                )
        else:
            buffer = getattr(sys.stdin, "buffer", None)
            if buffer is not None:
                raw = buffer.read(MAX_INPUT_BYTES + 1)
            else:
                raw = sys.stdin.read(MAX_INPUT_BYTES + 1).encode("utf-8")
            if len(raw) > MAX_INPUT_BYTES:
                raise SafeIOError(
                    f"Enrichment input exceeds the {MAX_INPUT_BYTES}-byte safety limit"
                )
    except SafeIOError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        _write_output([], output_path)
        sys.exit(1)
    if not raw.strip():
        _write_output([], output_path)
        return

    try:
        papers = parse_json_value(
            raw,
            max_bytes=MAX_INPUT_BYTES,
            label="Enrichment input",
        )
    except SafeIOError as e:
        print(f"JSON parse error: {e}", file=sys.stderr)
        _write_output([], output_path)
        sys.exit(1)

    if not isinstance(papers, list):
        print("JSON parse error: input must be an array of objects", file=sys.stderr)
        _write_output([], output_path)
        sys.exit(1)
    if len(papers) > MAX_INPUT_PAPERS:
        print(
            f"Input error: paper count exceeds the {MAX_INPUT_PAPERS}-item limit",
            file=sys.stderr,
        )
        _write_output([], output_path)
        sys.exit(1)
    if any(not isinstance(paper, dict) for paper in papers):
        print("JSON parse error: input must be an array of objects", file=sys.stderr)
        _write_output([], output_path)
        sys.exit(1)

    if not papers:
        _write_output([], output_path)
        return

    print(f"Enriching {len(papers)} papers...", file=sys.stderr)
    enriched = asyncio.run(enrich_all(papers))
    print(f"Done. Enriched {len(enriched)} papers.", file=sys.stderr)

    _write_output(enriched, output_path)


def _write_output(value: object, output_path: Path | None) -> None:
    """Encode one bounded artifact and write it atomically or to stdout."""
    try:
        encoded = encode_json_value(
            value,
            max_bytes=MAX_OUTPUT_BYTES,
            label="Enrichment output",
        )
        if output_path:
            atomic_write_bytes(
                output_path,
                encoded,
                mode=0o600,
                label="Enrichment output",
            )
            return
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.write(encoded.decode("utf-8"))
        sys.stdout.flush()
    except SafeIOError as exc:
        print(f"Output error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
