#!/usr/bin/env python3
"""Fetch every AXIOS-branded MAUDE adverse event from openFDA.

The 10k-row generic MAUDE sample loaded into fda_maude_events did not capture
the AXIOS lumen-apposing metal stent (Boston Scientific) — making the /axios
page's headline rate read 0. openFDA actually has ~2,367 AXIOS events
(79 deaths, 1,309 injuries) under search=device.brand_name:axios.

This script pulls them all and inserts via the existing upsert path, so the
/axios page populates on next refresh. No TinyFish needed — openFDA returns
the full corpus directly.

Usage:
    python scripts/fetch_axios_maude.py             # full pull
    python scripts/fetch_axios_maude.py --limit 50  # smoke test
    python scripts/fetch_axios_maude.py --dry-run   # fetch + log, no insert
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db, fda_client  # noqa: E402

# openFDA Lucene query. AXIOS is a single-vendor brand (Xlumena → Boston
# Scientific), so the brand match is sufficient — no manufacturer filter
# needed. We chunk by year to stay under the 25k skip ceiling.
SEARCH_BASE = "device.brand_name:axios"

# Year buckets (inclusive both ends). openFDA has AXIOS-named events going
# back to 2003 (likely the original Xlumena trademark before Boston Scientific
# acquired it in 2014). We capture the full history so the lifetime KPI is
# accurate; the /axios page's 12-month / prior-12-month windows handle recency.
YEAR_BUCKETS = [
    ("2003-01-01", "2013-12-31"),
    ("2014-01-01", "2017-12-31"),
    ("2018-01-01", "2019-12-31"),
    ("2020-01-01", "2021-12-31"),
    ("2022-01-01", "2023-12-31"),
    ("2024-01-01", "2024-12-31"),
    ("2025-01-01", "2025-12-31"),
    ("2026-01-01", "2026-12-31"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Stop after N events (across all year buckets).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch + log only; don't write to ClickHouse.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")
    log = logging.getLogger("axios_maude")

    client = fda_client.FDAClient()
    conn = None if args.dry_run else db.connect()

    total = 0
    for d_from, d_to in YEAR_BUCKETS:
        if args.limit and total >= args.limit:
            break
        # NB: spaces, not "+". requests encodes a literal "+" as %2B which
        # openFDA throws a 500 on. Spaces become + on the wire, which the
        # API parses correctly. (See src/risk_sources.py for the precedent.)
        search = (
            f"{SEARCH_BASE} AND date_received:["
            f"{d_from} TO {d_to}]"
        )
        log.info("Bucket %s..%s — querying openFDA", d_from, d_to)

        batch = []
        for row in client.iter_events(search=search,
                                       limit=(args.limit - total) if args.limit else None):
            batch.append(row)
            if len(batch) >= 500:
                if not args.dry_run:
                    n = db.upsert_fda_events(conn, batch)
                    log.info("  → upserted %d events", n)
                total += len(batch)
                batch = []
                if args.limit and total >= args.limit:
                    break
        if batch:
            if not args.dry_run:
                n = db.upsert_fda_events(conn, batch)
                log.info("  → upserted %d events (tail)", n)
            total += len(batch)

    log.info("DONE — pulled %d AXIOS events%s",
             total, " (dry-run, no inserts)" if args.dry_run else "")


if __name__ == "__main__":
    main()
