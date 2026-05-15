"""Medicare Revalidation Due Date List ingester.

Source: data.cms.gov 'Revalidation Due Date List' (~100K rows).
Tracks each enrolled provider's next revalidation deadline; lapsed
revalidations risk billing-eligibility loss.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging

from . import db
from .cms_dcat_helpers import resolve_csv_url, download_csv, iter_dict_rows

log = logging.getLogger(__name__)

DATASET_TITLE = "Revalidation Due Date List"
DATASET = {"id": "revalidation_due", "name": DATASET_TITLE}

COLS = [
    "enrollment_id", "npi", "first_name", "last_name", "org_name",
    "revalidation_due_date", "revalidation_status",
    "adjusted_revalidation_due_date", "source_url", "raw",
]
COL_TYPES = [
    "String", "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "String",
]


def _iso(s):
    if not s:
        return None
    s = str(s).strip()
    if not s or s.upper() in ("TBD", "N/A", "NA", "NONE"):
        return s.upper() if s else None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s  # keep raw if it's a status string like 'TBD'


def _find(row, *keys):
    for k in keys:
        if k in row and row[k]:
            return row[k]
    return None


def ingest(batch_size: int = 5000) -> tuple[int, int]:
    url = resolve_csv_url(DATASET_TITLE)
    payload = download_csv(url)
    rows: list[tuple] = []
    for r in iter_dict_rows(payload):
        eid = (_find(r, "Enrollment ID", "ENROLLMENT_ID") or "").strip()
        if not eid:
            continue
        due_raw = _find(r, "Revalidation Due Date", "REVALIDATION_DUE_DATE")
        rows.append((
            eid,
            _find(r, "National Provider Identifier", "NPI"),
            _find(r, "First Name", "FIRST_NAME"),
            _find(r, "Last Name", "LAST_NAME"),
            _find(r, "Organization Name", "ORG_NAME"),
            _iso(due_raw),
            _find(r, "Enrollment Type", "ENROLLMENT_TYPE"),
            _iso(_find(r, "Adjusted Due Date", "ADJUSTED_DUE_DATE")),
            url,
            json.dumps(r, ensure_ascii=False),
        ))

    # Dedup by enrollment_id
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
                conn.insert("medicare_revalidation", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("Revalidation upserted %d rows", upserted)
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
