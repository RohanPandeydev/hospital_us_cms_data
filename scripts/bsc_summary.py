#!/usr/bin/env python3
"""Boston Scientific footprint across the ClickHouse warehouse.

A one-shot ad-hoc report. Pulls together every Boston-Scientific-related
slice from the data:
  * Open Payments  — payments to physicians/hospitals
  * Clinical Trials — BSc-sponsored studies + interventions (HCPCS-mapped)
  * MA SREs        — Massachusetts hospital adverse events (BSc devices)

Usage:
    python scripts/bsc_summary.py                # full footprint
    python scripts/bsc_summary.py --product WATCHMAN
    python scripts/bsc_summary.py --year 2024
    python scripts/bsc_summary.py --top 20       # show top-N rows
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db  # noqa: E402

BSC_PATTERN = "%boston scientific%"


def section(title: str):
    print(f"\n{'='*68}\n  {title}\n{'='*68}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--product", help="Filter to one product name (substring match)")
    p.add_argument("--year",    type=int, help="Restrict to one program year")
    p.add_argument("--top",     type=int, default=15, help="Top-N rows per section (default 15)")
    args = p.parse_args()

    product_filter = ""
    if args.product:
        product_filter = f" AND lowerUTF8(product_name) LIKE '%{args.product.lower()}%'"
    year_filter = f" AND year = {args.year}" if args.year else ""

    with db.connect() as c:
        # ---- Open Payments ----
        section("BSc Open Payments — headline numbers")
        h = c.query(f"""
            SELECT
                count() AS payments,
                round(sum(payment_total)/1e6, 2) AS m_usd,
                countDistinct(physician_npi) AS physicians,
                countDistinct(teaching_hospital_ccn) AS hospitals,
                countDistinct(product_name) AS products,
                min(payment_date) AS first_pay,
                max(payment_date) AS last_pay
            FROM cms_open_payments
            WHERE lowerUTF8(manufacturer_name) LIKE '{BSC_PATTERN}'{product_filter}{year_filter}
        """).result_rows[0]
        print(f"  payments:   {h[0]:>10,}")
        print(f"  total $:    ${h[1]:>10,.2f}M")
        print(f"  physicians: {h[2]:>10,}")
        print(f"  hospitals:  {h[3]:>10,}")
        print(f"  products:   {h[4]:>10,}")
        print(f"  date range: {h[5]} → {h[6]}")

        section(f"BSc — top {args.top} products by $")
        rows = c.query(f"""
            SELECT product_name, count() AS payments,
                   round(sum(payment_total)/1e3, 1) AS k_usd,
                   countDistinct(physician_npi) AS physicians
            FROM cms_open_payments
            WHERE lowerUTF8(manufacturer_name) LIKE '{BSC_PATTERN}'{product_filter}{year_filter}
              AND product_name != ''
            GROUP BY product_name
            ORDER BY k_usd DESC LIMIT {args.top}
        """).result_rows
        print(f"  {'PRODUCT':<46} {'PAYMENTS':>10} {'TOTAL $K':>12} {'MDs':>6}")
        for r in rows:
            print(f"  {(r[0] or '')[:44]:<46} {r[1]:>10,} {r[2]:>12,.1f} {r[3]:>6,}")

        section(f"BSc — top {args.top} physicians by $")
        rows = c.query(f"""
            SELECT physician_name, physician_npi, physician_specialty,
                   count() AS pays,
                   round(sum(payment_total)/1e3, 1) AS k_usd
            FROM cms_open_payments
            WHERE lowerUTF8(manufacturer_name) LIKE '{BSC_PATTERN}'{product_filter}{year_filter}
              AND physician_name != ''
            GROUP BY physician_name, physician_npi, physician_specialty
            ORDER BY k_usd DESC LIMIT {args.top}
        """).result_rows
        print(f"  {'PHYSICIAN':<32} {'NPI':<12} {'SPECIALTY':<24} {'PAYS':>6} {'$K':>8}")
        for r in rows:
            print(f"  {(r[0] or '')[:30]:<32} {(r[1] or ''):<12} "
                   f"{(r[2] or '')[:22]:<24} {r[3]:>6,} {r[4]:>8,.1f}")

        section(f"BSc — payments by year")
        rows = c.query(f"""
            SELECT year, count(), round(sum(payment_total)/1e6, 2) AS m_usd
            FROM cms_open_payments
            WHERE lowerUTF8(manufacturer_name) LIKE '{BSC_PATTERN}'{product_filter}
            GROUP BY year ORDER BY year
        """).result_rows
        for r in rows:
            print(f"  {r[0]}: {r[1]:>10,} pays   ${r[2]:>8.2f}M")

        # ---- Clinical Trials ----
        section("BSc Clinical Trials — overview")
        rows = c.query("""
            SELECT count() AS trials,
                   countDistinct(device_category) AS device_buckets,
                   sum(if(has_results,1,0)) AS with_results,
                   countDistinct(overall_status) AS statuses
            FROM clinical_trials
            WHERE lowerUTF8(lead_sponsor) LIKE %(p)s
        """, parameters={"p": BSC_PATTERN}).result_rows[0]
        print(f"  trials: {rows[0]:,}  buckets: {rows[1]}  with-results: {rows[2]}  statuses: {rows[3]}")

        section("BSc Clinical Trials — by device bucket")
        rows = c.query("""
            SELECT device_category, count() AS trials,
                   sum(if(overall_status='COMPLETED',1,0)) AS completed,
                   sum(if(overall_status IN ('RECRUITING','ENROLLING_BY_INVITATION','ACTIVE_NOT_RECRUITING','NOT_YET_RECRUITING'),1,0)) AS active
            FROM clinical_trials
            WHERE lowerUTF8(lead_sponsor) LIKE %(p)s
            GROUP BY device_category ORDER BY trials DESC
        """, parameters={"p": BSC_PATTERN}).result_rows
        print(f"  {'DEVICE BUCKET':<28} {'TRIALS':>8} {'COMPLETED':>10} {'ACTIVE':>8}")
        for r in rows:
            print(f"  {r[0]:<28} {r[1]:>8,} {r[2]:>10,} {r[3]:>8,}")

        # ---- HCPCS linkage (via Groq-mapped trial interventions) ----
        section("BSc Clinical Trials — HCPCS codes linked")
        rows = c.query("""
            SELECT i.hcpcs_code,
                   any(m.short_desc) AS desc,
                   countDistinct(i.nct_id) AS n_trials
            FROM clinical_trial_interventions FINAL AS i
            INNER JOIN (
                SELECT nct_id FROM clinical_trials
                WHERE lowerUTF8(lead_sponsor) LIKE %(p)s
            ) AS t USING nct_id
            LEFT JOIN hcpcs_master FINAL AS m USING hcpcs_code
            WHERE i.hcpcs_code IS NOT NULL
            GROUP BY i.hcpcs_code
            ORDER BY n_trials DESC LIMIT 20
        """, parameters={"p": BSC_PATTERN}).result_rows
        if rows:
            print(f"  {'HCPCS':<8} {'DESCRIPTION':<46} {'TRIALS':>8}")
            for r in rows:
                print(f"  {r[0]:<8} {(r[1] or '')[:44]:<46} {r[2]:>8,}")
        else:
            print("  (no HCPCS mappings yet — run `python run.py map-trials-to-hcpcs`)")

        # ---- MA SREs ----
        section("MA SREs — Boston-area hospitals (context for BSc)")
        rows = c.query("""
            SELECT hospital_name, sum(event_count) AS events
            FROM state_adverse_events
            WHERE state='MA' AND lowerUTF8(hospital_name) IN (
                SELECT lowerUTF8(hospital_name) FROM state_adverse_events
                WHERE state='MA'
            )
            AND lowerUTF8(hospital_name) LIKE '%boston%'
            GROUP BY hospital_name
            ORDER BY events DESC LIMIT 10
        """).result_rows
        for r in rows:
            print(f"  {r[0]:<50} {r[1]:>6,} events")


if __name__ == "__main__":
    main()
