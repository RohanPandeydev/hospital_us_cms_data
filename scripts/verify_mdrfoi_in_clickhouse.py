#!/usr/bin/env python3
"""Diff: every MDR_REPORT_KEY in S3 `us-data/maude-data/mdrfoi/partition=*/`
should also exist in ClickHouse `default.flattened_adverse_event`.

Strategy:
  1. DuckDB reads the S3 mdrfoi parquets (httpfs extension) and writes the
     distinct MDR_REPORT_KEYs to a local parquet (~4M rows, cached for resume).
  2. We pull all distinct mdr_report_keys from ClickHouse into a second local
     parquet. (Doing this server-side and streaming is far cheaper than
     batching 4M keys back through `IN (…)` over HTTP — that hit a 414
     URI-too-long error in the first attempt.)
  3. DuckDB anti-joins the two parquets locally to find S3 keys missing from CH.
  4. For the missing keys, DuckDB joins back to the S3 parquets to pull each
     full row, and we write them to JSON.

Output: downloads/mdrfoi_diff/missing.json — full records of any reports
that exist in S3 mdrfoi but not in ClickHouse.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import warnings
from pathlib import Path

import duckdb
from dotenv import load_dotenv

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv()

OUT_DIR = ROOT / "downloads" / "mdrfoi_diff"
OUT_PATH = OUT_DIR / "missing.json"
KEYS_PARQUET = OUT_DIR / "mdrfoi_keys.parquet"
CH_KEYS_PARQUET = OUT_DIR / "ch_keys.parquet"

S3_GLOB = "s3://{bucket}/us-data/maude-data/mdrfoi/partition=*/*.parquet"

log = logging.getLogger("verify_mdrfoi")


def _ch_client():
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST"), port=443,
        username=os.getenv("CLICKHOUSE_USER"),
        password=os.getenv("CLICKHOUSE_PASSWORD"),
        database="default", secure=True,
        connect_timeout=15, send_receive_timeout=300,
    )


def _duck_with_s3() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        "SET s3_region='us-east-1';"
        f"SET s3_access_key_id='{os.getenv('AWS_ACCESS_KEY_ID')}';"
        f"SET s3_secret_access_key='{os.getenv('AWS_SECRET_ACCESS_KEY')}';"
    )
    return con


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def dump_ch_keys_to_parquet(out_path: Path) -> int:
    """Stream all distinct mdr_report_key from ClickHouse into a local parquet
    via Arrow. Cheaper and safer than batching keys back through `IN (…)`."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    ch = _ch_client()
    log.info("streaming distinct mdr_report_key from ClickHouse → %s", out_path)
    t0 = time.time()
    # query_arrow returns a single Arrow table — fine for ~24M small strings (~250MB).
    table = ch.query_arrow(
        "SELECT DISTINCT mdr_report_key AS k "
        "FROM default.flattened_adverse_event "
        "WHERE mdr_report_key IS NOT NULL AND mdr_report_key != ''"
    )
    pq.write_table(table, out_path, compression="zstd")
    log.info("wrote %d rows in %.1fs (%.1f MB)",
             table.num_rows, time.time() - t0,
             out_path.stat().st_size / 1024 / 1024)
    return table.num_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=10000,
                    help="Keys per CH probe (default 10k).")
    ap.add_argument("--use-cached-keys", action="store_true",
                    help="Skip the S3 scan and reuse the cached keys parquet.")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bucket = os.getenv("AWS_BUCKET")

    con = _duck_with_s3()

    # ── Step 1 ───────────────────────────────────────────────────────────
    if args.use_cached_keys and KEYS_PARQUET.exists():
        log.info("using cached keys file: %s", KEYS_PARQUET)
        # Cached parquet column may be `k` (from prior alias) or MDR_REPORT_KEY.
        cols = [c[0] for c in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{KEYS_PARQUET}')"
        ).fetchall()]
        col = "k" if "k" in cols else "MDR_REPORT_KEY"
        con.execute(
            f"CREATE OR REPLACE TABLE mdrfoi_keys AS "
            f"SELECT DISTINCT {col} AS k FROM read_parquet('{KEYS_PARQUET}')"
        )
    else:
        s3_glob = S3_GLOB.format(bucket=bucket)
        log.info("scanning S3 mdrfoi parquets for distinct MDR_REPORT_KEY…")
        t0 = time.time()
        con.execute(
            "CREATE OR REPLACE TABLE mdrfoi_keys AS "
            f"SELECT DISTINCT MDR_REPORT_KEY AS k FROM read_parquet('{s3_glob}') "
            "WHERE MDR_REPORT_KEY IS NOT NULL AND MDR_REPORT_KEY != ''"
        )
        # Cache to disk for resume.
        con.execute(f"COPY mdrfoi_keys TO '{KEYS_PARQUET}' (FORMAT PARQUET)")
        log.info("S3 scan done in %.1fs", time.time() - t0)

    n_keys = con.execute("SELECT count(*) FROM mdrfoi_keys").fetchone()[0]
    log.info("distinct mdrfoi MDR_REPORT_KEYs: %d", n_keys)

    # ── Step 2: dump CH keys to local parquet ───────────────────────────
    if args.use_cached_keys and CH_KEYS_PARQUET.exists():
        log.info("using cached CH keys file: %s", CH_KEYS_PARQUET)
    else:
        dump_ch_keys_to_parquet(CH_KEYS_PARQUET)

    # ── Step 3: anti-join in DuckDB ─────────────────────────────────────
    log.info("anti-joining S3 keys vs CH keys (in-process)")
    t0 = time.time()
    con.execute(
        f"CREATE OR REPLACE TABLE ch_keys AS "
        f"SELECT k FROM read_parquet('{CH_KEYS_PARQUET}')"
    )
    con.execute("CREATE INDEX IF NOT EXISTS ch_keys_idx ON ch_keys(k)")
    missing_rows = con.execute(
        "SELECT s.k FROM mdrfoi_keys s "
        "ANTI JOIN ch_keys c ON s.k = c.k"
    ).fetchall()
    missing = [r[0] for r in missing_rows]
    log.info("anti-join done in %.1fs. missing count: %d / %d",
             time.time() - t0, len(missing), n_keys)

    # Save the keys list NOW so a crash in the slow fetch step doesn't lose
    # the result of the (cheap) anti-join.
    keys_only_payload = {
        "source_glob": S3_GLOB.format(bucket=bucket),
        "ch_table": "default.flattened_adverse_event",
        "mdrfoi_distinct_keys": n_keys,
        "missing_count": len(missing),
        "missing_keys": missing[:50000],
        "missing_records_count": None,   # populated after the fetch
        "missing_records": [],
    }
    out_path.write_text(json.dumps(keys_only_payload, indent=2, default=str))
    log.info("checkpoint: saved missing-key list (%d keys) → %s",
             len(missing), out_path)

    # ── Step 3: fetch full S3 rows for the missing keys ─────────────────
    missing_records: list[dict] = []
    if missing:
        log.info("fetching full S3 records for %d missing keys…", len(missing))
        con.execute("CREATE OR REPLACE TABLE missing_keys (k VARCHAR)")
        con.executemany("INSERT INTO missing_keys VALUES (?)",
                        [(k,) for k in missing])
        s3_glob = S3_GLOB.format(bucket=bucket)
        rows = con.execute(f"""
            SELECT *
            FROM read_parquet('{s3_glob}') p
            JOIN missing_keys m ON p.MDR_REPORT_KEY = m.k
        """).fetchdf()
        for _, row in rows.iterrows():
            rec = {}
            for c in rows.columns:
                v = row[c]
                if v is None or (isinstance(v, float) and v != v):
                    rec[c] = None
                elif hasattr(v, "isoformat"):
                    rec[c] = v.isoformat()
                else:
                    rec[c] = str(v)
            missing_records.append(rec)
        log.info("fetched %d full record(s) for %d missing keys",
                 len(missing_records), len(missing))

    # ── Final save ─────────────────────────────────────────────────────
    out = {
        "source_glob": S3_GLOB.format(bucket=bucket),
        "ch_table": "default.flattened_adverse_event",
        "mdrfoi_distinct_keys": n_keys,
        "missing_count": len(missing),
        "missing_keys": missing[:50000],
        "missing_records_count": len(missing_records),
        "missing_records": missing_records,
    }
    out_path.write_text(json.dumps(out, indent=2, default=str))
    log.info("saved → %s  (missing_records=%d)", out_path, len(missing_records))


if __name__ == "__main__":
    main()
