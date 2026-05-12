#!/usr/bin/env python3
"""Use TinyFish to fetch bulk CSV URLs for the seven CMS data-api datasets
that are currently Akamai-blocked from this network.

The data-api itself (data.cms.gov/data-api/v1/dataset/{uuid}) times out
every request, including `/data-viewer/stats` which would normally give us
the direct CSV URL. The CMS *landing page* for each dataset is on plain
data.cms.gov (different CDN, not blocked) and links to the same CSV. We
use TinyFish to visit the landing page, click "Data API" / "Data" tab,
and capture the CSV href.

For each registered cms_data_api dataset:
  1. Resolve the landing URL: data.cms.gov/data-viewer/{slug}?id={uuid}
  2. Ask TinyFish to extract the "Download CSV" / Data Dictionary CSV URL
  3. Save {dataset_id, year, csv_url, total_rows} → downloads/cms_api/{dataset_id}.json
  4. (optional) Stream the CSV to disk so the ingest pipeline can run
     `python run.py ingest --dataset {dataset_id} --csv-path <local>`.

Run:
    python scripts/fetch_cms_api_csvs_tinyfish.py --discover-only
    python scripts/fetch_cms_api_csvs_tinyfish.py --download
    python scripts/fetch_cms_api_csvs_tinyfish.py --dataset medicare_outpatient_by_provider_service

Requires TINYFISH_API_KEY in .env.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import tinyfish_client  # noqa: E402
from src.datasets import DATASETS  # noqa: E402

OUT_DIR = ROOT / "downloads" / "cms_api"
OUT_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("cms_csv_tf")


def landing_url(uuid: str) -> str:
    """CMS data.cms.gov dataset landing page. The {slug} segment is
    cosmetic — only the `?id=` query param drives content. Using a
    generic slug keeps the URL robust across dataset renames."""
    return f"https://data.cms.gov/data-viewer/?id={uuid}"


GOAL_TEMPLATE = """
You are on a CMS data.cms.gov dataset landing page.

Step 1. Wait for the page to load fully. The page has a "Data" tab that
        shows a table preview plus download options.
Step 2. Locate the bulk download link. It is typically labeled
        "Download CSV", "Download Full Dataset", or "Data API" → bulk file.
        The href usually ends in `.csv` or points at /sites/default/files/.
Step 3. Capture:
        - csv_url: the absolute URL of the bulk CSV download
        - row_count: the total row count if displayed (parse from text
                     like "9,663,000 rows" or "Total rows: 9663000")
        - title: the dataset title shown on the page
        - publish_date: the most recent "Updated" / "Last modified" date
        - year_label: any year/period label shown (e.g. "Calendar Year 2023")
Step 4. If the page exposes more than one CSV (some show "Annual CSV" plus
        "Data Dictionary"), prefer the largest data-file CSV — never the
        data dictionary.

Return EXACTLY ONE JSON object, no prose outside it:
  {{
    "csv_url":      <str|null>,
    "row_count":    <int|null>,
    "title":        <str|null>,
    "publish_date": <str|null>,
    "year_label":   <str|null>,
    "notes":        <str>
  }}
"""


def discover_one(dataset: dict) -> dict:
    uuid = dataset.get("uuid")
    if not uuid:
        return {"error": "dataset has no uuid; nothing to discover"}
    url = landing_url(uuid)
    log.info("TinyFish discover: %s (uuid=%s)", dataset["id"], uuid)
    ev = tinyfish_client.run(url, GOAL_TEMPLATE, timeout=600)
    result = (ev.get("result") or {}).get("result") or ev.get("result") or {}
    if not isinstance(result, dict):
        result = {"raw": result}
    result.setdefault("dataset_id", dataset["id"])
    result.setdefault("uuid", uuid)
    result.setdefault("landing_url", url)
    return result


def download_csv(csv_url: str, out_path: Path, chunk_size: int = 1_000_000):
    """Stream a CSV to disk. Returns bytes written."""
    log.info("downloading CSV → %s", out_path)
    with requests.get(csv_url, stream=True, timeout=900,
                      headers={"User-Agent": "Mozilla/5.0"}) as r:
        r.raise_for_status()
        total = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
    log.info("  wrote %.1f MB", total / 1024 / 1024)
    return total


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset",
                   help="Limit to one dataset id (default: all data-api datasets)")
    p.add_argument("--discover-only", action="store_true",
                   help="Only resolve the CSV URL — do not download")
    p.add_argument("--download", action="store_true",
                   help="Discover then stream the CSV to downloads/cms_api/")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    targets = [d for d in DATASETS if d.get("source") == "cms_data_api"]
    if args.dataset:
        targets = [d for d in targets if d["id"] == args.dataset]
        if not targets:
            print(f"ERROR: no cms_data_api dataset with id {args.dataset!r}")
            sys.exit(2)

    print(f"Discovering {len(targets)} CMS data-api dataset CSV URLs via TinyFish…")
    results = {}
    for d in targets:
        try:
            info = discover_one(d)
        except Exception as e:
            log.exception("discover failed: %s", d["id"])
            info = {"error": repr(e)}
        results[d["id"]] = info
        out_json = OUT_DIR / f"{d['id']}.json"
        out_json.write_text(json.dumps(info, indent=2, default=str))
        print(f"  {d['id']:<42} csv={info.get('csv_url')!s:<60} rows={info.get('row_count')}")

        if args.download and info.get("csv_url"):
            try:
                out_csv = OUT_DIR / f"{d['id']}.csv"
                download_csv(info["csv_url"], out_csv)
            except Exception as e:
                log.exception("download failed: %s", d["id"])

    # Summary
    have_url = sum(1 for v in results.values() if v.get("csv_url"))
    print(f"\nResolved {have_url}/{len(results)} CSV URLs.")
    print(f"Manifests in: {OUT_DIR}/")
    if args.download:
        csvs = list(OUT_DIR.glob("*.csv"))
        print(f"Downloaded {len(csvs)} CSVs.")
        print("Next: ingest each with --csv-path, e.g.")
        for d in targets:
            local = OUT_DIR / f"{d['id']}.csv"
            if local.exists():
                print(f"  python run.py ingest --dataset {d['id']} "
                      f"--csv-path {local.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
