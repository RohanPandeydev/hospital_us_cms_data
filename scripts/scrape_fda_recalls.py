#!/usr/bin/env python3
"""Scrape FDA "Medical Device Recalls and Early Alerts" detail pages.

Pipeline (per entry):
  1. FDA listing page: /medical-devices/medical-device-safety/medical-device-recalls-and-early-alerts
  2. FDA detail page:  /medical-devices/medical-device-recalls-and-early-alerts/{slug}
     Captures: heading, body text, event_id, CDRH/Enforcement URLs, plus the
     two link blocks ("Additional FDA Resources" + "Additional Company
     Resources"). Does NOT crawl into the CDRH database itself — those URLs
     are stored as-is so they can be opened on demand.

Output: downloads/fda_recalls/recalls.json

Usage:
  python scripts/scrape_fda_recalls.py --limit 5     # smoke-test
  python scripts/scrape_fda_recalls.py               # all 159
  python scripts/scrape_fda_recalls.py --resume      # skip entries already in JSON
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "downloads" / "fda_recalls"

LIST_URL = "https://www.fda.gov/medical-devices/medical-device-safety/medical-device-recalls-and-early-alerts"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
})

log = logging.getLogger("scrape_fda_recalls")


# ---------- HTTP ----------

def get(url: str, referer: Optional[str] = None, retries: int = 3) -> str:
    """GET with polite retries. FDA's abuse-detection redirect is treated as a
    transient block — back off and retry rather than failing fast."""
    headers = {}
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
    for attempt in range(retries):
        try:
            r = SESSION.get(url, headers=headers, timeout=30)
            if r.status_code == 200 and "abuse-detection-apology" not in r.url:
                return r.text
            if "abuse-detection-apology" in r.url:
                wait = 30 * (attempt + 1)
                log.warning("FDA abuse-detection block for %s, backing off %ds", url, wait)
                time.sleep(wait)
                continue
            if r.status_code in (429, 503):
                wait = 10 * (attempt + 1)
                log.warning("got %d for %s, sleeping %ds", r.status_code, url, wait)
                time.sleep(wait)
                continue
            log.warning("HTTP %d for %s (final: %s)", r.status_code, url, r.url)
        except requests.RequestException as e:
            log.warning("network error %s: %s (attempt %d/%d)", url, e, attempt + 1, retries)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"failed to GET {url} after {retries} retries")


# ---------- parsers ----------

def parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    out = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        a = cells[1].find("a", href=True)
        if not a:
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.fda.gov" + href
        out.append({
            "date": cells[0].get_text(" ", strip=True),
            "title": cells[1].get_text(" ", strip=True),
            "product_area": cells[2].get_text(" ", strip=True),
            "status": cells[3].get_text(" ", strip=True),
            "detail_url": href,
            "slug": href.rsplit("/", 1)[-1],
        })
    return out


def _abs(href: str) -> str:
    if href.startswith("/"):
        return "https://www.fda.gov" + href
    return href


# Boilerplate <h2> sections that appear on EVERY recall page — same wording
# every time, no per-recall info, so we drop them.
_GENERIC_SECTIONS = {
    "unique device identifier (udi)",
    "how do i report a problem?",
    "regulated product(s)",
}


# Letter-type classification. Each pattern is checked against the link label;
# first match wins, so order them most-specific → least-specific.
_LETTER_PATTERNS: list[tuple[str, str, str]] = [
    # (kind, source, regex)
    ("warning_letter",        "fda",  r"warning letter"),
    ("untitled_letter",       "fda",  r"untitled letter"),
    ("form_483",              "fda",  r"\bform\s*483\b|\b483\b"),
    ("inspection_letter",     "fda",  r"inspection (classification|letter)"),
    ("safety_communication",  "fda",  r"safety communication"),
    ("enforcement_report",    "fda",  r"enforcement report"),
    ("recall_database",       "fda",  r"recall database"),
    ("response_letter",       "firm", r"response letter|response to"),
    ("correction_letter",     "firm", r"correction letter|medical device correction"),
    ("recall_letter",         "firm", r"recall letter|removal letter"),
    ("field_safety_notice",   "firm", r"field safety (notification|notice)"),
    ("press_release",         "firm", r"press release|press release\b"),
    ("customer_notification", "firm", r"customer (notification|letter)|notification letter"),
    ("urgent_correction",     "firm", r"urgent (medical device )?correction"),
]


def _classify_letters(resources: list[dict], default_source: str) -> list[dict]:
    """Tag each resource link with its letter-type kind. `default_source` is
    used when a link doesn't match a source-specific pattern (e.g. the link
    sits under "Additional Company Resources" so it's a firm letter)."""
    out = []
    for r in resources or []:
        label = (r.get("label") or "").strip()
        url = r.get("url")
        if not label:
            continue
        kind = source = None
        for k, src, pat in _LETTER_PATTERNS:
            if re.search(pat, label, re.I):
                kind, source = k, src
                break
        if not kind:
            continue
        # Pull a date from "[MM/DD/YYYY]" if present in the label.
        m = re.search(r"\[(\d{2}/\d{2}/\d{4})\]", label)
        date = m.group(1) if m else None
        out.append({
            "kind": kind,
            "source": source or default_source,
            "label": label,
            "url": url,
            "date": date,
        })
    return out


def collect_letters(detail: dict) -> list[dict]:
    """Combine letter-type links from both resource blocks into one list."""
    letters = []
    letters += _classify_letters(detail.get("additional_fda_resources"), "fda")
    letters += _classify_letters(detail.get("additional_company_resources"), "firm")
    return letters


def _clean_links_block(node) -> list[dict]:
    """Collect href/label pairs from a <ul>/<ol>."""
    items = []
    for li in node.find_all("li", recursive=False):
        a = li.find("a", href=True)
        label = li.get_text(" ", strip=True)
        items.append({"label": label, "url": _abs(a["href"]) if a else None})
    return items


def _walk_section(hd):
    """Yield siblings between hd and the next h2/h3 of the same level."""
    sib = hd.find_next_sibling()
    while sib and sib.name not in ("h2",):
        yield sib
        sib = sib.find_next_sibling()


def parse_detail(html: str) -> dict:
    """Pull a structured view of the FDA detail page.

    Returns the headline, the per-section text blocks (one entry per <h2>),
    the FDA/Company resource link lists, and the CDRH/Enforcement URLs.
    Boilerplate sections (UDI explainer, how-to-report MedWatch blurb,
    Regulated Products) are dropped — they're identical on every page."""
    soup = BeautifulSoup(html, "html.parser")

    main = (soup.find(id=re.compile("main", re.I))
            or soup.find("article")
            or soup)

    # Headline = first h1 (skip only site nav; the article's own <header>
    # wraps the title, so we DON'T skip parent <header>).
    headline = None
    for tag in main.find_all(["h1"]):
        if tag.find_parent("nav"):
            continue
        headline = tag.get_text(" ", strip=True)
        break

    # CDRH + Enforcement URLs (also surfaced as part of Additional FDA Resources).
    event_id = None
    cdrh_url = enforcement_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "accessdata.fda.gov" in href and "cfRES" in href and not cdrh_url:
            cdrh_url = href
            m = re.search(r"event_id=(\d+)", href)
            if m:
                event_id = int(m.group(1))
        elif "accessdata.fda.gov" in href and "ires" in href.lower() and not enforcement_url:
            enforcement_url = href

    # Walk h2 sections inside the main content area.
    sections: dict[str, str] = {}
    additional_fda_resources: list[dict] = []
    additional_company_resources: list[dict] = []
    timeline: list[dict] = []
    content_current_as_of: str | None = None

    for hd in main.find_all("h2"):
        if hd.find_parent(["nav", "header"]):
            continue
        title = hd.get_text(" ", strip=True).strip()
        title_norm = title.rstrip(":").lower()

        if title_norm == "additional fda resources":
            for sib in _walk_section(hd):
                if sib.name in ("ul", "ol"):
                    additional_fda_resources.extend(_clean_links_block(sib))
            continue

        if title_norm == "additional company resources":
            for sib in _walk_section(hd):
                if sib.name in ("ul", "ol"):
                    additional_company_resources.extend(_clean_links_block(sib))
            continue

        if title_norm == "timeline of communication updates":
            # Usually rendered as a <table> with Date | Actions columns,
            # often wrapped in <div class="table-responsive">.
            for sib in _walk_section(hd):
                for tbl in sib.find_all("table") if sib.name != "table" else [sib]:
                    for tr in tbl.find_all("tr"):
                        cells = tr.find_all(["td", "th"])
                        if len(cells) >= 2:
                            timeline.append({
                                "date": cells[0].get_text(" ", strip=True),
                                "text": cells[1].get_text(" ", strip=True),
                            })
            if timeline and timeline[0]["date"].lower() == "date":
                timeline.pop(0)
            continue

        if title_norm == "content current as of":
            for sib in _walk_section(hd):
                txt = sib.get_text(" ", strip=True)
                if txt:
                    content_current_as_of = txt
                    break
            continue

        if title_norm in _GENERIC_SECTIONS:
            continue

        # Default: collect prose, structured label/value items, and any links.
        parts: list[str] = []
        items: list[dict] = []
        links: list[dict] = []
        for sib in _walk_section(hd):
            if sib.name not in ("p", "ul", "ol", "div", "table"):
                continue
            t = sib.get_text(" ", strip=True)
            if t:
                parts.append(t)
            # Pull <li><strong>Label:</strong> value</li> pairs and any <a> links.
            for li in sib.find_all("li"):
                strong = li.find("strong")
                a = li.find("a", href=True)
                if strong:
                    label = strong.get_text(" ", strip=True).rstrip(":").strip()
                    # Value = li text with the <strong> removed.
                    strong.extract()
                    value = li.get_text(" ", strip=True).lstrip(":").strip()
                    if label:
                        item = {"label": label, "value": value or None}
                        if a and a.get("href"):
                            item["url"] = _abs(a["href"])
                        items.append(item)
            for a in sib.find_all("a", href=True):
                links.append({
                    "text": a.get_text(" ", strip=True),
                    "url": _abs(a["href"]),
                })
        if parts or items or links:
            sections[title] = {
                "text": "\n\n".join(parts) if parts else None,
                "items": items,
                "links": links,
            }

    # Pull common product identifiers (Device Name, UDI, Model #, Catalog #,
    # Lot #, Product Code) out of the Affected Product section if present —
    # different recalls use different label phrasings, so we do a fuzzy match.
    product_identifiers: dict[str, str] = {}
    affected = sections.get("Affected Product") or {}
    for it in affected.get("items", []):
        lab = it["label"].lower()
        val = it.get("value")
        if not val:
            continue
        if "device name" in lab and "device_name" not in product_identifiers:
            product_identifiers["device_name"] = val
        elif "unique device identifier" in lab or lab == "udi":
            product_identifiers["udi"] = val
        elif "model" in lab:
            product_identifiers["model_number"] = val
        elif "catalog" in lab:
            product_identifiers["catalog_number"] = val
        elif "lot" in lab:
            product_identifiers["lot_numbers"] = val
        elif "product code" in lab:
            product_identifiers["product_code"] = val
        elif "510(k)" in lab or "510k" in lab:
            product_identifiers["k_number_510k"] = val
        elif "manufacturer" in lab:
            product_identifiers["manufacturer"] = val

    detail_out = {
        "headline": headline,
        "event_id": event_id,
        "cdrh_search_url": cdrh_url,
        "enforcement_report_url": enforcement_url,
        "product_identifiers": product_identifiers,
        "sections": sections,
        "additional_fda_resources": additional_fda_resources,
        "additional_company_resources": additional_company_resources,
        "timeline": timeline,
        "content_current_as_of": content_current_as_of,
    }
    detail_out["letters"] = collect_letters(detail_out)
    return detail_out


# ---------- driver ----------

def scrape_one(entry: dict, sleep_s: float) -> dict:
    """Fetch one FDA detail page and capture body + resource links."""
    log.info("[%s] %s", entry["date"], entry["title"][:80])
    detail_html = get(entry["detail_url"], referer=LIST_URL)
    detail = parse_detail(detail_html)
    log.info("  event_id=%s, FDA-resources=%d, company-resources=%d",
             detail.get("event_id"),
             len(detail.get("additional_fda_resources") or []),
             len(detail.get("additional_company_resources") or []))
    return {"fda_listing": entry, "fda_detail": detail}


def annotate_letters(out_path: Path) -> int:
    """Re-derive each record's `fda_detail.letters` from its already-stored
    resource lists. Pure local work — no HTTP, no re-fetch — so it's safe to
    run alongside an in-progress scrape and over older JSON snapshots."""
    data = json.loads(out_path.read_text())
    touched = 0
    for rec in data:
        fd = rec.get("fda_detail")
        if not fd:
            continue
        fd["letters"] = collect_letters(fd)
        touched += 1
    out_path.write_text(json.dumps(data, indent=2, default=str))
    return touched


def _clean_url(u: str) -> str:
    """Strip any whitespace/quote pollution that crept in from malformed
    HTML hrefs (we've seen `…?event_id=98598" \\t "_blank` from a stray
    target="_blank" attribute that bs4 mis-attributed)."""
    if not u:
        return u
    # Cut at the first whitespace or quote — both are illegal in valid URLs.
    return re.split(r"[\s\"']", u, maxsplit=1)[0]


def fetch_recall_numbers(cdrh_search_url: str) -> dict:
    """Fetch the CDRH search-results page and return:
       { recall_numbers: [Z-…], result_count: int, detail_ids: [int] }
    A single recall *event* often produces multiple Z-numbered records (one
    per affected product variant), so this is naturally a list."""
    html = get(_clean_url(cdrh_search_url), referer="https://www.fda.gov/")
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    recall_numbers = list(dict.fromkeys(re.findall(r"\bZ-\d+-\d{4}\b", text)))
    detail_ids: list[int] = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"res\.cfm\?id=(\d+)", a["href"])
        if m:
            detail_ids.append(int(m.group(1)))
    rc = re.search(r"(\d+)\s+result", text)
    return {
        "recall_numbers": recall_numbers,
        "result_count": int(rc.group(1)) if rc else len(recall_numbers),
        "cdrh_detail_ids": list(dict.fromkeys(detail_ids)),
    }


def annotate_recall_numbers(out_path: Path, sleep_s: float) -> int:
    """For each record with a `cdrh_search_url`, fetch the CDRH search page
    and store the Z-numbers (recall numbers) and per-product detail IDs.
    Skips records that already have `recall_numbers` so the call is idempotent."""
    data = json.loads(out_path.read_text())
    todo = [r for r in data
            if (r.get("fda_detail") or {}).get("cdrh_search_url")
            and not (r.get("fda_detail") or {}).get("recall_numbers")]
    log.info("Annotating recall numbers for %d records (skipping %d without "
             "event_id and any already-annotated)", len(todo), len(data) - len(todo))

    for n, rec in enumerate(todo, 1):
        fd = rec["fda_detail"]
        url = fd["cdrh_search_url"]
        try:
            info = fetch_recall_numbers(url)
            fd["recall_numbers"] = info["recall_numbers"]
            fd["cdrh_result_count"] = info["result_count"]
            fd["cdrh_detail_ids"] = info["cdrh_detail_ids"]
            log.info("[%d/%d] event_id=%s → %d Z-numbers: %s",
                     n, len(todo), fd.get("event_id"),
                     len(info["recall_numbers"]), info["recall_numbers"][:3])
        except Exception as e:
            log.error("[%d/%d] FAILED for event_id=%s: %s",
                      n, len(todo), fd.get("event_id"), e)
            fd["recall_numbers_error"] = f"{type(e).__name__}: {e}"
        # Save after every fetch so partial progress survives interruption.
        out_path.write_text(json.dumps(data, indent=2, default=str))
        time.sleep(sleep_s)

    return len(todo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="Only scrape this many entries (0 = all).")
    ap.add_argument("--sleep", type=float, default=2.5,
                    help="Seconds to wait between HTTP calls.")
    ap.add_argument("--resume", action="store_true",
                    help="Skip entries whose detail_url already exists in the output JSON.")
    ap.add_argument("--out", default=str(OUT_DIR / "recalls.json"),
                    help="Output JSON path.")
    ap.add_argument("--annotate-letters", action="store_true",
                    help="Don't scrape — just (re)derive the `letters` field on each "
                         "record in --out from its already-stored resource lists.")
    ap.add_argument("--annotate-recall-numbers", action="store_true",
                    help="For each record with a `cdrh_search_url`, fetch the CDRH "
                         "search page and store the Z-numbers (recall_numbers).")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.annotate_letters:
        n = annotate_letters(out_path)
        log.info("Annotated letters on %d records in %s", n, out_path)
        return
    if args.annotate_recall_numbers:
        n = annotate_recall_numbers(out_path, args.sleep)
        log.info("Annotated recall numbers on %d records in %s", n, out_path)
        return

    log.info("Fetching FDA recall listing")
    listing = parse_listing(get(LIST_URL))
    log.info("Found %d entries", len(listing))

    existing: list[dict] = []
    seen_urls: set[str] = set()
    if args.resume and out_path.exists():
        existing = json.loads(out_path.read_text())
        seen_urls = {r["fda_listing"]["detail_url"] for r in existing}
        log.info("Resuming: %d entries already in %s", len(existing), out_path)

    results = list(existing)
    todo = [e for e in listing if e["detail_url"] not in seen_urls]
    if args.limit:
        todo = todo[: args.limit]
    log.info("Will scrape %d entries", len(todo))

    for n, entry in enumerate(todo, 1):
        try:
            rec = scrape_one(entry, args.sleep)
        except Exception as e:
            log.error("FAILED for %s: %s", entry["detail_url"], e)
            rec = {"fda_listing": entry, "error": f"{type(e).__name__}: {e}"}
        results.append(rec)
        # Save after every entry so partial progress survives interruption.
        out_path.write_text(json.dumps(results, indent=2, default=str))
        log.info("  saved %d/%d → %s", n, len(todo), out_path)
        time.sleep(args.sleep)

    log.info("Done. %d records in %s", len(results), out_path)


if __name__ == "__main__":
    sys.exit(main())
