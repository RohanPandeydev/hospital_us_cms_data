"""Hospital Quality Summary ingester.

Source: data.cms.gov Provider Data Catalog dataset 'Hospital General Information'
(xubh-q36u). Per-CCN summary counts of how many quality measures fell
"Better than national" / "No different" / "Worse than national" across 5
groups: Mortality, Safety, Readmissions, Patient Experience, Timely & Effective.

The "_worse" columns are the closest publicly-available CMS-level signal
for "how many quality problems did this hospital have vs peers" — a proxy
for complaint volume without scraping QCOR per hospital.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import urllib.request

import requests

from . import db

log = logging.getLogger(__name__)

DATASET = {"id": "hospital_general_info", "name": "Hospital General Information"}

# Resolve the latest CSV URL via the Provider Data Catalog metastore API
# (the dataset slug is stable; the resource path rotates).
METASTORE = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/xubh-q36u"

COLS = [
    "ccn", "hospital_overall_rating",
    "mort_total", "mort_facility_count", "mort_better", "mort_no_different", "mort_worse",
    "safety_total", "safety_facility_count", "safety_better", "safety_no_different", "safety_worse",
    "readm_total", "readm_facility_count", "readm_better", "readm_no_different", "readm_worse",
    "pt_exp_total", "pt_exp_facility_count",
    "te_total", "te_facility_count",
    "source_url", "raw",
]
COL_TYPES = [
    "String", "Nullable(Int8)",
    "Nullable(Int8)", "Nullable(Int8)", "Nullable(Int8)", "Nullable(Int8)", "Nullable(Int8)",
    "Nullable(Int8)", "Nullable(Int8)", "Nullable(Int8)", "Nullable(Int8)", "Nullable(Int8)",
    "Nullable(Int8)", "Nullable(Int8)", "Nullable(Int8)", "Nullable(Int8)", "Nullable(Int8)",
    "Nullable(Int8)", "Nullable(Int8)",
    "Nullable(Int8)", "Nullable(Int8)",
    "Nullable(String)", "String",
]


def _int(v):
    if v in (None, "", "Not Available", "Not Applicable", "N/A", "NA", "*"):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def resolve_csv_url() -> str:
    log.info("Resolving Hospital General Information CSV via metastore")
    r = requests.get(METASTORE, timeout=60)
    r.raise_for_status()
    meta = r.json()
    for dist in meta.get("distribution", []):
        url = dist.get("downloadURL") or dist.get("accessURL", "")
        if url and (url.endswith(".csv") or (dist.get("mediaType") == "text/csv")):
            log.info("Resolved → %s", url)
            return url
    raise RuntimeError("No CSV distribution found for Hospital General Information")


def ingest(batch_size: int = 5000) -> tuple[int, int]:
    url = resolve_csv_url()
    log.info("Downloading %s", url)
    r = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("Downloaded %d bytes", len(r.content))
    text = r.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[tuple] = []
    for rec in reader:
        ccn = (rec.get("Facility ID") or "").strip()
        if not ccn:
            continue
        rows.append((
            ccn,
            _int(rec.get("Hospital overall rating")),
            _int(rec.get("MORT Group Measure Count")),
            _int(rec.get("Count of Facility MORT Measures")),
            _int(rec.get("Count of MORT Measures Better")),
            _int(rec.get("Count of MORT Measures No Different")),
            _int(rec.get("Count of MORT Measures Worse")),
            _int(rec.get("Safety Group Measure Count")),
            _int(rec.get("Count of Facility Safety Measures")),
            _int(rec.get("Count of Safety Measures Better")),
            _int(rec.get("Count of Safety Measures No Different")),
            _int(rec.get("Count of Safety Measures Worse")),
            _int(rec.get("READM Group Measure Count")),
            _int(rec.get("Count of Facility READM Measures")),
            _int(rec.get("Count of READM Measures Better")),
            _int(rec.get("Count of READM Measures No Different")),
            _int(rec.get("Count of READM Measures Worse")),
            _int(rec.get("Pt Exp Group Measure Count")),
            _int(rec.get("Count of Facility Pt Exp Measures")),
            _int(rec.get("TE Group Measure Count")),
            _int(rec.get("Count of Facility TE Measures")),
            url,
            json.dumps(rec, ensure_ascii=False),
        ))

    # Dedup by ccn
    seen: dict[str, tuple] = {}
    for r in rows:
        seen[r[0]] = r
    rows = list(seen.values())

    fetched = len(rows)
    upserted = 0
    status = "success"
    error: str | None = None
    with db.connect() as conn:
        handle = db.start_ingest_log(conn, DATASET)
        try:
            for i in range(0, len(rows), batch_size):
                chunk = rows[i : i + batch_size]
                conn.insert("hospital_quality_summary", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("Hospital Quality Summary upserted %d rows", upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    f, u = ingest()
    print(f"Done. fetched={f}, upserted={u}")
