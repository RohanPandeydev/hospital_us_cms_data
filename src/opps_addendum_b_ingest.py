"""OPPS Addendum B ingester — HCPCS → APC crosswalk + payment rates.

The CMS Outpatient PUF (medicare_outpatient_by_provider_service) is APC-keyed,
not HCPCS-keyed. That makes per-hospital HCPCS queries lossy for outpatient
services. Addendum B is the missing crosswalk: every HCPCS code that OPPS
recognizes, mapped to its APC group, status indicator, and payment rate.

Source: cms.gov/files/zip/{month}-{year}-opps-addendum-b.zip
Released quarterly (Jan / Apr / Jul / Oct). We ingest the latest available.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import zipfile

import requests

from . import db

log = logging.getLogger(__name__)

# Latest known release. CMS keeps prior releases at predictable URLs, so we
# can ingest multiple effective quarters by extending this list.
CURRENT_RELEASES = [
    ("https://www.cms.gov/files/zip/january-2026-opps-addendum-b.zip", "2026Q1"),
    ("https://www.cms.gov/files/zip/january-2025-opps-addendum-b.zip", "2025Q1"),
    ("https://www.cms.gov/files/zip/april-2025-opps-addendum-b.zip",   "2025Q2"),
]

COLUMNS = [
    "hcpcs_code", "effective_quarter", "short_descriptor",
    "status_indicator", "apc_code", "relative_weight", "payment_rate",
    "national_copayment", "minimum_copayment",
    "pass_through_expiry_year", "raw",
]


def _to_float(v):
    if v in (None, "", " "):
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_zip(content: bytes, quarter: str) -> list[tuple]:
    """Parse the CSV inside the ZIP into ClickHouse tuples."""
    z = zipfile.ZipFile(io.BytesIO(content))
    csv_name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
    if not csv_name:
        raise RuntimeError(f"no CSV in addendum ZIP: {z.namelist()}")
    text = z.read(csv_name).decode("latin-1", errors="replace")
    lines = text.splitlines()

    # The first ~5 rows are licence text; the real header has 'HCPCS Code' as
    # column 0 plus 'SI' and 'APC' columns.
    header_idx = next(
        (i for i, line in enumerate(lines[:30])
         if line.startswith("HCPCS Code") and "APC" in line),
        None,
    )
    if header_idx is None:
        raise RuntimeError(f"could not find header row in {csv_name}")
    reader = csv.DictReader(lines[header_idx:])

    rows: list[tuple] = []
    for r in reader:
        code = (r.get("HCPCS Code") or "").strip()
        if not code:
            continue
        rows.append((
            code,
            quarter,
            (r.get("Short Descriptor") or "").strip() or None,
            (r.get("SI") or "").strip() or None,
            (r.get("APC") or "").strip() or None,
            _to_float(r.get("Relative Weight")),
            _to_float(r.get("Payment Rate")),
            _to_float(r.get("National Unadjusted Copayment")),
            _to_float(r.get("Minimum Unadjusted Copayment")),
            (r.get("Drug and Device Pass-Through Expiration during Calendar Year") or "").strip() or None,
            json.dumps(r, ensure_ascii=False),
        ))
    return rows


def ingest(limit: int | None = None) -> tuple[int, int]:
    """Download every release, parse, upsert. Returns (fetched, upserted)."""
    dataset_meta = {"id": "opps_addendum_b",
                    "name": "OPPS Addendum B (HCPCS→APC crosswalk)"}
    fetched = 0
    upserted = 0
    status = "success"
    error = None
    all_rows: list[tuple] = []

    for url, quarter in CURRENT_RELEASES:
        log.info("downloading %s (%s)", url, quarter)
        try:
            r = requests.get(url, timeout=120,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
        except Exception as e:
            log.warning("skip %s: %s", url, e)
            continue
        rows = _parse_zip(r.content, quarter)
        log.info("  parsed %d rows from %s", len(rows), quarter)
        all_rows.extend(rows)
        fetched += len(rows)
        if limit is not None and len(all_rows) >= limit:
            all_rows = all_rows[:limit]
            break

    with db.connect() as conn:
        handle = db.start_ingest_log(conn, dataset_meta)
        try:
            if all_rows:
                # Dedupe by (hcpcs_code, effective_quarter)
                seen: dict[tuple[str, str], tuple] = {}
                for row in all_rows:
                    seen[(row[0], row[1])] = row
                deduped = list(seen.values())
                conn.insert("opps_addendum_b", deduped, column_names=COLUMNS)
                upserted = len(deduped)
                log.info("inserted %d rows into opps_addendum_b", upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            log.exception("OPPS Addendum B ingest failed")
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted
