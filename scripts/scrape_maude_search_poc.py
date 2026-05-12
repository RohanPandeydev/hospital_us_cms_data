#!/usr/bin/env python3
"""TinyFish proof-of-concept for FDA MAUDE search-form scraping.

Goal:
  For ONE product problem, run the FDA MAUDE search at
    https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfmaude/search.cfm
  with date range 01/01/2010 → today, then find and capture the
  "Download" / "Export" link the results page exposes.

This POC validates a single iteration before scaling to all 873 product
problems (~24 hours of TinyFish runs and proportional cost).

The search page is behind an Akamai bot challenge so plain HTTP cannot
submit the form — TinyFish drives a real browser that solves the challenge.
"""

import json
import logging
import sys
import warnings
from datetime import date
from pathlib import Path

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import tinyfish_client  # noqa: E402


SEARCH_URL = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfmaude/search.cfm"

POC_PRODUCT_PROBLEM = "crack"
POC_DATE_FROM = "01/01/2010"
POC_DATE_TO = date.today().strftime("%m/%d/%Y")


GOAL_TEMPLATE = """
You are on the FDA MAUDE search page (the form titled "MAUDE Search" or "Search Database")
at https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfmaude/search.cfm

Step 1. In the "Product Problem" dropdown, select the option whose visible text matches
        exactly: "{product_problem}"
Step 2. In the "Date Report Received by FDA (mm/dd/yyyy)" range, type:
          From: {date_from}
          To:   {date_to}
Step 3. Set the "Records per Page" / "pagenum" dropdown to its largest option (500).
Step 4. Click the "Search" button and wait for the results page to fully render.

Step 5. On the results page, look CAREFULLY for any link, button, or icon labeled:
          "Download Files"  /  "Download to Excel"  /  "Export"  /  "CSV"  /
          "Download All"    /  "Export to Excel"   /  "Save"   /  "XLS"   /
          or a small download icon (a downward arrow).
        It is usually near the top-right of the results, sometimes under a "More" menu.
Step 6. If you find such a link, capture its href URL into `download_url`.
        If you have to click it to obtain the URL (e.g. it triggers a POST or JS),
        click it and capture whichever URL the browser lands on or initiates.
        If clicking actually downloads a file, capture the file's source URL.

Return EXACTLY ONE JSON object with these keys:
  - result_count            : integer total of results shown on the page (parse from text like "1234 results found"). null if not found.
  - download_url            : string URL of the export/download button, or null if no such button exists.
  - download_button_label   : exact visible text of the download button, or null.
  - sample_columns          : list of column header strings from the results table.
  - sample_rows             : the FIRST 5 rows of the results table, each as an object keyed by column header (lowercased, snake_case).
  - notes                   : short string describing anything unusual you saw (e.g. "results capped at 500", "no export button visible").

No prose outside the JSON.
""".strip()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")
    out_path = ROOT / "downloads" / "maude_search" / "poc.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    log = logging.getLogger("maude_poc")
    log.info("Single POC: product_problem=%r, dates=%s..%s",
             POC_PRODUCT_PROBLEM, POC_DATE_FROM, POC_DATE_TO)

    goal = GOAL_TEMPLATE.format(
        product_problem=POC_PRODUCT_PROBLEM,
        date_from=POC_DATE_FROM,
        date_to=POC_DATE_TO,
    )
    out = tinyfish_client.run(SEARCH_URL, goal, timeout=900)

    rec = {
        "query": {
            "product_problem": POC_PRODUCT_PROBLEM,
            "date_from": POC_DATE_FROM,
            "date_to": POC_DATE_TO,
        },
        "run_id": out.get("run_id"),
        "raw_result": out.get("result"),
    }
    out_path.write_text(json.dumps(rec, indent=2, default=str))
    log.info("Saved POC output to %s", out_path)
    # Surface the key signals so the run-log shows them:
    inner = (out.get("result") or {}).get("result") or out.get("result") or {}
    if isinstance(inner, dict):
        log.info("result_count   = %s", inner.get("result_count"))
        log.info("download_url   = %s", inner.get("download_url"))
        log.info("button_label   = %s", inner.get("download_button_label"))
        log.info("sample_columns = %s", inner.get("sample_columns"))
        log.info("notes          = %s", inner.get("notes"))


if __name__ == "__main__":
    main()
