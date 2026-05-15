"""CMS Hospital-Acquired Conditions (HAC) Measures ingester.

Source: data.cms.gov 'Deficit Reduction Act Hospital-Acquired Condition Measures'
(~12K rows / ~1 MB). Per-hospital rates for 4 device-relevant safety measures:

  - Foreign Object Retained After Surgery (CMS-level proxy for "device left behind")
  - Air Embolism (catheter / IV-related)
  - Blood Incompatibility
  - Falls and Trauma

This is the closest CMS-level dataset to "what device-related safety issues did
each hospital have." It complements the existing HAI_* measures already in
cms_hospital_measures (CLABSI / CAUTI / SSI).
"""

from __future__ import annotations

import json
import logging

from . import db
from .cms_dcat_helpers import resolve_csv_url, download_csv, iter_dict_rows

log = logging.getLogger(__name__)

DATASET_TITLE = "Deficit Reduction Act Hospital-Acquired Condition Measures"
DATASET = {"id": "hac_measures", "name": DATASET_TITLE}

COLS = [
    "ccn", "measure_name", "rate", "footnote",
    "start_quarter", "end_quarter", "source_url", "raw",
]
COL_TYPES = [
    "String", "String", "Nullable(Float64)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)", "String",
]


def _num(v):
    if v in (None, "", "Not Available", "N/A", "NA"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ingest(batch_size: int = 5000) -> tuple[int, int]:
    url = resolve_csv_url(DATASET_TITLE)
    payload = download_csv(url)
    rows: list[tuple] = []
    for r in iter_dict_rows(payload):
        ccn = (r.get("Provider_ID") or "").strip()
        measure = (r.get("Measure") or "").strip()
        if not ccn or not measure:
            continue
        rows.append((
            ccn,
            measure,
            _num(r.get("Rate")),
            r.get("Footnote") or None,
            r.get("Start_Quarter") or None,
            r.get("End_Quarter") or None,
            url,
            json.dumps(r, ensure_ascii=False),
        ))

    # Dedup by (ccn, measure)
    seen: dict[tuple, tuple] = {}
    for r in rows:
        seen[(r[0], r[1])] = r
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
                conn.insert("hospital_hac_measures", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("HAC upserted %d rows", upserted)
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
