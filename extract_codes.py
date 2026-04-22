#!/usr/bin/env python3
"""
extract_codes.py — Pull HCPCS codes and CCN (hospital) codes from the CMS warehouse.

Usage:
    python extract_codes.py                   # summary: counts + samples
    python extract_codes.py --hcpcs           # all HCPCS codes to stdout (CSV)
    python extract_codes.py --ccn             # all CCN hospital codes to stdout (CSV)
    python extract_codes.py --hcpcs --out hcpcs_codes.csv
    python extract_codes.py --ccn   --out ccn_codes.csv
    python extract_codes.py --devices-only    # device-family codes only (C/E/K/L)
"""

import sys
import csv
import argparse
import warnings

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from src import db   # uses .env automatically


# ── helpers ────────────────────────────────────────────────────────────────────

def q(sql, params=None):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params) if params else cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return cols, rows


def write_csv(cols, rows, out_path, label):
    if out_path:
        fh = open(out_path, "w", newline="", encoding="utf-8")
    else:
        fh = sys.stdout

    writer = csv.writer(fh)
    writer.writerow(cols)
    writer.writerows(rows)

    if out_path:
        fh.close()
        print(f"✅  {len(rows):,} {label} written → {out_path}")
    else:
        sys.stderr.write(f"# {len(rows):,} {label}\n")


# ── commands ───────────────────────────────────────────────────────────────────

def cmd_summary():
    print("=" * 64)
    print("  CMS WAREHOUSE — HCPCS + CCN CODE SUMMARY")
    print("=" * 64)

    # HCPCS
    _, r = q("SELECT count(*) FROM hcpcs_master")
    total = r[0][0]
    _, r = q("SELECT count(*) FROM hcpcs_master WHERE is_device")
    devices = r[0][0]
    print(f"\n📋  HCPCS Level II codes (hcpcs_master)")
    print(f"    Total codes   : {total:,}")
    print(f"    Device codes  : {devices:,}  (families C / E / K / L)")

    _, fam = q(
        "SELECT code_family, count(*) AS n "
        "FROM hcpcs_master WHERE code_family IS NOT NULL "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 12"
    )
    print("    By family prefix:")
    for row in fam:
        marker = " ← device" if row[0] in ("C", "E", "K", "L") else ""
        print(f"      {row[0]}  {row[1]:>6,}{marker}")

    # Sample HCPCS
    print("\n    Sample rows (first 5 device codes):")
    _, sample = q(
        "SELECT hcpcs_code, short_desc, betos_code, is_device "
        "FROM hcpcs_master WHERE is_device ORDER BY hcpcs_code LIMIT 5"
    )
    for r in sample:
        print(f"      {r[0]}  {str(r[1] or '')[:45]:<45}  BETOS={r[2] or '—'}")

    # CCN
    _, r = q("SELECT count(*) FROM cms_hospitals")
    total_h = r[0][0]
    _, r = q("SELECT count(DISTINCT state) FROM cms_hospitals WHERE state IS NOT NULL")
    states = r[0][0]
    print(f"\n🏥  Hospital CCN codes (cms_hospitals)")
    print(f"    Total hospitals  : {total_h:,}")
    print(f"    States covered   : {states}")

    _, top = q(
        "SELECT state, count(*) AS n FROM cms_hospitals "
        "WHERE state IS NOT NULL GROUP BY state ORDER BY n DESC LIMIT 5"
    )
    print("    Top 5 states by hospital count:")
    for r in top:
        print(f"      {r[0]}  {r[1]:,}")

    # Sample CCN
    print("\n    Sample CCN rows (first 5):")
    _, sample = q(
        "SELECT facility_id, facility_name, city, state, hospital_type "
        "FROM cms_hospitals ORDER BY state, facility_id LIMIT 5"
    )
    for r in sample:
        print(f"      CCN={r[0]}  {str(r[1] or '')[:35]:<35}  {r[2]}, {r[3]}  [{r[4]}]")

    print()


def cmd_hcpcs(out_path=None, devices_only=False):
    where = "WHERE is_device = TRUE" if devices_only else ""
    label = "device HCPCS codes" if devices_only else "HCPCS codes"
    cols, rows = q(
        f"""
        SELECT
            hcpcs_code,
            short_desc,
            long_desc,
            betos_code,
            code_family,
            is_device,
            pricing_indicator,
            coverage_code,
            asc_payment_grp,
            type_of_service,
            action_code,
            effective_qtr
        FROM hcpcs_master
        {where}
        ORDER BY hcpcs_code
        """
    )
    write_csv(cols, rows, out_path, label)


def cmd_ccn(out_path=None):
    cols, rows = q(
        """
        SELECT
            facility_id          AS ccn,
            facility_name,
            address,
            city,
            state,
            zip_code,
            county_name,
            hospital_type,
            hospital_ownership,
            overall_rating,
            emergency_services
        FROM cms_hospitals
        ORDER BY state, facility_id
        """
    )
    write_csv(cols, rows, out_path, "CCN hospital codes")


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--hcpcs",        action="store_true", help="Dump all HCPCS codes as CSV")
    parser.add_argument("--ccn",          action="store_true", help="Dump all CCN codes as CSV")
    parser.add_argument("--devices-only", action="store_true", help="With --hcpcs: device family codes only")
    parser.add_argument("--out",          metavar="FILE",       help="Output CSV file (default: stdout)")
    args = parser.parse_args()

    if args.hcpcs:
        cmd_hcpcs(out_path=args.out, devices_only=args.devices_only)
    elif args.ccn:
        cmd_ccn(out_path=args.out)
    else:
        cmd_summary()


if __name__ == "__main__":
    main()
