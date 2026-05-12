#!/usr/bin/env python3
"""TinyFish scrape — AXIOS adverse-event coverage that openFDA doesn't expose.

openFDA already exposes ~2,367 AXIOS MAUDE events; use scripts/fetch_axios_maude.py
for those. This script targets sources behind JS / Akamai that openFDA doesn't:

  1. FDA accessdata MAUDE search portal
       https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfmaude/search.cfm
       — submits the form with brand="AXIOS" and captures the result count +
         any export URL the portal exposes (some date ranges show an Excel
         download not available via openFDA).
  2. FDA medical-device recalls listing
       — pulls AXIOS-named recall pages (more detail than the openFDA recalls API).
  3. Boston Scientific safety-notice page
       — manufacturer's own posted recalls / advisories for AXIOS (precedes the
         FDA recall public-database entry by days/weeks).

Output: downloads/tinyfish/axios_<source>.json — same shape as other tinyfish
runs in this project (consumed by src/tinyfish_ingest.py if you wire it).

Usage:
    python scripts/scrape_axios_tinyfish.py --source maude_search
    python scripts/scrape_axios_tinyfish.py --source recalls
    python scripts/scrape_axios_tinyfish.py --source bsc_safety
    python scripts/scrape_axios_tinyfish.py --source all
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import tinyfish_client  # noqa: E402

OUT_DIR = ROOT / "downloads" / "tinyfish"
OUT_DIR.mkdir(parents=True, exist_ok=True)


SOURCES = {
    # ── FDA MAUDE search portal (Akamai-protected) ──────────────────────
    "maude_search": {
        "url": "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfmaude/search.cfm",
        "out": "axios_maude_search.json",
        "goal": """
You are on the FDA MAUDE Search page.

Step 1. In the "Brand Name" text input, type exactly: AXIOS
Step 2. In the "Date Report Received by FDA (mm/dd/yyyy)" range, type:
          From: 01/01/2014
          To:   {today}
Step 3. Set the "Records per Page" / "pagenum" dropdown to its largest option.
Step 4. Click "Search" and wait for results.
Step 5. On the results page, capture:
          - the total result count (parse from text like "1234 results found"),
          - any "Download" / "Export" / "Excel" link href,
          - the FIRST 25 rows of the results table.

Return EXACTLY ONE JSON object:
  {{
    "result_count": <int|null>,
    "download_url": <str|null>,
    "download_button_label": <str|null>,
    "sample_columns": [<str>...],
    "sample_rows": [{{<col>: <val>}}, ... up to 25],
    "notes": <str>
  }}
No prose outside the JSON.
""",
    },

    # ── FDA medical-device recalls (HTML, not behind Akamai) ────────────
    "recalls": {
        "url": "https://www.fda.gov/medical-devices/medical-device-recalls/recalls-medical-device-product-archive",
        "out": "axios_recalls.json",
        "goal": """
You are on the FDA "Recalls of Medical Device Products" archive page.

Step 1. Use the search/filter input on the page to filter for "AXIOS".
Step 2. For every result row, capture:
          - title, recall_initiation_date, classification (Class I/II/III),
          - reason_for_recall (short text), product_code if shown,
          - the link href to the detail page,
          - manufacturer name.
Step 3. Visit the FIRST result's detail page and capture body_text + any
        related FDA links into `first_detail`.

Return EXACTLY ONE JSON object:
  {
    "result_count": <int>,
    "rows": [
      {"title":..., "recall_initiation_date":..., "classification":...,
       "reason_for_recall":..., "product_code":..., "manufacturer":..., "url":...},
      ...
    ],
    "first_detail": {"url":..., "body_text":..., "related_links":[...]},
    "notes": <str>
  }
No prose outside the JSON.
""",
    },

    # ── Boston Scientific manufacturer safety / advisory page ───────────
    "bsc_safety": {
        "url": "https://www.bostonscientific.com/en-US/customer-service/product-recalls.html",
        "out": "axios_bsc_safety.json",
        "goal": """
You are on the Boston Scientific Product Recalls / Safety Notices page.

Step 1. Find every entry whose title or body mentions "AXIOS" (case-insensitive).
Step 2. For each match, capture:
          - notice_title, notice_date, severity_label (e.g. "Urgent",
            "Field Safety Notice"), affected_product_codes/model_numbers,
            short_description, link_url to the PDF/detail.
Step 3. If a PDF is linked and openable inline, capture the first 500 chars
        of its visible text into `first_pdf_excerpt`.

Return EXACTLY ONE JSON object:
  {
    "result_count": <int>,
    "rows": [
      {"notice_title":..., "notice_date":..., "severity_label":...,
       "affected_models":..., "short_description":..., "link_url":...},
      ...
    ],
    "first_pdf_excerpt": <str|null>,
    "notes": <str>
  }
No prose outside the JSON.
""",
    },
}


def run_one(name: str):
    log = logging.getLogger("axios_tf")
    spec = SOURCES[name]
    goal = spec["goal"].format(today=date.today().strftime("%m/%d/%Y"))
    log.info("Source=%s url=%s", name, spec["url"])
    out = tinyfish_client.run(spec["url"], goal, timeout=900)
    out_path = OUT_DIR / spec["out"]
    out_path.write_text(json.dumps(out, indent=2, default=str))
    log.info("Saved → %s", out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=list(SOURCES) + ["all"], default="all")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")

    targets = list(SOURCES) if args.source == "all" else [args.source]
    for name in targets:
        run_one(name)


if __name__ == "__main__":
    main()
