"""HCRIS — Hospital Provider Cost Report ingester.

Source: data.cms.gov 'Hospital Provider Cost Report' (~6K hospitals × year,
parsed from the raw CMS HCRIS extract on the data.cms.gov side).

Each row is one hospital cost report for a fiscal year. The headline metrics
are bed count, discharges, total charges/costs, capital, uncompensated care.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging

from . import db
from .cms_dcat_helpers import resolve_csv_url, download_csv, iter_dict_rows

log = logging.getLogger(__name__)

DATASET_TITLE = "Hospital Provider Cost Report"
DATASET = {"id": "hcris_hospital_cost", "name": DATASET_TITLE}

COLS = [
    "rpt_rec_num", "ccn", "fy_bgn_dt", "fy_end_dt", "proc_dt",
    "initl_rpt_sw", "last_rpt_sw", "trnsmtl_num", "fi_num", "adr_vndr_cd",
    "fy_year",
    "total_beds", "total_discharges", "medicare_discharges", "medicaid_discharges",
    "total_charges", "total_costs",
    "medical_supplies_cost", "capital_expenditure", "uncompensated_care_cost",
    "source_url", "raw",
]
COL_TYPES = [
    "UInt64", "String", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(UInt16)",
    "Nullable(Int32)", "Nullable(Int64)", "Nullable(Int64)", "Nullable(Int64)",
    "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(String)", "String",
]


def _num(v):
    if v in (None, "", "NA"):
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


def _iso(v):
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def _find(row, *keys):
    """Loose case/whitespace match."""
    for k in keys:
        kl = k.lower().strip()
        for col, v in row.items():
            if (col or "").lower().strip() == kl:
                return v
    return None


def ingest(batch_size: int = 2000) -> tuple[int, int]:
    url = resolve_csv_url(DATASET_TITLE)
    payload = download_csv(url)
    rows: list[tuple] = []
    for r in iter_dict_rows(payload):
        rrn_raw = _find(r, "rpt_rec_num", "Report Record Number")
        try:
            rrn = int(rrn_raw)
        except (TypeError, ValueError):
            continue
        ccn = (_find(r, "Provider CCN", "ccn") or "").strip()
        fy_end = _iso(_find(r, "Fiscal Year End Date"))
        fy_year = None
        if fy_end and len(fy_end) >= 4 and fy_end[:4].isdigit():
            fy_year = int(fy_end[:4])
        rows.append((
            rrn,
            ccn,
            _iso(_find(r, "Fiscal Year Begin Date")),
            fy_end,
            None,  # proc_dt not exposed in this extract
            None, None, None, None, None,
            fy_year,
            _int(_find(r, "Number of Beds")),
            _int(_find(r, "Total Discharges Title XVIII", "Total Discharges (V + XVIII + XIX + Unknown)")),
            _int(_find(r, "Total Discharges Title XVIII")),
            _int(_find(r, "Total Discharges Title XIX")),
            _num(_find(r, "Combined Outpatient + Inpatient Total Charges",
                       "Total Charges")),
            _num(_find(r, "Total Costs")),
            None,  # Total Medical Supply Costs not in this DCAT extract
            _num(_find(r, "Depreciation Cost")),  # capital proxy
            _num(_find(r, "Cost of Uncompensated Care",
                       "Cost of Charity Care")),
            url,
            json.dumps(r, ensure_ascii=False),
        ))

    # Dedup by rpt_rec_num
    seen: dict[int, tuple] = {}
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
                conn.insert("hcris_hospital_cost_reports", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("HCRIS upserted %d rows", upserted)
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
