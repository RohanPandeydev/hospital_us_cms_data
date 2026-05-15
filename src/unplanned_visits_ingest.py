"""Unplanned Hospital Visits ingester.

Source: data.cms.gov Provider Data Catalog dataset 'Unplanned Hospital Visits - Hospital'
(632h-zaca). ~67K rows. Per-CCN, per-measure 30-day return / readmission rates:

  - EDAC_30_AMI (heart attack return days)        ← Boston Scientific stents/cardio
  - EDAC_30_HF  (heart failure return days)       ← BSc CRM/Watchman
  - EDAC_30_PN  (pneumonia)
  - READM_30_CABG (coronary bypass readmits)      ← BSc cardio
  - READM_30_HIP_KNEE (joint replacement)
  - READM_30_COPD
  - OP_32 (colonoscopy follow-up)                 ← BSc endoscopy
  - OP_35_* (chemotherapy/cancer return)

Stored under cms_hospital_measures (same shape as Complications & Deaths)
with dataset_id='unplanned_hospital_visits'.
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

DATASET_ID = "unplanned_hospital_visits"
DATASET_NAME = "Unplanned Hospital Visits - Hospital"
CSV_URL = (
    "https://data.cms.gov/provider-data/sites/default/files/resources/"
    "30edc1d0417a34b58affcc2495a02b0a_1777413968/Unplanned_Hospital_Visits-Hospital.csv"
)


def _row_id(*parts) -> int:
    """63-bit deterministic Int64. cms_hospital_measures uses id Int64 sort key."""
    key = "|".join(str(p or "") for p in parts)
    return int(hashlib.sha1(key.encode()).hexdigest()[:15], 16)


def _num(v):
    if v in (None, "", "Not Available", "Not Applicable", "N/A", "NA", "*"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


COLS = [
    "id", "dataset_id", "facility_id", "measure_id", "measure_name",
    "score", "score_num", "lower_estimate", "lower_estimate_num",
    "higher_estimate", "higher_estimate_num", "compared_to_national",
    "denominator", "denominator_num",
    "start_date", "start_date_parsed",
    "end_date", "end_date_parsed",
    "footnote", "raw",
]
COL_TYPES = [
    "Int64", "String", "String", "String", "Nullable(String)",
    "Nullable(String)", "Nullable(Float64)", "Nullable(String)", "Nullable(Float64)",
    "Nullable(String)", "Nullable(Float64)", "Nullable(String)",
    "Nullable(String)", "Nullable(Float64)",
    "Nullable(String)", "Nullable(Date)",
    "Nullable(String)", "Nullable(Date)",
    "Nullable(String)", "String",
]


def _row_to_tuple(rec: dict) -> tuple | None:
    fid = (rec.get("Facility ID") or "").strip()
    mid = (rec.get("Measure ID") or "").strip()
    if not fid or not mid:
        return None
    score = rec.get("Score")
    lower = rec.get("Lower Estimate")
    higher = rec.get("Higher Estimate")
    denom = rec.get("Denominator")
    start = rec.get("Start Date")
    end = rec.get("End Date")
    return (
        _row_id(DATASET_ID, fid, mid),
        DATASET_ID, fid, mid,
        rec.get("Measure Name"),
        score, _num(score),
        lower, _num(lower),
        higher, _num(higher),
        rec.get("Compared to National") or None,
        denom, _num(denom),
        start, None,
        end, None,
        rec.get("Footnote") or None,
        json.dumps(rec, ensure_ascii=False),
    )


def ingest(batch_size: int = 5000) -> tuple[int, int]:
    log.info("Downloading %s", CSV_URL)
    r = requests.get(CSV_URL, timeout=300, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("Downloaded %d bytes", len(r.content))
    text = r.content.decode("utf-8-sig", errors="replace")
    rows: list[tuple] = []
    for rec in csv.DictReader(io.StringIO(text)):
        t = _row_to_tuple(rec)
        if t:
            rows.append(t)

    # Dedup by id (live table's ReplacingMergeTree key)
    seen: dict[int, tuple] = {}
    for row in rows:
        seen[row[0]] = row
    rows = list(seen.values())

    fetched = len(rows)
    upserted = 0
    status = "success"
    error: str | None = None
    with db.connect() as conn:
        handle = db.start_ingest_log(conn, {"id": DATASET_ID, "name": DATASET_NAME})
        try:
            for i in range(0, len(rows), batch_size):
                chunk = rows[i : i + batch_size]
                conn.insert("cms_hospital_measures", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("Unplanned visits upserted %d rows", upserted)
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
