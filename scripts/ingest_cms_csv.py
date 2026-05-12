#!/usr/bin/env python3
"""Ingest a CMS direct-download CSV into cms_provider_summary.

Bypasses the broken iter_pages_per_value path. Works for any of the
direct CSVs found via the CMS catalog (data.cms.gov/data.json):

  - Physician PUF by Provider × HCPCS  (any year)
  - DMEPOS by Supplier × HCPCS         (any year)
  - HCRIS Hospital Cost Report          (any year — note: no HCPCS column)

Auto-detects the schema (Rndrng_/Suplr_/Provider CCN) and maps to the
common cms_provider_summary tuple.

Usage:
    python scripts/ingest_cms_csv.py downloads/physician_2022.csv \
        --dataset-id medicare_physician_by_provider_service --year 2022

    python scripts/ingest_cms_csv.py downloads/dmepos_2023.csv \
        --dataset-id medicare_dmepos_by_supplier_service --year 2023
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

BATCH_SIZE = 5000


def _client():
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=config.CLICKHOUSE_HOST, port=config.CLICKHOUSE_PORT,
        username=config.CLICKHOUSE_USER, password=config.CLICKHOUSE_PASSWORD,
        database=config.CLICKHOUSE_DATABASE, secure=config.CLICKHOUSE_SECURE,
        connect_timeout=15, send_receive_timeout=300,
    )


def _first(row, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def _num(v):
    if v in (None, "", " "):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _row_to_tuple(dataset_id: str, default_year: int, r: dict) -> tuple:
    """Map any of the three CSV shapes to the cms_provider_summary tuple."""
    return (
        dataset_id,
        default_year,
        _first(r, "Rndrng_Prvdr_CCN", "Prvdr_CCN", "Provider CCN"),
        _first(r, "Rndrng_NPI", "Suplr_NPI", "Rfrg_NPI", "Prvdr_NPI"),
        _first(r, "Rfrg_NPI"),
        _first(r, "HCPCS_Cd", "HCPCS_CD", "Betos_Cd"),
        _first(r, "DRG_Cd", "MS_DRG_Cd"),
        _first(r, "DRG_Desc"),
        _first(r, "Rndrng_Prvdr_State_Abrvtn", "Suplr_Prvdr_State_Abrvtn",
                  "Rfrg_Prvdr_State_Abrvtn", "State Code"),
        _first(r, "Rndrng_Prvdr_City", "Suplr_Prvdr_City",
                  "Rfrg_Prvdr_City", "City"),
        _first(r, "Rndrng_Prvdr_Org_Name", "Suplr_Prvdr_Last_Name_Org",
                  "Rfrg_Prvdr_Last_Org_Name", "Hospital Name") or
        (r.get("Rndrng_Prvdr_Last_Org_Name") or ""),
        _num(_first(r, "Tot_Srvcs", "Tot_Suplr_Srvcs", "Tot_Dschrgs", "CAPC_Srvcs")),
        _num(_first(r, "Tot_Benes", "Tot_Suplr_Benes", "Bene_Cnt")),
        _num(_first(r, "Avg_Mdcr_Pymt_Amt", "Avg_Suplr_Mdcr_Pymt_Amt",
                       "Avg_Tot_Pymt_Amt", "Avg_Mdcr_Stdzd_Amt")),
        json.dumps(r),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")
    log = logging.getLogger("cms_csv")

    path = Path(args.csv_path)
    if not path.exists():
        log.error("file not found: %s", path)
        sys.exit(1)

    cli = _client()
    cols = ["dataset_id", "year", "ccn", "npi", "referring_npi",
            "hcpcs_code", "drg_code", "drg_description",
            "provider_state", "provider_city", "provider_name",
            "total_services", "total_beneficiaries", "total_payment_amt", "raw"]

    log.info("Loading %s into %s (year=%d)", path, args.dataset_id, args.year)
    total, batch = 0, []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            batch.append(_row_to_tuple(args.dataset_id, args.year, row))
            if len(batch) >= BATCH_SIZE:
                cli.insert("cms_provider_summary", batch, column_names=cols)
                total += len(batch)
                if total % 50000 == 0:
                    log.info("  wrote %d rows", total)
                batch = []
                if args.limit and total >= args.limit:
                    break
        if batch and (not args.limit or total < args.limit):
            cli.insert("cms_provider_summary", batch, column_names=cols)
            total += len(batch)

    log.info("DONE — wrote %d rows from %s", total, path.name)


if __name__ == "__main__":
    main()
