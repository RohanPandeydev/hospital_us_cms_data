"""Stark Law DHS (Designated Health Services) CPT/HCPCS list ingester.

Source: cms.gov/medicare/regulations-guidance/physician-self-referral
        /list-cpt-hcpcs-codes

Each year CMS publishes a ZIP containing both the XLSX and a TXT of the
DHS code list. The page links to ZIPs through an AMA-license redirect; we
satisfy it with a POST `agree=yes`. The TXT inside is easy to parse:
section headers in ALL CAPS, then `<5-char code>\\t<short description>`.

A code can appear under more than one DHS category (e.g. CT scans count as
both "Radiology" and "Inpatient/Outpatient Hospital Services"), so the
natural key is (hcpcs_code, dhs_category, effective_year).
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Iterable

import requests

from . import db

log = logging.getLogger(__name__)

LANDING_URL = (
    "https://www.cms.gov/medicare/regulations-guidance/"
    "physician-self-referral/list-cpt-hcpcs-codes"
)
DOWNLOAD_BASE = "https://www.cms.gov"

# Section headers in the Stark DHS TXT are uppercase, ≥10 chars, contain no
# lowercase, and don't start with INCLUDE/EXCLUDE/CPT-prose. We accept any
# line matching that shape rather than hardcoding the category list because
# the file varies year to year (e.g. 2026 has "EPO AND OTHER DIALYSIS-RELATED
# DRUGS" and "PREVENTIVE SCREENING TESTS AND VACCINES" that older years don't).
_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9 ,/&()\.\-:]+$")
_CODE_RE   = re.compile(r"^([A-Z0-9]{5})\s+(.+)$")
# The first non-blank line in every file is the document title; skip it.
_TITLE_PREFIX = "LIST OF CPT"


def list_available_zips() -> list[tuple[str, int]]:
    """Return [(zip_path, effective_year), ...] sorted newest-first.

    Scrapes the landing page for href=*.zip links that match
    'list-codes-effective-january-1-YYYY'.
    """
    r = requests.get(LANDING_URL, timeout=60,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    hits = re.findall(r'href="([^"]*list-codes-effective[^"]*\.zip)"', r.text, re.I)
    out: list[tuple[str, int]] = []
    for h in hits:
        m = re.search(r"effective-january-1-(\d{4})", h)
        if not m:
            continue
        year = int(m.group(1))
        # The href goes through /apps/ama/license.asp?file=/files/zip/...zip
        # The actual download path is the `file=` query value.
        m2 = re.search(r"file=(/files/zip/[^&\"\s]+\.zip)", h)
        if m2:
            zip_path = m2.group(1)
        elif h.startswith("/files/zip/"):
            zip_path = h
        else:
            continue
        out.append((zip_path, year))
    # Dedup by year, keep first (the newest "updated" version wins because
    # the landing page lists the most-recently-published first).
    seen: dict[int, str] = {}
    for path, year in out:
        seen.setdefault(year, path)
    return sorted(seen.items(), key=lambda kv: -kv[0])  # type: ignore[return-value]


def download_zip(zip_path: str) -> bytes:
    """POST `agree=yes` to the AMA-license endpoint and return the ZIP bytes.

    The CMS license page form posts back to the same /files/zip/ URL with
    `agree=yes`. The response body is then the ZIP. Status is 200 with
    Content-Type: application/zip.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    # First load the license page so any session cookies are set.
    s.get(f"{DOWNLOAD_BASE}/license/ama?file={zip_path}", timeout=60)
    r = s.post(f"{DOWNLOAD_BASE}{zip_path}",
               data={"agree": "yes", "ReferURL": LANDING_URL},
               timeout=120, allow_redirects=True)
    if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("application/"):
        raise RuntimeError(
            f"Stark DHS download failed for {zip_path}: "
            f"status={r.status_code} ct={r.headers.get('Content-Type')!r}"
        )
    return r.content


def _section_normalize(line: str) -> str | None:
    """If `line` looks like a DHS section header, return its canonical name."""
    s = " ".join(line.split())
    if not s or len(s) < 10 or not _HEADER_RE.match(s):
        return None
    if s.startswith(_TITLE_PREFIX):
        return None
    if s.startswith("INCLUDE") or s.startswith("EXCLUDE"):
        return None
    return s


def parse_txt(text: str) -> Iterable[tuple[str, str, str | None, str]]:
    """Yield (hcpcs_code, dhs_category, short_description, raw_line)."""
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip().strip('"')
        if not line:
            continue
        # Code lines first — otherwise lines like "A9546 CO57/58" pass as a
        # section header (the header regex accepts uppercase+digits).
        m = _CODE_RE.match(line)
        if m and current:
            yield m.group(1), current, m.group(2).strip(), raw.rstrip()
            continue
        header = _section_normalize(line)
        if header:
            current = header


def ingest(limit: int | None = None) -> tuple[int, int]:
    """Download every available year, parse, upsert. Returns (fetched, upserted)."""
    available = list_available_zips()
    if not available:
        log.warning("No Stark DHS ZIPs found on the landing page.")
        return 0, 0
    log.info("Stark DHS: found %d ZIPs: %s",
             len(available), [y for y, _ in available])

    rows: list[tuple] = []
    for year, zip_path in available:
        url = f"{DOWNLOAD_BASE}{zip_path}"
        log.info("downloading %s", url)
        try:
            payload = download_zip(zip_path)
        except Exception as e:
            log.warning("skip %s: %s", url, e)
            continue
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            txt_name = next((n for n in z.namelist() if n.lower().endswith(".txt")), None)
            if not txt_name:
                log.warning("no .txt in %s (names=%s)", zip_path, z.namelist())
                continue
            text = z.read(txt_name).decode("latin-1", errors="replace")
        n_before = len(rows)
        for code, cat, desc, raw in parse_txt(text):
            rows.append((code, cat, year, desc, txt_name, url, raw))
            if limit is not None and len(rows) >= limit:
                break
        log.info("year=%d: parsed %d rows from %s",
                 year, len(rows) - n_before, txt_name)
        if limit is not None and len(rows) >= limit:
            break

    if limit is not None:
        rows = rows[:limit]

    dataset_meta = {"id": "stark_dhs", "name": "Stark Law DHS CPT/HCPCS list"}
    fetched = len(rows)
    upserted = 0
    status = "success"
    error = None
    columns = [
        "hcpcs_code", "dhs_category", "effective_year",
        "short_description", "source_doc", "source_url", "raw_line",
    ]
    with db.connect() as conn:
        handle = db.start_ingest_log(conn, dataset_meta)
        try:
            if rows:
                # Dedup within the batch — same code can repeat under one
                # category if the upstream file lists it twice.
                seen: dict[tuple[str, str, int], tuple] = {}
                for r in rows:
                    seen[(r[0], r[1], r[2])] = r
                deduped = list(seen.values())
                conn.insert("stark_dhs_codes", deduped, column_names=columns)
                upserted = len(deduped)
                log.info("inserted %d rows into stark_dhs_codes", upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            log.exception("Stark DHS ingest failed")
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted
