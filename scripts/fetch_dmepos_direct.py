#!/usr/bin/env python3
"""DMEPOS by Supplier × Service — direct download bypassing the broken
iter_pages_per_value path.

The registered config has wrong field-name prefixes (Rfrg_* vs Suplr_*),
which made the per-state pagination silently insert NULL hcpcs codes.
This script hits the bulk download endpoint, normalizes Suplr_* → the
common cms_provider_summary tuple, and writes directly to ClickHouse.

Usage:
    python scripts/fetch_dmepos_direct.py
    python scripts/fetch_dmepos_direct.py --limit 10000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

DMEPOS_UUID = "1746a83e-bb65-4300-8e02-21edbab77c6b"
URL = f"https://data.cms.gov/data-api/v1/dataset/{DMEPOS_UUID}/data"
DATASET_ID = "medicare_dmepos_by_supplier_service"
PAGE_SIZE = 1000  # CMS API gets slow above this


def _client():
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=config.CLICKHOUSE_HOST, port=config.CLICKHOUSE_PORT,
        username=config.CLICKHOUSE_USER, password=config.CLICKHOUSE_PASSWORD,
        database=config.CLICKHOUSE_DATABASE, secure=config.CLICKHOUSE_SECURE,
        connect_timeout=15, send_receive_timeout=300,
    )


def _num(v):
    if v in (None, "", " "):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _row(r: dict) -> tuple:
    """Map a DMEPOS API row to cms_provider_summary tuple.
    Schema:
      dataset_id, year, ccn, npi, referring_npi, hcpcs_code, drg_code,
      drg_description, provider_state, provider_city, provider_name,
      total_services, total_beneficiaries, total_payment_amt, raw
    """
    return (
        DATASET_ID,
        None,                                     # year — DMEPOS row has no Year col
        None,                                     # ccn — supplier-keyed, no CCN
        r.get("Suplr_NPI"),                       # npi (the supplier)
        r.get("Rfrg_NPI"),                        # referring_npi if present
        r.get("HCPCS_Cd"),
        None,                                     # drg_code
        None,                                     # drg_description
        r.get("Suplr_Prvdr_State_Abrvtn"),
        r.get("Suplr_Prvdr_City"),
        r.get("Suplr_Prvdr_Last_Name_Org") or
            (r.get("Suplr_Prvdr_First_Name", "") + " " +
             r.get("Suplr_Prvdr_Last_Name_Org", "")).strip(),
        _num(r.get("Tot_Suplr_Srvcs") or r.get("Tot_Srvcs")),
        _num(r.get("Tot_Suplr_Benes") or r.get("Tot_Benes")),
        _num(r.get("Avg_Suplr_Mdcr_Pymt_Amt") or
             r.get("Avg_Mdcr_Pymt_Amt") or r.get("Avg_Mdcr_Stdzd_Amt")),
        json.dumps(r),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Stop after N rows (default: full table)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")
    log = logging.getLogger("dmepos")

    cli = _client()
    cols = ["dataset_id", "year", "ccn", "npi", "referring_npi",
            "hcpcs_code", "drg_code", "drg_description",
            "provider_state", "provider_city", "provider_name",
            "total_services", "total_beneficiaries", "total_payment_amt", "raw"]

    session = requests.Session()
    session.headers["User-Agent"] = "rp360-direct/0.1"

    offset = 0
    total = 0
    while True:
        if args.limit and total >= args.limit:
            break
        size = PAGE_SIZE if not args.limit else min(PAGE_SIZE, args.limit - total)
        log.info("page offset=%d size=%d (total so far=%d)", offset, size, total)
        # Retry transient timeouts — CMS occasionally takes 30-60s for an
        # offset window that's deep in the table.
        for attempt in range(1, 4):
            try:
                r = session.get(
                    URL,
                    params={"size": size, "offset": offset, "download": "true"},
                    timeout=180,
                )
                r.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                log.warning("  attempt %d failed: %s", attempt, e)
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        rows = r.json()
        if not isinstance(rows, list):
            rows = rows.get("data") or []
        if not rows:
            log.info("upstream returned no more rows; stopping")
            break

        tuples = [_row(x) for x in rows if x.get("HCPCS_Cd")]
        if tuples:
            cli.insert("cms_provider_summary", tuples, column_names=cols)
        total += len(rows)
        log.info("  → wrote %d rows (skipped %d w/o HCPCS_Cd)",
                 len(tuples), len(rows) - len(tuples))
        offset += len(rows)
        if len(rows) < size:
            log.info("partial page received; assuming end of dataset")
            break
        time.sleep(0.2)

    log.info("DONE — fetched %d total rows", total)


if __name__ == "__main__":
    main()
