#!/usr/bin/env python3
"""Pull AXIOS / Boston Scientific clinical trials directly into ClickHouse.

Bypasses the risk_db Postgres detour — writes straight to clinical_trials and
clinical_trial_interventions in ClickHouse, matching the existing schema.

Usage:
    python scripts/fetch_bsc_trials.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, risk_sources  # noqa: E402

# Buckets covering BSC's full device portfolio (the ones missing from the
# existing CTG_DEVICE_BUCKETS in src/risk_sources.py).
NEW_BUCKETS = [
    # Endoscopy / GI
    ("AXIOS",                           "ercp_lams"),
    ("lumen apposing metal stent",      "ercp_lams"),
    ("EUS guided drainage",             "ercp_lams"),
    ("ERCP biliary stent",              "ercp_stent"),
    ("self expanding metal stent",      "biliary_stent"),
    ("EXALT bronchoscope",              "single_use_bronchoscope"),
    ("single use bronchoscope",         "single_use_bronchoscope"),
    ("OverStitch endoscopic suturing",  "endoscopic_suturing"),
    # Pulmonary
    ("EXPECT pulmonary needle",         "transbronchial_needle"),
    # Pain / interventional
    ("Intracept basivertebral",         "basivertebral_ablation"),
    ("Vertiflex spacer",                "interspinous_spacer"),
    # Structural heart
    ("Watchman left atrial appendage",  "laa_closure"),
    ("Farapulse pulse field ablation",  "pulse_field_ablation"),
    # Peripheral vascular
    ("Eluvia drug eluting stent",       "peripheral_des"),
    ("Ranger drug coated balloon",      "peripheral_dcb"),
    # Urology
    ("Rezum water vapor",               "rezum_bph"),
    ("GreenLight laser",                "greenlight_bph"),
]


def _client():
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=config.CLICKHOUSE_HOST, port=config.CLICKHOUSE_PORT,
        username=config.CLICKHOUSE_USER, password=config.CLICKHOUSE_PASSWORD,
        database=config.CLICKHOUSE_DATABASE, secure=config.CLICKHOUSE_SECURE,
        connect_timeout=15, send_receive_timeout=120,
    )


def _to_date(v):
    """ClinicalTrials.gov dates can be 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD'.
    ClickHouse Date column needs a datetime.date object."""
    if not v:
        return None
    s = str(v).strip()
    try:
        if len(s) == 7 and s[4] == "-":              # 'YYYY-MM'
            return datetime.strptime(s, "%Y-%m").date().replace(day=1)
        if len(s) == 4 and s.isdigit():               # 'YYYY'
            return date(int(s), 1, 1)
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _trial_row(r: dict) -> tuple:
    """Map a risk_sources.iter_trials row to the ClickHouse column tuple."""
    return (
        r.get("nct_id") or "",
        r.get("brief_title"),
        r.get("official_title"),
        r.get("overall_status"),
        r.get("phase"),
        r.get("study_type"),
        r.get("condition"),
        int(r["enrollment_count"]) if r.get("enrollment_count") not in (None, "") else None,
        1 if r.get("has_results") else 0,
        r.get("why_stopped"),
        _to_date(r.get("start_date")),
        _to_date(r.get("completion_date")),
        _to_date(r.get("last_update_date")),
        r.get("lead_sponsor"),
        r.get("device_category"),
        json.dumps(r.get("raw") or {}),
    )


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")
    log = logging.getLogger("bsc_trials")
    cli = _client()

    trial_cols = ["nct_id", "brief_title", "official_title", "overall_status",
                  "phase", "study_type", "condition", "enrollment_count",
                  "has_results", "why_stopped", "start_date", "completion_date",
                  "last_update_date", "lead_sponsor", "device_category", "raw"]
    iv_cols = ["nct_id", "intervention_name", "intervention_type", "description"]

    total_trials, total_ivs = 0, 0
    for bucket_term, device_category in NEW_BUCKETS:
        log.info("Bucket %r → device_category=%s", bucket_term, device_category)
        trial_rows, iv_rows = [], []
        n_in_bucket = 0
        try:
            for row in risk_sources.iter_trials(bucket_term, device_category):
                trial_rows.append(_trial_row(row))
                for iv in row.get("interventions") or []:
                    iv_rows.append((
                        iv.get("nct_id") or "",
                        iv.get("intervention_name") or "",
                        iv.get("intervention_type"),
                        iv.get("description"),
                    ))
                n_in_bucket += 1
        except Exception as e:
            log.warning("  bucket failed: %s", e)
            continue

        if trial_rows:
            cli.insert("clinical_trials", trial_rows, column_names=trial_cols)
            total_trials += len(trial_rows)
        if iv_rows:
            # Drop blanks; the schema requires non-empty intervention_name
            iv_rows = [r for r in iv_rows if r[1]]
            if iv_rows:
                cli.insert("clinical_trial_interventions", iv_rows, column_names=iv_cols)
                total_ivs += len(iv_rows)
        log.info("  → %d trials, %d interventions", n_in_bucket, len(iv_rows))

    log.info("DONE — wrote %d trial rows, %d intervention rows across %d buckets",
             total_trials, total_ivs, len(NEW_BUCKETS))


if __name__ == "__main__":
    main()
