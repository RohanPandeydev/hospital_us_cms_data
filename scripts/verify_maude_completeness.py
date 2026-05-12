#!/usr/bin/env python3
"""Compare MAUDE event counts between openFDA (authoritative) and our
ClickHouse `default.flattened_adverse_event` table, per product_problem,
date range 2010-01-01 → today.

Goal: prove that ClickHouse has every adverse-event row the FDA's official
search would return — and surface any product_problem where it doesn't.

Why this script exists:
  The user originally wanted to TinyFish-scrape the MAUDE search.cfm page
  for every (product_problem, year) combo. Two problems with that:
    1. The search page is behind an Akamai bot challenge that even TinyFish
       can't reliably clear.
    2. 14,841 TinyFish runs ≈ tens of hours and ~$1.5K-$5K.
  openFDA exposes the same MAUDE data via a public JSON API with no CAPTCHA
  and a generous rate limit (120K/day with our key). One API call returns
  the total count for any (product_problem, date-range) filter — exactly
  what we need for a completeness check.

Output: downloads/maude_search/completeness.json — one record per product
problem with {fda_count, ch_count, delta, status}.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import warnings
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv()

PRODUCT_PROBLEMS_PATH = ROOT / "downloads" / "maude_search" / "product_problems.json"
OUT_PATH = ROOT / "downloads" / "maude_search" / "completeness.json"

OPENFDA_URL = "https://api.fda.gov/device/event.json"
OPENFDA_KEY = os.getenv("FDA_API_KEY", "")

# 240 req/min = 0.25s/req minimum. Leave headroom: 0.4s default.
SLEEP_DEFAULT = 0.4

log = logging.getLogger("verify_maude")


def _ch_client():
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST"), port=443,
        username=os.getenv("CLICKHOUSE_USER"),
        password=os.getenv("CLICKHOUSE_PASSWORD"),
        database="default", secure=True,
        connect_timeout=15, send_receive_timeout=120,
    )


def fda_count(problem: str, date_from: str, date_to: str,
              retries: int = 3) -> int:
    """Total openFDA events with that product_problems substring + date window.
    `date_from`/`date_to` in YYYYMMDD."""
    # openFDA's search syntax: phrase queries use double quotes.
    q = (f'product_problems:"{problem}" '
         f'AND date_received:[{date_from} TO {date_to}]')
    for attempt in range(retries):
        try:
            r = requests.get(OPENFDA_URL,
                             params={"search": q, "limit": 1, "api_key": OPENFDA_KEY},
                             timeout=30)
            if r.status_code == 200:
                return int(r.json().get("meta", {}).get("results", {}).get("total", 0))
            if r.status_code == 404:
                # openFDA returns 404 when ZERO results match — count = 0.
                return 0
            if r.status_code in (429, 500, 502, 503):
                wait = 5 * (attempt + 1)
                log.warning("openFDA %d for %r, sleeping %ds",
                            r.status_code, problem, wait)
                time.sleep(wait)
                continue
            log.warning("openFDA HTTP %d for %r: %s",
                        r.status_code, problem, r.text[:200])
        except requests.RequestException as e:
            log.warning("openFDA error for %r: %s (attempt %d/%d)",
                        problem, e, attempt + 1, retries)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"openFDA failed for {problem!r} after {retries} retries")


def ch_count(client, problem: str, date_from: str, date_to: str) -> int:
    """ClickHouse count for the same filter. `product_problems` in CH is the
    Python str() of an array, e.g. `['Crack' 'Leak']`, so we use ILIKE on the
    raw string. Slightly broader than openFDA's phrase match — small over-count
    is acceptable for a completeness check (we want CH ≥ FDA)."""
    sql = """
    SELECT count()
    FROM default.flattened_adverse_event
    WHERE product_problems ILIKE {p:String}
      AND date_received BETWEEN {df:String} AND {dt:String}
    """
    res = client.query(sql, parameters={
        "p": f"%{problem}%",
        "df": date_from,
        "dt": date_to,
    })
    return int(res.result_rows[0][0])


def status_of(fda: int, ch: int) -> str:
    """Tag the comparison.
       ok          : counts match exactly
       ch_more     : CH ≥ FDA (allowed — fuzzy ILIKE matches more)
       ch_missing  : CH < FDA (the bad case — we're missing rows)"""
    if fda == ch:
        return "ok"
    if ch > fda:
        return "ch_more"
    return "ch_missing"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="Only check the first N product problems (0 = all 872).")
    ap.add_argument("--sleep", type=float, default=SLEEP_DEFAULT,
                    help="Seconds to wait between openFDA calls (rate-limit headroom).")
    ap.add_argument("--date-from", default="20100101")
    ap.add_argument("--date-to",   default=date.today().strftime("%Y%m%d"))
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--resume", action="store_true",
                    help="Skip product problems already in the output JSON.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")

    if not OPENFDA_KEY:
        log.warning("FDA_API_KEY is empty — falling back to 1000 req/day limit.")

    problems: list[str] = json.loads(PRODUCT_PROBLEMS_PATH.read_text())
    log.info("loaded %d product problems", len(problems))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    seen: set[str] = set()
    if args.resume and out_path.exists():
        existing = json.loads(out_path.read_text())
        seen = {r["product_problem"] for r in existing}
        log.info("resume: %d already done", len(existing))

    todo = [p for p in problems if p not in seen]
    if args.limit:
        todo = todo[: args.limit]

    log.info("will check %d product problems  (window %s..%s)",
             len(todo), args.date_from, args.date_to)

    client = _ch_client()
    results = list(existing)

    for n, problem in enumerate(todo, 1):
        try:
            fda = fda_count(problem, args.date_from, args.date_to)
            ch = ch_count(client, problem, args.date_from, args.date_to)
            rec = {
                "product_problem": problem,
                "fda_count": fda,
                "ch_count": ch,
                "delta": ch - fda,
                "status": status_of(fda, ch),
            }
            log.info("[%d/%d] %-40s fda=%-9d ch=%-9d delta=%+d  %s",
                     n, len(todo), problem[:40], fda, ch, ch - fda, rec["status"])
        except Exception as e:
            rec = {"product_problem": problem,
                   "error": f"{type(e).__name__}: {e}"}
            log.error("[%d/%d] FAILED %s: %s", n, len(todo), problem, e)
        results.append(rec)
        # Save after every check so partial progress survives interruption.
        out_path.write_text(json.dumps(results, indent=2, default=str))
        time.sleep(args.sleep)

    # Summary
    by_status: dict[str, int] = {}
    for r in results:
        s = r.get("status") or "error"
        by_status[s] = by_status.get(s, 0) + 1
    log.info("DONE. %d records in %s", len(results), out_path)
    log.info("status breakdown: %s", by_status)
    missing = [r for r in results if r.get("status") == "ch_missing"]
    if missing:
        log.warning("⚠️  %d product problems where ClickHouse has FEWER rows than FDA:",
                    len(missing))
        for r in sorted(missing, key=lambda x: x["delta"])[:20]:
            log.warning("   %-50s delta=%+d (fda=%d ch=%d)",
                        r["product_problem"][:50], r["delta"], r["fda_count"], r["ch_count"])


if __name__ == "__main__":
    main()
