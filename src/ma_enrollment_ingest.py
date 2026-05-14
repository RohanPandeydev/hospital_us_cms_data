"""Medicare Monthly Enrollment ingester.

Source: data.cms.gov 'Medicare Monthly Enrollment' (~567K rows / ~200 MB).
County-level monthly enrollment counts split by Original Medicare / MA /
demographics. Used for hospital catchment market sizing.
"""

from __future__ import annotations

import json
import logging

from . import db
from .cms_dcat_helpers import resolve_csv_url, download_csv, iter_dict_rows

log = logging.getLogger(__name__)

DATASET_TITLE = "Medicare Monthly Enrollment"
DATASET = {"id": "medicare_monthly_enrollment", "name": DATASET_TITLE}

COLS = [
    "snapshot_month", "contract_id", "plan_id",
    "state", "county", "fips_state_county", "enrollment",
    "source_url", "raw",
]
COL_TYPES = [
    "String", "String", "String",
    "Nullable(String)", "Nullable(String)", "String", "Nullable(Int64)",
    "Nullable(String)", "String",
]

MONTH_MAP = {
    "January": "01", "February": "02", "March": "03", "April": "04",
    "May": "05", "June": "06", "July": "07", "August": "08",
    "September": "09", "October": "10", "November": "11", "December": "12",
}


def _int(v):
    if v in (None, "", "*"):
        return None
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def ingest(batch_size: int = 20000, limit: int | None = None) -> tuple[int, int]:
    url = resolve_csv_url(DATASET_TITLE)
    payload = download_csv(url)
    rows: list[tuple] = []
    for r in iter_dict_rows(payload):
        year = (r.get("YEAR") or "").strip()
        month = (r.get("MONTH") or "").strip()
        # The CSV stores months as "January", "February" etc. Convert.
        mm = MONTH_MAP.get(month) or (month if month.isdigit() else "")
        if not year or not mm:
            continue
        # Subset: store only state-level rows (BENE_GEO_LVL='State') to keep
        # row count manageable. County-level rows blow up size 3000x.
        geo_lvl = (r.get("BENE_GEO_LVL") or "").strip()
        if geo_lvl and geo_lvl.lower() != "state":
            continue
        snapshot = f"{year}-{mm}"
        fips = (r.get("BENE_FIPS_CD") or "").strip()
        rows.append((
            snapshot,
            "",  # contract_id not in this file
            "",  # plan_id not in this file
            r.get("BENE_STATE_ABRVTN") or None,
            r.get("BENE_COUNTY_DESC") or r.get("BENE_STATE_DESC") or None,
            fips,
            _int(r.get("TOT_BENES")),
            url,
            json.dumps({
                "MA": r.get("MA_AND_OTH_BENES"),
                "ORIG": r.get("ORGNL_MDCR_BENES"),
                "AGED": r.get("AGED_TOT_BENES"),
                "DSBLD": r.get("DSBLD_TOT_BENES"),
            }, ensure_ascii=False),
        ))
        if limit and len(rows) >= limit:
            break

    # Dedup by (snapshot, contract_id, plan_id, fips)
    seen: dict[tuple, tuple] = {}
    for r in rows:
        seen[(r[0], r[1], r[2], r[5])] = r
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
                conn.insert("medicare_ma_partd_enrollment", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("MA enrollment upserted %d rows", upserted)
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
