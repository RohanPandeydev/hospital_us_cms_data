"""Medicare Opt-Out Affidavits ingester.

Source: data.cms.gov DCAT-1.1 catalog → "Opt Out Affidavits" dataset.
Currently ~55K rows / ~3 MB CSV. Refreshes monthly.

Each row = provider who formally opted out of Medicare. They cannot bill
Medicare for ANY service, so device manufacturers cannot drive Medicare
revenue through them. Useful negative-filter for sales targeting.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import logging

import requests

from . import db

log = logging.getLogger(__name__)

DCAT_URL = "https://data.cms.gov/data.json"
DATASET_TITLE = "Opt Out Affidavits"
DATASET = {"id": "opt_out_affidavits", "name": DATASET_TITLE}

COLS = [
    "npi", "optout_effective",
    "first_name", "last_name", "specialty",
    "optout_effective_date", "optout_end_date", "last_updated",
    "first_name_alias",
    "address_line1", "address_line2", "city", "state", "zip",
    "source_url", "raw",
]

COL_TYPES = [
    "String", "String",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "String",
]


def _iso(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def discover_csv_url() -> str:
    """Resolve the latest Opt-Out Affidavits CSV download URL from CMS DCAT."""
    log.info("Resolving %s URL from DCAT catalog", DATASET_TITLE)
    r = requests.get(DCAT_URL, timeout=120)
    r.raise_for_status()
    catalog = r.json()
    for item in catalog.get("dataset", []):
        if item.get("title", "").strip().lower() == DATASET_TITLE.lower():
            for dist in item.get("distribution", []):
                url = dist.get("downloadURL") or dist.get("accessURL")
                mt = dist.get("mediaType", "")
                if url and "csv" in mt:
                    log.info("Resolved → %s", url)
                    return url
    raise RuntimeError(f"Could not find CSV download for '{DATASET_TITLE}' in DCAT")


def download_csv(url: str) -> bytes:
    log.info("Downloading %s", url)
    r = requests.get(url, timeout=300, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("Downloaded %d bytes", len(r.content))
    return r.content


def parse_rows(csv_bytes: bytes, source_url: str):
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        npi = (r.get("npi") or r.get("NPI") or "").strip()
        if not npi:
            continue
        eff_iso = _iso(r.get("Optout Effective Date"))
        yield (
            npi,
            eff_iso or "",
            r.get("First Name") or None,
            r.get("Last Name") or None,
            r.get("Specialty") or None,
            eff_iso,
            _iso(r.get("Optout End Date")),
            _iso(r.get("Last updated")),
            None,  # first_name_alias not in source
            r.get("First Line Street Address") or None,
            r.get("Second Line Street Address") or None,
            r.get("City Name") or None,
            r.get("State Code") or None,
            r.get("Zip code") or None,
            source_url,
            json.dumps(r, ensure_ascii=False),
        )


def ingest(batch_size: int = 5000, limit: int | None = None) -> tuple[int, int]:
    url = discover_csv_url()
    payload = download_csv(url)
    rows = list(parse_rows(payload, url))
    if limit:
        rows = rows[:limit]

    # Dedup by (npi, optout_effective)
    seen: dict[tuple[str, str], tuple] = {}
    for r in rows:
        seen[(r[0], r[1])] = r
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
                conn.insert("medicare_opt_out", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("Opt-Out upserted %d rows", upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            log.exception("Opt-Out ingest failed")
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    f, u = ingest()
    print(f"Done. fetched={f}, upserted={u}")
