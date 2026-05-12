#!/usr/bin/env python3
"""Build bridge_maude_to_ccn — the missing CCN dimension on every MAUDE event.

MAUDE has manufacturer + brand + facility_name + facility_state + facility_zip,
but no CCN. CMS hospitals are keyed by CCN. Without this bridge there's no
single dataset with manufacturer + device + CCN in one row.

Multi-pass strategy (highest confidence first; ReplacingMergeTree on
report_number means later passes don't overwrite higher-confidence matches):

  PASS 1 — exact normalized name + state          → confidence='high'
  PASS 2 — ngramSimilarity(name) >= 0.85 + state  → confidence='medium'
  PASS 3 — ngramSimilarity(name) >= 0.65 + state  → confidence='low'

Normalization: uppercase, strip everything that isn't [A-Z0-9].

After it runs, the all-three row for AXIOS:
    SELECT d.manufacturer, d.brand_name,
           b.facility_id AS ccn, h.facility_name, h.state
    FROM fda_maude_devices d FINAL
    JOIN bridge_maude_to_ccn b FINAL ON b.report_number = d.report_number
    JOIN cms_hospitals h FINAL ON h.facility_id = b.facility_id
    WHERE d.brand_name ILIKE '%axios%';
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Normalize a column expression: uppercase + strip non-alphanumerics.
def norm(col: str) -> str:
    return f"upperUTF8(replaceRegexpAll(ifNull({col}, ''), '[^A-Za-z0-9]+', ''))"


def _client():
    from src import config
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=config.CLICKHOUSE_HOST, port=config.CLICKHOUSE_PORT,
        username=config.CLICKHOUSE_USER, password=config.CLICKHOUSE_PASSWORD,
        database=config.CLICKHOUSE_DATABASE, secure=config.CLICKHOUSE_SECURE,
        connect_timeout=15, send_receive_timeout=600,
    )


def insert_exact(client, dry_run: bool) -> int:
    """PASS 1 — strict normalized-exact name + state."""
    sql = f"""
        INSERT INTO bridge_maude_to_ccn
          (report_number, facility_id, facility_name_maude, facility_name_ccn,
           state, score, match_method, confidence)
        SELECT e.report_number,
               h.facility_id,
               e.facility_name,
               h.facility_name,
               h.state,
               1.0,
               'exact_name_state',
               'high'
        FROM fda_maude_events e FINAL
        INNER JOIN cms_hospitals h FINAL
                ON {norm('e.facility_name')} = {norm('h.facility_name')}
               AND upperUTF8(e.facility_state) = upperUTF8(h.state)
        WHERE e.facility_name IS NOT NULL
          AND e.facility_state IS NOT NULL
          AND length({norm('e.facility_name')}) >= 6
    """
    return _run(client, sql, "exact_name_state", dry_run)


def insert_ngram(client, threshold: float, confidence: str, dry_run: bool) -> int:
    """PASS 2/3 — fuzzy match within state, take best CCN per report.

    Skips report_numbers already matched by an earlier pass."""
    sql = f"""
        INSERT INTO bridge_maude_to_ccn
          (report_number, facility_id, facility_name_maude, facility_name_ccn,
           state, score, match_method, confidence)
        SELECT report_number,
               argMax(facility_id, sim),
               argMax(f_maude,    sim),
               argMax(f_ccn,      sim),
               argMax(state,      sim),
               max(sim),
               'ngram_state',
               '{confidence}'
        FROM (
          SELECT e.report_number,
                 h.facility_id,
                 e.facility_name AS f_maude,
                 h.facility_name AS f_ccn,
                 h.state         AS state,
                 ngramSimilarityUTF8({norm('e.facility_name')},
                                     {norm('h.facility_name')}) AS sim
          FROM fda_maude_events e FINAL
          INNER JOIN cms_hospitals h FINAL
                  ON upperUTF8(e.facility_state) = upperUTF8(h.state)
          WHERE e.facility_name IS NOT NULL
            AND e.facility_state IS NOT NULL
            AND length({norm('e.facility_name')}) >= 6
            AND e.report_number NOT IN (
              SELECT report_number FROM bridge_maude_to_ccn FINAL
            )
        )
        WHERE sim >= {threshold}
        GROUP BY report_number
    """
    return _run(client, sql, f"ngram@{threshold}", dry_run)


def _run(client, insert_sql: str, label: str, dry_run: bool) -> int:
    if dry_run:
        # Count rows the inner SELECT would return.
        count_sql = "SELECT count() FROM (\n" + \
            insert_sql.split("FROM", 1)[1].rsplit("GROUP BY", 1)[0] + "\n) _"
        try:
            n = client.query(count_sql).result_rows[0][0]
        except Exception:
            n = -1
        logging.info("[dry-run] %s would insert ~%d rows", label, n)
        return n
    res = client.query(insert_sql)
    written = res.summary.get("written_rows") if hasattr(res, "summary") else 0
    logging.info("%s — wrote %s rows", label, written)
    return int(written or 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="pass_no", type=int, choices=[0, 1, 2, 3],
                    default=0, help="Run only one pass (default 0 = all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Count what would be inserted; no writes.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")
    log = logging.getLogger("maude_ccn")
    client = _client()

    n_events = client.query(
        "SELECT count() FROM fda_maude_events FINAL "
        "WHERE facility_name IS NOT NULL"
    ).result_rows[0][0]
    n_hosp = client.query(
        "SELECT count() FROM cms_hospitals FINAL"
    ).result_rows[0][0]
    log.info("Source: %d MAUDE events with facility_name, %d CMS hospitals",
             n_events, n_hosp)
    if n_events == 0 or n_hosp == 0:
        log.error("Source table empty — nothing to bridge.")
        return

    if args.pass_no in (0, 1):
        insert_exact(client, args.dry_run)
    if args.pass_no in (0, 2):
        insert_ngram(client, threshold=0.85, confidence="medium",
                     dry_run=args.dry_run)
    if args.pass_no in (0, 3):
        insert_ngram(client, threshold=0.65, confidence="low",
                     dry_run=args.dry_run)

    if not args.dry_run:
        cov = client.query("""
            SELECT confidence, count() FROM bridge_maude_to_ccn FINAL
            GROUP BY confidence ORDER BY confidence
        """).result_rows
        matched = client.query(
            "SELECT count() FROM bridge_maude_to_ccn FINAL"
        ).result_rows[0][0]
        log.info("Coverage by confidence: %s", cov)
        log.info("Total: %d / %d MAUDE events bridged (%.1f%%)",
                 matched, n_events, 100.0 * matched / max(n_events, 1))


if __name__ == "__main__":
    main()
