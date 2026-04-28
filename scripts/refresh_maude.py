#!/usr/bin/env python3
"""Refresh MAUDE 2021-2025 for product codes in bridge_hcpcs_to_product_code.

Why scoped (not full-table): the openFDA `/device/event.json` endpoint caps
`skip` at 25,000 per search. A full 2021-2025 pull is ~24M events and would
need monthly date slices for every PC. This script targets only the ~48
product codes the bridge actually uses, year-sliced — and falls back to
month-slicing for the handful of high-volume PCs (FRN, QBJ, BZD, …) that
exceed 25K events/year.

Usage:
    python scripts/refresh_maude.py            # all bridge PCs, 2021-2025
    python scripts/refresh_maude.py --pc DXY   # one PC
    python scripts/refresh_maude.py --year-from 2024
"""

import argparse
import logging
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, db
from src.fda_client import FDAClient
import clickhouse_connect

log = logging.getLogger("refresh_maude")

# openFDA hard cap on `skip`. If a PC×year has more than this many events,
# slice into months.
MAX_PER_QUERY = 25_000


def _ch():
    return clickhouse_connect.get_client(
        host=config.CLICKHOUSE_HOST, port=config.CLICKHOUSE_PORT,
        username=config.CLICKHOUSE_USER, password=config.CLICKHOUSE_PASSWORD,
        database=config.CLICKHOUSE_DATABASE, secure=config.CLICKHOUSE_SECURE,
        connect_timeout=30, send_receive_timeout=120,
    )


def bridge_product_codes(ch):
    r = ch.query(
        "SELECT DISTINCT product_code FROM bridge_hcpcs_to_product_code "
        "WHERE product_code IS NOT NULL AND length(product_code)=3 "
        "ORDER BY product_code"
    )
    return [row[0] for row in r.result_rows]


def count_events(client: FDAClient, search: str) -> int:
    url = f"{client.base_url}/device/event.json"
    try:
        data = client._get(url, {"search": search, "limit": 1})
        return data.get("meta", {}).get("results", {}).get("total", 0)
    except Exception as e:
        log.warning("count failed (%s): %s", search, e)
        return 0


def pull_window(client: FDAClient, search: str, batch_size: int = 500):
    """Generator yielding rows for a search window. Wraps client.iter_events."""
    batch = []
    for row in client.iter_events(search=search):
        batch.append(row)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def date_windows(year: int, total: int):
    """Pick a slicing granularity for a (PC, year). Returns list of (search,)."""
    if total < MAX_PER_QUERY:
        return [f"date_received:[{year}0101 TO {year}1231]"]
    # Month slice — at MAX_PER_QUERY ≥ 25K/month most PCs fit; the very few
    # that don't (FRN > 100K/mo) get capped at 25K by openFDA which is okay
    # — we get the most recent 25K per month, plenty for time-series shape.
    months = []
    for m in range(1, 13):
        if m == 12:
            end = f"{year}1231"
        else:
            end = f"{year}{m+1:02d}01"
        start = f"{year}{m:02d}01"
        months.append(f"date_received:[{start} TO {end}]")
    return months


def refresh_pc(client: FDAClient, ch, pc: str, year_from: int, year_to: int):
    pulled = 0
    for year in range(year_from, year_to + 1):
        head = f"device.device_report_product_code:{pc}"
        year_search = f"{head} AND date_received:[{year}0101 TO {year}1231]"
        total = count_events(client, year_search)
        if total == 0:
            log.info("  %s %d: 0 events", pc, year)
            continue
        log.info("  %s %d: %d events", pc, year, total)
        windows = date_windows(year, total)
        for win in windows:
            search = f"{head} AND {win}"
            try:
                for batch in pull_window(client, search):
                    n = db.upsert_fda_events(ch, batch)
                    pulled += n or 0
            except Exception as e:
                log.warning("  window failed (%s): %s", search, e)
                time.sleep(2)
    log.info("DONE %s: pulled %d events", pc, pulled)
    return pulled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pc", help="Single product_code to refresh")
    ap.add_argument("--year-from", type=int, default=2021)
    ap.add_argument("--year-to", type=int, default=2025)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )

    ch = _ch()
    client = FDAClient()
    if not client.api_key:
        log.warning("FDA_API_KEY unset — daily cap is 1000 requests; this will be slow.")

    if args.pc:
        pcs = [args.pc.upper()]
    else:
        pcs = bridge_product_codes(ch)
    log.info("Refreshing %d product codes for %d-%d",
             len(pcs), args.year_from, args.year_to)

    grand = 0
    for i, pc in enumerate(pcs, 1):
        log.info("[%d/%d] %s", i, len(pcs), pc)
        try:
            grand += refresh_pc(client, ch, pc, args.year_from, args.year_to)
        except Exception as e:
            log.exception("PC %s failed: %s", pc, e)

    log.info("GRAND TOTAL pulled: %d", grand)


if __name__ == "__main__":
    main()
