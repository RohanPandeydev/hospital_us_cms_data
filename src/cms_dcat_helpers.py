"""Tiny helpers for data.cms.gov DCAT-1.1 catalog ingestion.

Resolve a dataset's latest CSV download URL by exact-title match against the
CMS catalog at https://data.cms.gov/data.json. Stream + parse the CSV row by
row. The per-source ingester maps CSV columns to ClickHouse tuples.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Callable, Iterable

import requests

log = logging.getLogger(__name__)

DCAT_URL = "https://data.cms.gov/data.json"


def resolve_csv_url(dataset_title: str,
                    filename_hint: str | None = None,
                    prefer_newest: bool = True) -> str:
    """Return one CSV download URL for a dataset by exact title match.

    If multiple distributions exist (older snapshots), `filename_hint` filters
    to a substring; otherwise we pick the lexicographically latest CSV URL.
    """
    r = requests.get(DCAT_URL, timeout=120)
    r.raise_for_status()
    candidates: list[str] = []
    for item in r.json().get("dataset", []):
        if item.get("title", "").strip().lower() != dataset_title.lower():
            continue
        for dist in item.get("distribution", []):
            url = dist.get("downloadURL") or dist.get("accessURL", "")
            if not url or not url.endswith(".csv"):
                continue
            if filename_hint and filename_hint not in url:
                continue
            candidates.append(url)
    if not candidates:
        raise RuntimeError(
            f"No CSV download for dataset '{dataset_title}' "
            f"(hint={filename_hint!r}) in DCAT catalog"
        )
    chosen = max(candidates) if prefer_newest else candidates[0]
    log.info("Resolved '%s' → %s", dataset_title, chosen)
    return chosen


def download_csv(url: str, timeout: int = 600) -> bytes:
    log.info("Downloading %s", url)
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("Downloaded %d bytes", len(r.content))
    return r.content


def iter_dict_rows(csv_bytes: bytes) -> Iterable[dict]:
    """Yield CSV rows as dicts. Strips BOM, tolerates encoding errors."""
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    yield from reader
