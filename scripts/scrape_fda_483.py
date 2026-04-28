#!/usr/bin/env python3
"""One-shot TinyFish scrape of FDA's inspection classification database.

Goal: pull recent device-related inspections that name the firm — these
sometimes name medical facilities (not just device manufacturers), so they
are the closest free public source for "this hospital had a device problem."

Existing 30 rows in fda_483_inspection are all from a single date (2026-04-09)
and mostly equipment vendors. This run targets:
  - product_type = "Devices"
  - last 12 months of inspections
  - sorted by inspection_end_date DESC
  - up to 200 rows
"""

import json
import logging
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import tinyfish_client


URL = "https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/inspection-classification-database"

GOAL = """
You are on the FDA Inspection Classification Database page.

1. Click "Search Inspection Classification Database".
2. In the filter form, set:
   - Product Type: "Devices"
   - Inspection End Date Range: from 12 months ago to today
3. Sort by Inspection End Date DESC (most recent first).
4. Extract the first 200 rows. For each row capture exactly these fields:
   - firm_name (name of the establishment)
   - firm_city
   - firm_state
   - firm_country
   - inspection_end_date (MM/DD/YYYY)
   - posted_date (MM/DD/YYYY, may be blank)
   - product_type (should be "Devices")
   - classification (NAI / VAI / OAI)

Return as a JSON array of objects with EXACTLY those field names. Do not
include other fields. Do not include duplicate rows. Do not include any
prose — return only the JSON.
""".strip()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")
    log = logging.getLogger("scrape_fda_483")

    log.info("Submitting TinyFish run for FDA inspection database")
    out = tinyfish_client.run(URL, GOAL, timeout=900)

    out_path = ROOT / "downloads" / "tinyfish" / "fda_483_v3.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    log.info("Saved raw run output to %s", out_path)

    # Surface a count + a couple of sample rows so we know the run worked.
    result = out.get("result") or {}
    rows = result.get("result") or result.get("rows") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    log.info("Got %d rows", len(rows))
    if rows:
        log.info("Sample row 0: %s", json.dumps(rows[0])[:300])
    return rows


if __name__ == "__main__":
    main()
