"""FDA Device Classification ingester.

Source: https://api.fda.gov/device/classification.json — the authoritative
product_code → device_class/specialty/regulation master list. One row per
product_code, ~7000 total. We ingest all of it (it's small and rarely
changes) so the risk pipeline has authoritative regulatory metadata for any
product code that surfaces in MAUDE, 510(k), or recalls.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Iterator

import psycopg2.extras

log = logging.getLogger(__name__)

_ENDPOINT = "https://api.fda.gov/device/classification.json"
_PAGE_SIZE = 1000  # openFDA max


def iter_classification(limit: int | None = None) -> Iterator[dict]:
    skip = 0
    yielded = 0
    while True:
        qs = urllib.parse.urlencode({"limit": _PAGE_SIZE, "skip": skip})
        url = f"{_ENDPOINT}?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "risk-pipeline"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.load(resp)
        results = body.get("results") or []
        total = body.get("meta", {}).get("results", {}).get("total", 0)
        if not results:
            return
        for r in results:
            yield r
            yielded += 1
            if limit and yielded >= limit:
                return
        skip += len(results)
        if skip >= total:
            return
        time.sleep(0.2)


def upsert(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO fda_device_classification (
            product_code, device_name, device_class, regulation_number,
            medical_specialty, medical_specialty_description, review_panel,
            submission_type_id, definition, life_sustain_support_flag,
            implant_flag, third_party_flag, gmp_exempt_flag, raw
        ) VALUES %s
        ON CONFLICT (product_code) DO UPDATE SET
            device_name = EXCLUDED.device_name,
            device_class = EXCLUDED.device_class,
            regulation_number = EXCLUDED.regulation_number,
            medical_specialty = EXCLUDED.medical_specialty,
            medical_specialty_description = EXCLUDED.medical_specialty_description,
            review_panel = EXCLUDED.review_panel,
            submission_type_id = EXCLUDED.submission_type_id,
            definition = EXCLUDED.definition,
            life_sustain_support_flag = EXCLUDED.life_sustain_support_flag,
            implant_flag = EXCLUDED.implant_flag,
            third_party_flag = EXCLUDED.third_party_flag,
            gmp_exempt_flag = EXCLUDED.gmp_exempt_flag,
            raw = EXCLUDED.raw,
            fetched_at = now()
    """
    values = []
    for r in rows:
        pc = (r.get("product_code") or "").strip()
        if not pc:
            continue
        values.append((
            pc,
            r.get("device_name"),
            r.get("device_class"),
            r.get("regulation_number"),
            r.get("medical_specialty"),
            r.get("medical_specialty_description"),
            r.get("review_panel"),
            r.get("submission_type_id"),
            r.get("definition"),
            r.get("life_sustain_support_flag"),
            r.get("implant_flag"),
            r.get("third_party_flag"),
            r.get("gmp_exempt_flag"),
            json.dumps(r, default=str),
        ))
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, values, page_size=500)
    return len(values)


def run(conn, limit: int | None = None, batch: int = 1000) -> int:
    buf: list[dict] = []
    total = 0
    for r in iter_classification(limit=limit):
        buf.append(r)
        if len(buf) >= batch:
            total += upsert(conn, buf)
            buf = []
            log.info("  FDA classification: %d rows upserted", total)
    if buf:
        total += upsert(conn, buf)
    log.info("FDA classification ingest done: %d rows", total)
    return total
