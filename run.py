#!/usr/bin/env python3
"""CLI for the CMS hospital ingestion pipeline.

Examples:
    # One-time DB + schema setup
    python run.py init

    # Ingest everything
    python run.py ingest

    # Ingest one dataset
    python run.py ingest --dataset xubh-q36u

    # Smoke test: fetch only first 50 rows of a dataset
    python run.py ingest --dataset xubh-q36u --limit 50

    # List available datasets
    python run.py list

    # Show ingestion history
    python run.py status
"""

import argparse
import logging
import sys
import warnings

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from src import db
from src import bridge_seeds
from src.datasets import DATASETS
from src.ingest import ingest_all


def _setup_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )


def cmd_init(args):
    db.ensure_database()
    db.apply_schema()
    print("OK: database + schema ready")


def cmd_list(args):
    print(f"{'DATASET ID':<14} {'KIND':<18} NAME")
    print("-" * 80)
    for d in DATASETS:
        print(f"{d['id']:<14} {d['kind']:<18} {d['name']}")


def cmd_ingest(args):
    # Auto-init: makes this the single command users need to run.
    db.ensure_database()
    db.apply_schema()
    ids = [args.dataset] if args.dataset else None
    results = ingest_all(dataset_ids=ids, limit=args.limit)
    # Always refresh manual crosswalks — idempotent, fast.
    with db.connect() as conn:
        bridge_seeds.seed_all(conn)
    print("\nSummary:")
    for ds_id, info in results.items():
        print(f"  {ds_id}: {info}")


def cmd_seed(args):
    """Load/refresh manual bridge crosswalks (HCPCS ↔ product_code, etc.)."""
    db.ensure_database()
    db.apply_schema()
    with db.connect() as conn:
        n = bridge_seeds.seed_all(conn)
    print(f"OK: seeded {n} crosswalk rows")


def cmd_auto_bridge(args):
    """Auto-populate bridge_hcpcs_to_product_code from GUDID description matches.

    Requires fda_gudid_devices to be populated first:
        python run.py ingest --dataset fda_gudid
    """
    from src import gudid_matcher
    with db.connect() as conn:
        n_gudid = conn.query("SELECT count() FROM fda_gudid_devices FINAL").result_rows[0][0]
        if n_gudid == 0:
            print("SKIP: fda_gudid_devices is empty. Run `python run.py ingest --dataset fda_gudid` first.")
            return
        print(f"Running GUDID matcher against {n_gudid:,} devices (min_support={args.min_support}) …")
        proposed, inserted = gudid_matcher.auto_populate(
            conn, min_support=args.min_support,
        )
    print(f"OK: proposed={proposed}  inserted={inserted}  skipped={proposed - inserted}")


def cmd_refresh(args):
    """No-op on ClickHouse.

    The Postgres build had two materialized views (hcpcs_device_profile,
    hospital_hcpcs_enriched) that needed periodic refresh. On ClickHouse the
    UI computes those joins at query time, so there's nothing to refresh.
    """
    print("OK: no materialized views on ClickHouse — nothing to refresh")


def cmd_probe(args):
    """Check CMS data-api/v1 availability for each registered cms_data_api dataset.

    Hits /data-viewer/stats (cheap) and reports row count + CSV bulk-download URL.
    Use this to verify CMS is back up before kicking off a full ingest.
    """
    from src.cms_data_api_client import CMSDataApiClient
    client = CMSDataApiClient(timeout=45, max_retries=1)
    api_datasets = [d for d in DATASETS if d.get("source") == "cms_data_api"]
    print(f"Probing {len(api_datasets)} CMS data-api datasets (45s timeout each)…\n")
    print(f"{'DATASET':<42} {'STATUS':<6} {'ROWS':>12}  BULK CSV")
    print("-" * 110)
    up = down = 0
    for d in api_datasets:
        st = client.stats(d["uuid"])
        if st.get("total_rows") is not None:
            up += 1
            rows = f"{st['total_rows']:,}"
            csv = (st.get("data_file_url") or "")[:50]
            print(f"{d['id']:<42} {'UP':<6} {rows:>12}  {csv}")
        else:
            down += 1
            print(f"{d['id']:<42} {'DOWN':<6} {'—':>12}")
    print(f"\n{up} UP, {down} DOWN")


def cmd_status(args):
    sql = """
        SELECT dataset_id, dataset_name, started_at, finished_at,
               rows_fetched, rows_upserted, status, error
          FROM cms_ingestion_log
         ORDER BY started_at DESC
         LIMIT 50
    """
    with db.connect() as conn:
        rows = conn.query(sql).result_rows
    if not rows:
        print("No ingestion runs yet.")
        return
    for r in rows:
        ds_id, name, started, finished, fetched, upserted, status, error = r
        print(
            f"{started:%Y-%m-%d %H:%M}  {ds_id:<12}  {status:<8}  "
            f"fetched={fetched}  upserted={upserted}  {name}"
        )
        if error:
            print(f"    ERROR: {error}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create DB + apply schema")
    sub.add_parser("list", help="List datasets")
    sub.add_parser("status", help="Show ingestion history")
    sub.add_parser("seed", help="Load/refresh manual crosswalks (HCPCS↔product_code, etc.)")
    sub.add_parser("probe", help="Check if CMS data-api/v1 is responding (fast, read-only)")
    sub.add_parser("refresh", help="Refresh materialized views (hcpcs_device_profile, hospital_hcpcs_enriched)")
    ab = sub.add_parser("auto-bridge", help="Auto-populate HCPCS↔product_code bridge from GUDID text match")
    ab.add_argument("--min-support", type=int, default=2, help="Minimum GUDID records per (HCPCS, PC) pair (default: 2)")

    ing = sub.add_parser("ingest", help="Fetch CMS data into Postgres")
    ing.add_argument("--dataset", help="Ingest a single dataset id (default: all)")
    ing.add_argument("--limit", type=int, help="Hard cap rows per dataset (for testing)")

    args = parser.parse_args()
    _setup_logging(args.verbose)

    dispatch = {
        "init": cmd_init,
        "list": cmd_list,
        "ingest": cmd_ingest,
        "status": cmd_status,
        "seed": cmd_seed,
        "probe": cmd_probe,
        "refresh": cmd_refresh,
        "auto-bridge": cmd_auto_bridge,
    }
    try:
        dispatch[args.cmd](args)
    except Exception as e:
        logging.exception("Command failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
