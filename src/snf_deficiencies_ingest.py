"""Nursing Home Health Deficiencies + Citation Codes ingester.

Source: data.cms.gov/provider-data
  - Health Deficiencies (~418K rows / ~165 MB)
  - Citation Code Look-up (~644 rows / ~97 KB)

Per-survey-date deficiency rows with the ACTUAL Form-2567-style citation
text + severity codes + tag numbers. This is the publicly-accessible
equivalent of what ProPublica's Hospital Inspections project published
(for nursing homes, not hospitals). 15K+ SNFs covered.

We use the metastore API to resolve the current month's CSV URL so the
ingester self-updates as CMS rolls quarterly releases.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import requests

from . import db

log = logging.getLogger(__name__)

METASTORE = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items"


def _hash_id(*parts) -> int:
    """63-bit deterministic id for ReplacingMergeTree."""
    key = "|".join(str(p or "") for p in parts)
    return int(hashlib.sha1(key.encode()).hexdigest()[:15], 16)


def _int(v):
    if v in (None, "", "Not Available", "N/A", "*"):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _flag(v):
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("Y", "YES", "TRUE", "1"):
        return 1
    if s in ("N", "NO", "FALSE", "0"):
        return 0
    return None


def resolve_url(dataset_title: str) -> str:
    r = requests.get(METASTORE, timeout=60)
    r.raise_for_status()
    for it in r.json():
        if it.get("title","").strip() == dataset_title:
            for dist in it.get("distribution", []):
                url = dist.get("downloadURL","")
                if url and url.endswith(".csv"):
                    log.info("Resolved '%s' → %s", dataset_title, url)
                    return url
    raise RuntimeError(f"Dataset '{dataset_title}' not in Provider Data Catalog")


# --- Health Deficiencies ---
DEF_COLS = [
    "deficiency_id", "ccn", "provider_name", "state", "survey_date", "survey_type",
    "deficiency_prefix", "deficiency_category", "deficiency_tag", "deficiency_text",
    "scope_severity", "correction_date", "inspection_cycle",
    "is_standard", "is_complaint", "is_infection_control",
    "source_url", "raw",
]
DEF_TYPES = [
    "UInt64", "String", "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(UInt8)",
    "Nullable(UInt8)", "Nullable(UInt8)", "Nullable(UInt8)",
    "Nullable(String)", "String",
]


def ingest_deficiencies(batch_size: int = 10000) -> tuple[int, int]:
    url = resolve_url("Health Deficiencies")
    log.info("Downloading %s", url)
    r = requests.get(url, timeout=600, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("Downloaded %d bytes", len(r.content))
    text = r.content.decode("utf-8-sig", errors="replace")
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        ccn = (rec.get("CMS Certification Number (CCN)") or "").strip()
        survey_date = (rec.get("Survey Date") or "").strip()
        tag = (rec.get("Deficiency Tag Number") or "").strip()
        loc = (rec.get("Location") or "").strip()
        if not ccn:
            continue
        rows.append((
            _hash_id(ccn, survey_date, tag, loc),
            ccn,
            rec.get("Provider Name") or None,
            rec.get("State") or None,
            survey_date or None,
            rec.get("Survey Type") or None,
            rec.get("Deficiency Prefix") or None,
            rec.get("Deficiency Category") or None,
            tag or None,
            rec.get("Deficiency Description") or None,
            rec.get("Scope Severity Code") or None,
            rec.get("Correction Date") or None,
            _int(rec.get("Inspection Cycle")),
            _flag(rec.get("Standard Deficiency")),
            _flag(rec.get("Complaint Deficiency")),
            _flag(rec.get("Infection Control Inspection Deficiency")),
            url,
            json.dumps(rec, ensure_ascii=False),
        ))

    # Dedup by deficiency_id
    seen: dict[int, tuple] = {}
    for row in rows:
        seen[row[0]] = row
    rows = list(seen.values())

    fetched = len(rows)
    upserted = 0
    with db.connect() as conn:
        handle = db.start_ingest_log(conn, {"id": "snf_health_deficiencies", "name": "SNF Health Deficiencies"})
        try:
            for i in range(0, len(rows), batch_size):
                chunk = rows[i : i + batch_size]
                conn.insert("snf_health_deficiencies", chunk,
                            column_names=DEF_COLS, column_type_names=DEF_TYPES)
                upserted += len(chunk)
                if upserted % 100000 == 0:
                    log.info("  ...progress: %d rows", upserted)
            log.info("SNF deficiencies upserted %d rows", upserted)
            db.finish_ingest_log(conn, handle, fetched, upserted, "success", None)
        except Exception as e:
            db.finish_ingest_log(conn, handle, fetched, upserted, "error", repr(e))
            raise
    return fetched, upserted


# --- Citation Codes Look-up ---
CODE_COLS = ["tag_prefix", "tag_number", "tag_combined", "tag_description", "tag_category", "source_url"]
CODE_TYPES = ["String", "String", "String", "Nullable(String)", "Nullable(String)", "Nullable(String)"]


def ingest_citation_codes() -> tuple[int, int]:
    url = resolve_url("Citation Code Look-up")
    log.info("Downloading %s", url)
    r = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        prefix = (rec.get("Deficiency Prefix") or "").strip()
        num = (rec.get("Deficiency Tag Number") or "").strip()
        if not prefix or not num:
            continue
        rows.append((
            prefix, num,
            rec.get("Deficiency Prefix and Number") or f"{prefix}-{num}",
            rec.get("Deficiency Description") or None,
            rec.get("Deficiency Category") or None,
            url,
        ))
    # Dedup
    seen: dict[tuple, tuple] = {}
    for row in rows:
        seen[(row[0], row[1])] = row
    rows = list(seen.values())
    with db.connect() as conn:
        conn.insert("snf_citation_codes", rows,
                    column_names=CODE_COLS, column_type_names=CODE_TYPES)
        log.info("Citation codes upserted %d rows", len(rows))
    return len(rows), len(rows)


def ingest() -> tuple[int, int]:
    f1, u1 = ingest_citation_codes()
    f2, u2 = ingest_deficiencies()
    return f1 + f2, u1 + u2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    f, u = ingest()
    print(f"Done. fetched={f}, upserted={u}")
