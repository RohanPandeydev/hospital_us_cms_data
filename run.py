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
    csv_path = getattr(args, "csv_path", None)
    if csv_path and not args.dataset:
        print("ERROR: --csv-path requires --dataset <id>")
        sys.exit(2)
    results = ingest_all(dataset_ids=ids, limit=args.limit, csv_path=csv_path)
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


def cmd_risk_init(args):
    """Create Postgres DB `cms_hospitals` if missing and apply risk_schema.sql."""
    from src import risk_db
    risk_db.ensure_database()
    risk_db.apply_risk_schema()
    print("OK: risk schema applied to Postgres cms_hospitals")


def _log_failure(handle, total, err):
    """Write the failure log on a fresh connection — the data-path transaction
    is aborted by the time we get here."""
    from src import risk_db
    try:
        with risk_db.connect() as log_conn:
            risk_db.finish_log(log_conn, handle, 0, total, "error", str(err))
    except Exception:
        logging.exception("could not persist failure log")


def cmd_risk_ingest(args):
    """Pull one of the new risk-intelligence sources into Postgres.

    Sources:
      recalls          — FDA openFDA /device/enforcement.json
      clinical_trials  — clinicaltrials.gov v2 (loops over device-family buckets)
      open_payments    — requires --dataset <dkan-uuid> for the target program year
    """
    from src import risk_db, risk_sources

    # start_log runs on its own short transaction so the handle survives even if
    # the data-path tx aborts later.
    with risk_db.connect() as log_conn:
        handle = risk_db.start_log(log_conn, args.source, params={
            "limit": args.limit, "dataset": getattr(args, "dataset", None),
        })

    total = 0
    try:
        with risk_db.connect() as conn:
            if args.source == "510k":
                batch = []
                for row in risk_sources.iter_510k(limit=args.limit):
                    batch.append(row)
                    if len(batch) >= 500:
                        total += risk_db.upsert_510k(conn, batch)
                        batch = []
                if batch:
                    total += risk_db.upsert_510k(conn, batch)
                print(f"OK: upserted {total} 510(k) clearances")

            elif args.source == "maude":
                # Year-sliced so we can go past the openFDA 25k skip cap.
                # per_year_limit = args.limit, total cap has no practical limit.
                batch = []
                for row in risk_sources.iter_maude_year_sliced(
                    args.year_from, args.year_to, per_year_limit=args.limit,
                ):
                    batch.append(row)
                    if len(batch) >= 500:
                        total += risk_db.upsert_maude(conn, batch)
                        conn.commit()
                        batch = []
                if batch:
                    total += risk_db.upsert_maude(conn, batch)
                print(f"OK: upserted {total} MAUDE adverse-event reports "
                      f"({args.year_from}-{args.year_to})")

            elif args.source == "recalls":
                from src import risk_recall_tagger
                batch = []
                # If the caller narrowed the year range from defaults, slice
                # by year so we can go past the openFDA 25k skip cap.
                if args.year_from != 2019 or args.year_to != 2024:
                    recall_iter = risk_sources.iter_recalls_year_sliced(
                        args.year_from, args.year_to, per_year_limit=args.limit,
                    )
                elif args.limit is None:
                    # Full pull: year-slice across the default 2019-2024 window.
                    recall_iter = risk_sources.iter_recalls_year_sliced(
                        args.year_from, args.year_to,
                    )
                else:
                    recall_iter = risk_sources.iter_recalls(limit=args.limit)
                for row in recall_iter:
                    batch.append(row)
                    if len(batch) >= 500:
                        total += risk_db.upsert_recalls(conn, batch)
                        conn.commit()
                        batch = []
                if batch:
                    total += risk_db.upsert_recalls(conn, batch)
                tag = risk_recall_tagger.tag_all(
                    conn, use_llm=not args.no_llm, llm_max_rows=args.llm_max_rows,
                )
                _by_method = tag.get("by_method", {})
                print(f"OK: upserted {total} recalls; tagged "
                      f"{tag['tagged']}/{tag['total']} with device_category")
                if _by_method:
                    parts = [f"{m}={c}" for m, c in sorted(_by_method.items(),
                                                             key=lambda kv: -kv[1])]
                    print("  match methods this run: " + ", ".join(parts))

            elif args.source == "clinical_trials":
                for bucket_term, device_category in risk_sources.CTG_DEVICE_BUCKETS:
                    print(f"  bucket: {bucket_term!r} -> {device_category}")
                    trial_batch, iv_batch = [], []
                    for row in risk_sources.iter_trials(bucket_term, device_category,
                                                          limit=args.limit):
                        trial_batch.append(row)
                        iv_batch.extend(row.get("interventions") or [])
                        if len(trial_batch) >= 200:
                            total += risk_db.upsert_trials(conn, trial_batch)
                            risk_db.upsert_trial_interventions(conn, iv_batch)
                            trial_batch, iv_batch = [], []
                    if trial_batch:
                        total += risk_db.upsert_trials(conn, trial_batch)
                        risk_db.upsert_trial_interventions(conn, iv_batch)
                print(f"OK: upserted {total} clinical trials")

            elif args.source == "open_payments":
                if not args.dataset:
                    print("ERROR: --dataset <dkan-uuid> required for open_payments")
                    sys.exit(2)
                batch = []
                for row in risk_sources.iter_open_payments(args.dataset, limit=args.limit):
                    batch.append(row)
                    if len(batch) >= 500:
                        total += risk_db.upsert_open_payments(conn, batch)
                        # Commit per batch so a mid-run crash (network drop,
                        # SIGKILL, DKAN 5xx) doesn't roll back hours of work.
                        conn.commit()
                        batch = []
                        if total % 5000 == 0:
                            logging.info("open_payments: committed %d rows", total)
                if batch:
                    total += risk_db.upsert_open_payments(conn, batch)
                    conn.commit()
                print(f"OK: upserted {total} open-payments rows")

            else:
                print(f"ERROR: unknown source {args.source!r}")
                sys.exit(2)
    except Exception as e:
        _log_failure(handle, total, e)
        raise

    # Success: finish the log on a fresh short transaction.
    with risk_db.connect() as log_conn:
        risk_db.finish_log(log_conn, handle, total, total, "ok")


def cmd_risk_build(args):
    """Assemble the unified risk-intelligence JSON rows."""
    from src import risk_db
    from src import risk_assembler
    with risk_db.connect() as conn:
        result = risk_assembler.assemble(
            conn,
            limit_hospitals=args.limit_hospitals,
            state=args.state,
            ccn=args.ccn,
        )
    print(f"OK: wrote {result['rows']} risk rows "
          f"({result['hospitals']} hospitals × {result['categories']} categories)")


def cmd_state_events_ingest(args):
    """Pull state-mandated adverse-event registries (currently MA SREs)
    into `state_adverse_events`. This is the only public US source we've
    verified that links a hospital identity to a device-related event —
    public MAUDE has no hospital identifier.
    """
    from src import state_event_ingest as sei
    summary = sei.ingest_ma(years=args.year, variants=args.variant)
    for v, info in summary.items():
        print(f"  {v}: {info['rows']} rows · {info['ccn_matched']} CCN-matched")


def cmd_risk_load_medicare(args):
    """Ingest CMS Medicare Inpatient PUF + Physician datasets + DRG map +
    FDA Classification + build the unified code crosswalk.

    These are the *direct hospital ↔ device linkage* layers:
      * cms_hospital_drg_volume  — CCN × DRG × discharges (claims evidence)
      * fda_device_classification — authoritative product-code metadata
      * device_code_crosswalk     — every code type → device_category
    """
    from src import (risk_db, risk_drg_map, risk_ingest_medicare as ing,
                     risk_fda_classification as fc, risk_crosswalk)
    summary: dict = {}
    with risk_db.connect() as conn:
        if not args.skip_drg_map:
            n = risk_drg_map.seed_drg_device_map(conn)
            summary["drg_map_rows"] = n

        if args.inpatient:
            path = args.inpatient
            if path.startswith("http"):
                path = ing.download(path, "/tmp/cms_inpatient_drg.csv")
            n = ing.load_inpatient(conn, path)
            summary["inpatient_rows"] = n

        if args.physician:
            path = args.physician
            if path.startswith("http"):
                path = ing.download(path, "/tmp/cms_physician.csv")
            n = ing.load_physician(conn, path)
            summary["physician_rows"] = n

        if not args.skip_classification:
            n = fc.run(conn, limit=args.classification_limit)
            summary["classification_rows"] = n

        if not args.skip_crosswalk:
            summary["crosswalk"] = risk_crosswalk.build(conn)

    print("OK: Medicare/crosswalk load complete")
    for k, v in summary.items():
        print(f"  {k}: {v}")


def cmd_risk_tag(args):
    """Re-run the layered matcher (K# → PC → MFG → regex → LLM) over
    fda_recalls rows with NULL device_category."""
    from src import risk_db, risk_recall_tagger
    with risk_db.connect() as conn:
        res = risk_recall_tagger.tag_all(
            conn, use_llm=not args.no_llm, llm_max_rows=args.llm_max_rows,
        )
    print(f"OK: {res['tagged']}/{res['total']} total tagged")
    by_method = res.get("by_method", {})
    if by_method:
        print("  match methods this run: " + ", ".join(
            f"{m}={c}" for m, c in sorted(by_method.items(), key=lambda kv: -kv[1])
        ))
    by_conf = res.get("by_confidence", {})
    if by_conf:
        print("  confidence mix:       " + ", ".join(
            f"{c}={n}" for c, n in sorted(by_conf.items(), key=lambda kv: -kv[1])
        ))


def cmd_risk_show(args):
    """Print one unified risk JSON row (pretty-printed)."""
    import json
    from src import risk_db
    with risk_db.connect() as conn, conn.cursor() as cur:
        if args.ccn and args.device_category:
            cur.execute(
                "SELECT payload FROM hospital_device_risk_intelligence "
                "WHERE ccn = %s AND device_category = %s",
                (args.ccn, args.device_category),
            )
        elif args.ccn:
            cur.execute(
                "SELECT payload FROM hospital_device_risk_intelligence "
                "WHERE ccn = %s ORDER BY final_score DESC LIMIT 1",
                (args.ccn,),
            )
        else:
            cur.execute(
                "SELECT payload FROM hospital_device_risk_intelligence "
                "ORDER BY final_score DESC NULLS LAST LIMIT 1"
            )
        row = cur.fetchone()
    if not row:
        print("No matching row.")
        return
    print(json.dumps(row[0], indent=2, default=str))


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
    ing.add_argument("--csv-path",
                     help="Ingest from a locally-downloaded CSV instead of the "
                          "CMS data-api (useful when the API is down). "
                          "Requires --dataset and only works with cms_data_api datasets.")

    # ---- Risk Intelligence pipeline (Postgres cms_hospitals) ----
    sub.add_parser("risk-init",
                    help="Create Postgres cms_hospitals DB + apply risk_schema.sql")

    ri = sub.add_parser("risk-ingest",
                         help="Pull a new risk source (maude/recalls/clinical_trials/open_payments) into Postgres")
    ri.add_argument("--source", required=True,
                     choices=["maude", "recalls", "clinical_trials", "open_payments", "510k"])
    ri.add_argument("--limit", type=int,
                     help="Cap rows (per bucket for clinical_trials, per year for maude/recalls)")
    ri.add_argument("--dataset",
                     help="DKAN UUID for Open Payments year (required for open_payments)")
    ri.add_argument("--year-from", type=int, default=2019,
                     help="Start year for year-sliced sources (maude, recalls)")
    ri.add_argument("--year-to", type=int, default=2024,
                     help="End year for year-sliced sources (maude, recalls)")
    ri.add_argument("--no-llm", action="store_true",
                     help="Skip the Groq LLM fallback for recall device_category tagging")
    ri.add_argument("--llm-max-rows", type=int, default=None,
                     help="Cap how many regex-miss rows we send to Groq (default: all)")

    rb = sub.add_parser("risk-build",
                         help="Assemble unified risk JSON per (hospital × device_category)")
    rb.add_argument("--limit-hospitals", type=int,
                     help="Cap the number of hospitals processed (for testing)")
    rb.add_argument("--state", help="Filter hospitals to one state, e.g. CA")
    rb.add_argument("--ccn", help="Build for a single CCN")

    rt = sub.add_parser("risk-tag",
                         help="Re-run the regex+Groq tagger over fda_recalls with NULL device_category")
    rt.add_argument("--no-llm", action="store_true")
    rt.add_argument("--llm-max-rows", type=int, default=None)

    rs = sub.add_parser("risk-show",
                         help="Pretty-print one unified risk JSON row")
    rs.add_argument("--ccn")
    rs.add_argument("--device-category", dest="device_category")

    sae = sub.add_parser("state-events-ingest",
                          help="Ingest state-mandated adverse-event registries "
                               "(currently MA SREs — the only public source "
                               "with hospital-named device events).")
    sae.add_argument("--state", default="MA", choices=["MA"],
                     help="Which state's registry to pull (only MA today).")
    sae.add_argument("--year", type=int, action="append",
                     help="Restrict to specific year(s); repeat for multiple. "
                          "Default: all published years.")
    sae.add_argument("--variant", action="append",
                     choices=["acute", "non_acute", "asc"],
                     help="Restrict to facility variant(s). Default: all.")

    rlm = sub.add_parser("risk-load-medicare",
                         help="Ingest CMS Medicare claims + FDA classification + build crosswalk")
    rlm.add_argument("--inpatient",
                      help="Path or URL of CMS Inpatient by Provider & Service CSV")
    rlm.add_argument("--physician",
                      help="Path or URL of CMS Physician by Provider & Service CSV")
    rlm.add_argument("--skip-drg-map", action="store_true",
                      help="Skip re-seeding drg_to_device_category")
    rlm.add_argument("--skip-classification", action="store_true",
                      help="Skip FDA classification ingest")
    rlm.add_argument("--skip-crosswalk", action="store_true",
                      help="Skip device_code_crosswalk rebuild")
    rlm.add_argument("--classification-limit", type=int, default=None,
                      help="Cap FDA classification rows (testing)")

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
        "risk-init":   cmd_risk_init,
        "risk-ingest": cmd_risk_ingest,
        "risk-tag":    cmd_risk_tag,
        "risk-build":  cmd_risk_build,
        "risk-show":   cmd_risk_show,
        "risk-load-medicare": cmd_risk_load_medicare,
        "state-events-ingest": cmd_state_events_ingest,
    }
    try:
        dispatch[args.cmd](args)
    except Exception as e:
        logging.exception("Command failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
