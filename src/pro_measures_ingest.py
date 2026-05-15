"""Patient-Reported Outcomes (PROM) ingester.

Source: data.cms.gov Provider Data Catalog dataset 'Patient-Reported Outcomes - Hospital'
(mxtu-43qs). ~4.6K rows of THA/TKA PRO-PM — hospital-level performance on
the patient-reported outcome measure for total hip / total knee arthroplasty.

Stored under cms_hospital_measures with dataset_id='patient_reported_outcomes'.
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

DATASET_ID = "patient_reported_outcomes"
DATASET_NAME = "Patient-Reported Outcomes - Hospital"
CSV_URL = (
    "https://data.cms.gov/provider-data/sites/default/files/resources/"
    "60ee1962356ca17e50e12f95d2871b46_1777413971/PATIENT_REPORTED_OUTCOMES_FACILITY.csv"
)


def _row_id(*parts) -> int:
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
    return (
        _row_id(DATASET_ID, fid, mid),
        DATASET_ID, fid, mid,
        rec.get("Measure Name"),
        score, _num(score),
        None, None,                              # PROM file has no Lower/Higher
        None, None,
        None,                                    # no Compared to National
        None, None,                              # no Denominator
        rec.get("Start Date"), None,
        rec.get("End Date"), None,
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
            log.info("PROM upserted %d rows", upserted)
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
