#!/usr/bin/env python3
"""Fetch CSV URLs for the 11 CMS data-api datasets via the public catalog.

The CMS data-api at data.cms.gov/data-api/v1/dataset/{uuid}/data is
Akamai-blocked from this network. The fallback we originally tried —
scraping the dataset landing page via TinyFish — breaks because CMS
recently changed those URLs (the `/data-viewer/?id={uuid}` pattern
returns 404).

The clean alternative: CMS publishes a DCAT-1.1 JSON catalog at
data.cms.gov/data.json that lists every dataset, its identifier, and a
list of `distribution` entries each with a direct `downloadURL`. We hit
that catalog once, match by UUID, pick the newest CSV, and stream it to
downloads/cms_api/{dataset_id}.csv.

This is the right design — no TinyFish credits, no flaky scraping,
deterministic UUID → CSV-URL resolution.

Run:
    python scripts/fetch_cms_api_csvs.py                  # discover + download all
    python scripts/fetch_cms_api_csvs.py --discover-only  # just resolve URLs
    python scripts/fetch_cms_api_csvs.py --dataset medicare_outpatient_by_provider_service
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.datasets import DATASETS  # noqa: E402

OUT_DIR = ROOT / "downloads" / "cms_api"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CATALOG_URL = "https://data.cms.gov/data.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; rp360-cms-ingest/0.3)"}

log = logging.getLogger("cms_csv_catalog")


def load_catalog() -> list[dict]:
    log.info("fetching catalog %s", CATALOG_URL)
    r = requests.get(CATALOG_URL, timeout=60, headers=UA)
    r.raise_for_status()
    return r.json().get("dataset", []) or []


def find_csv(catalog: list[dict], uuid: str) -> tuple[str | None, dict | None]:
    """Find the newest CSV downloadURL for a given dataset UUID.

    The catalog identifier sometimes embeds the UUID; otherwise the
    distribution URLs themselves can contain it. We check both.
    Returns (csv_url, catalog_entry) or (None, None).
    """
    for d in catalog:
        ident = str(d.get("identifier", ""))
        dist_blob = json.dumps(d.get("distribution") or [])
        if uuid not in ident and uuid not in dist_blob:
            continue
        # Found the dataset. Pick the newest CSV — distribution is usually
        # ordered newest-first; we also prefer URLs that contain the
        # most-recent year prefix in their /sites/default/files/{YYYY-MM}/ path.
        csvs = []
        for dist in d.get("distribution") or []:
            url = dist.get("downloadURL") or dist.get("accessURL") or ""
            if not url.lower().endswith(".csv"):
                continue
            # Extract YYYY-MM from URL path for sort key (newer = higher).
            m = re.search(r"/(\d{4})-(\d{2})/", url)
            sort_key = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            csvs.append((sort_key, url))
        if not csvs:
            return None, d
        csvs.sort(reverse=True)
        return csvs[0][1], d
    return None, None


def stream_download(url: str, out_path: Path) -> int:
    """Stream a CSV to disk with progress logging every 100 MB.

    Multi-GB CSVs (physician PUF service is ~3 GB) used to log only on
    completion, making the script look frozen. The periodic log makes
    it obvious the download is alive and lets you eyeball the rate.
    """
    log.info("download → %s", out_path.name)
    bytes_written = 0
    next_log_at = 100 * 1024 * 1024  # log every 100 MB
    with requests.get(url, stream=True, timeout=900, headers=UA) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1_000_000):
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)
                    if bytes_written >= next_log_at:
                        pct = f" ({bytes_written*100//total}%)" if total else ""
                        log.info("  …%s %.0f MB%s",
                                  out_path.name, bytes_written / 1024 / 1024, pct)
                        next_log_at += 100 * 1024 * 1024
    log.info("  wrote %.1f MB → %s", bytes_written / 1024 / 1024, out_path.name)
    return bytes_written


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", help="Limit to one dataset id")
    p.add_argument("--discover-only", action="store_true",
                   help="Just resolve CSV URLs; do not download")
    p.add_argument("--force", action="store_true",
                   help="Re-download even if the CSV already exists locally")
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

    catalog = load_catalog()
    print(f"\nResolving {len(targets)} datasets against {len(catalog)} catalog entries\n")

    results = {}
    for d in targets:
        uuid = d.get("uuid")
        if not uuid:
            print(f"  ✗ {d['id']:<42} (no uuid in registry)")
            continue
        csv_url, entry = find_csv(catalog, uuid)
        title = (entry or {}).get("title", "?")
        info = {
            "dataset_id": d["id"],
            "uuid": uuid,
            "title": title,
            "csv_url": csv_url,
        }
        if csv_url:
            print(f"  ✓ {d['id']:<42} {csv_url[-60:]}")
        else:
            print(f"  ✗ {d['id']:<42} no CSV in catalog (title={title!r})")
        results[d["id"]] = info
        (OUT_DIR / f"{d['id']}.json").write_text(json.dumps(info, indent=2))

    if args.discover_only:
        print("\nDiscover-only mode — no downloads.")
        return

    print("\nDownloading (parallel, 4 concurrent)…")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    jobs: dict = {}
    skipped = 0
    for ds_id, info in results.items():
        csv_url = info.get("csv_url")
        if not csv_url:
            continue
        out_csv = OUT_DIR / f"{ds_id}.csv"
        if out_csv.exists() and not args.force:
            print(f"  ⏭ {ds_id:<42} already on disk ({out_csv.stat().st_size/1024/1024:.0f} MB)")
            skipped += 1
            continue
        jobs[ds_id] = (csv_url, out_csv)

    downloaded = skipped
    if jobs:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(stream_download, url, path): ds_id
                for ds_id, (url, path) in jobs.items()
            }
            for fut in as_completed(futures):
                ds_id = futures[fut]
                try:
                    fut.result()
                    downloaded += 1
                except Exception as e:
                    log.exception("download failed: %s", ds_id)
                    print(f"  ✗ {ds_id:<42} download failed: {e}")

    print(f"\nDownloaded/present: {downloaded}/{len(results)} CSVs.")
    print(f"Files in: {OUT_DIR}/")
    print("\nNext: ingest each CSV.  Phase 6 of run_pipeline.sh does this automatically.")
    print("To do it manually:")
    for ds_id in results:
        local = OUT_DIR / f"{ds_id}.csv"
        if local.exists():
            print(f"  python run.py ingest --dataset {ds_id} "
                  f"--csv-path {local.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
