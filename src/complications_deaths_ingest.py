"""CMS Complications and Deaths + PSI-90 composite ingester.

Both files share the cms_hospital_measures schema. Source:
  - Complications and Deaths - Hospital (95,841 rows / ~23 MB)
    https://data.cms.gov/provider-data/dataset/...complications-and-deaths-hospital
  - CMS Medicare PSI-90 and component measures (52,361 rows / ~9 MB)
    https://data.cms.gov/provider-data/dataset/...psi-six-decimal-file

Both are per-hospital per-measure rows. We ingest into the existing
cms_hospital_measures table under distinct dataset_ids so we can keep the
existing columns / measure_ids logic unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import requests

from . import db


def _row_id(*parts) -> int:
    """63-bit deterministic Int64 from natural key. Required because the live
    cms_hospital_measures table uses `id Int64` as its ReplacingMergeTree
    sort key (not the (dataset_id, facility_id, measure_id) tuple in the
    schema file). Without unique ids, all rows collide on id=0 and the
    table collapses everything to a single row per merge."""
    key = "|".join(str(p or "") for p in parts)
    return int(hashlib.sha1(key.encode()).hexdigest()[:15], 16)

log = logging.getLogger(__name__)

SOURCES = [
    {
        "dataset_id": "complications_and_deaths_hospital",
        "name": "Complications and Deaths - Hospital",
        "url": "https://data.cms.gov/provider-data/sites/default/files/resources/6af7c44d77436e5a1caac3ce39a83fe9_1777413950/Complications_and_Deaths-Hospital.csv",
    },
    {
        "dataset_id": "psi_90_composite",
        "name": "CMS Medicare PSI-90 and component measures",
        "url": "https://data.cms.gov/provider-data/sites/default/files/resources/84bd78c1b4e386185bcef2af963d8cf9_1777413948/CMS_PSI_6_decimal_file.csv",
    },
]


def _num(v):
    if v in (None, "", "Not Available", "Not Applicable", "N/A", "NA", "*"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _row_to_tuple(dataset_id: str, rec: dict) -> tuple | None:
    fid = (rec.get("Facility ID") or "").strip()
    mid = (rec.get("Measure ID") or "").strip()
    if not fid or not mid:
        return None
    score = rec.get("Score") or rec.get("Rate")
    lower = rec.get("Lower Estimate") or rec.get("Lower Confidence Limit")
    higher = rec.get("Higher Estimate") or rec.get("Higher Confidence Limit")
    denom = rec.get("Denominator")
    start = rec.get("Start Date") or rec.get("Measurement Start Date")
    end = rec.get("End Date") or rec.get("Measurement End Date")
    return (
        _row_id(dataset_id, fid, mid),
        dataset_id, fid, mid,
        rec.get("Measure Name"),
        score, _num(score),
        lower, _num(lower),
        higher, _num(higher),
        rec.get("Compared to National") or None,
        denom, _num(denom),
        start, None,                                  # start_date, start_date_parsed
        end, None,                                    # end_date, end_date_parsed
        rec.get("Footnote") or None,
        json.dumps(rec, ensure_ascii=False),
    )


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


def ingest_one(source: dict, batch_size: int = 5000) -> tuple[int, int]:
    url = source["url"]
    dsid = source["dataset_id"]
    log.info("Downloading %s", url)
    r = requests.get(url, timeout=300, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("Downloaded %d bytes", len(r.content))
    text = r.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for rec in reader:
        t = _row_to_tuple(dsid, rec)
        if t:
            rows.append(t)

    # Dedup by row.id (the ReplacingMergeTree key on the live table)
    seen: dict[int, tuple] = {}
    for row in rows:
        seen[row[0]] = row
    rows = list(seen.values())

    fetched = len(rows)
    upserted = 0
    status = "success"
    error: str | None = None
    with db.connect() as conn:
        handle = db.start_ingest_log(conn, {"id": dsid, "name": source["name"]})
        try:
            for i in range(0, len(rows), batch_size):
                chunk = rows[i : i + batch_size]
                conn.insert("cms_hospital_measures", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("%s upserted %d rows", dsid, upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted


def ingest() -> tuple[int, int]:
    total_f = total_u = 0
    for src in SOURCES:
        f, u = ingest_one(src)
        total_f += f
        total_u += u
    return total_f, total_u


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    f, u = ingest()
    print(f"Done. fetched={f}, upserted={u}")
