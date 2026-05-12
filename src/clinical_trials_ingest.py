"""ClinicalTrials.gov v2 ingester for ClickHouse.

Writes into the existing `clinical_trials` and `clinical_trial_interventions`
tables (originally seeded by the older risk_db pipeline — same target,
different name). One row per study × one row per intervention.

The intervention rows leave `hcpcs_code` NULL — a separate Groq mapper step
can backfill them later. The schema columns are added via ALTER in
schema_clickhouse.sql, so this ingester is safe to run before/after the
schema has been bumped.

Why bucket-scoped, not "everything": pulling every CTG study would be
~500k+ rows, most irrelevant to CMS-billed device families. The
CTG_DEVICE_BUCKETS list mirrors the device categories used in the
HCPCS crosswalk so trials join cleanly downstream.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging

from . import db
from . import risk_sources

log = logging.getLogger(__name__)


# Mirrors the existing `clinical_trials` schema in ClickHouse (created by
# risk_db.apply_risk_schema). Field order is the column order at insert.
TRIAL_COLUMNS = [
    "nct_id", "brief_title", "official_title", "overall_status",
    "phase", "study_type", "condition",
    "enrollment_count", "has_results", "why_stopped",
    "start_date", "completion_date", "last_update_date",
    "lead_sponsor", "device_category", "raw",
]

# Existing schema is keyed by (nct_id) only — meaning ReplacingMergeTree
# collapses to one intervention per trial. That's a known limitation of the
# upstream schema; we keep parity rather than fork.
INTERVENTION_COLUMNS = [
    "nct_id", "intervention_name", "intervention_type", "description",
]


def _to_date(v):
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return _dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _trial_row(t: dict) -> tuple:
    """Map a flattened risk_sources.iter_trials row → clinical_trials tuple."""
    raw = t.get("raw") or {}
    return (
        t.get("nct_id"),
        t.get("brief_title"),
        t.get("official_title"),
        t.get("overall_status"),
        t.get("phase"),
        t.get("study_type"),
        t.get("condition"),
        t.get("enrollment_count"),
        1 if t.get("has_results") else 0,
        t.get("why_stopped"),
        _to_date(t.get("start_date")),
        _to_date(t.get("completion_date")),
        _to_date(t.get("last_update_date")),
        t.get("lead_sponsor"),
        t.get("device_category"),
        json.dumps(raw, default=str, ensure_ascii=False)[:200_000],
    )


def _intervention_rows(t: dict):
    nct = t.get("nct_id")
    if not nct:
        return
    for intv in t.get("interventions") or []:
        name = intv.get("intervention_name")
        if not name:
            continue
        yield (nct, name, intv.get("intervention_type"), intv.get("description"))


def _flush(conn, trials: list, ivs: list) -> int:
    if trials:
        seen: dict[str, tuple] = {t[0]: t for t in trials if t[0]}
        conn.insert("clinical_trials", list(seen.values()),
                    column_names=TRIAL_COLUMNS)
    if ivs:
        # Dedupe by (nct_id, intervention_name) so a re-fetch of the same
        # study doesn't duplicate rows before the merge runs.
        seen_iv: dict[tuple, tuple] = {(r[0], r[1]): r for r in ivs}
        conn.insert("clinical_trial_interventions", list(seen_iv.values()),
                    column_names=INTERVENTION_COLUMNS)
    return len(trials)


def ingest(limit: int | None = None,
           per_bucket_limit: int | None = None) -> tuple[int, int]:
    """Pull every CTG bucket, upsert into ClickHouse.

    limit            — hard cap across all buckets (smoke testing)
    per_bucket_limit — cap rows per bucket (defaults to no cap)
    """
    dataset_meta = {"id": "clinical_trials", "name": "ClinicalTrials.gov v2"}
    fetched = 0
    upserted = 0
    status = "success"
    error = None

    with db.connect() as conn:
        handle = db.start_ingest_log(conn, dataset_meta)
        try:
            trial_batch: list[tuple] = []
            iv_batch: list[tuple] = []
            for bucket_term, device_category in risk_sources.CTG_DEVICE_BUCKETS:
                log.info("CTG bucket: %r -> %s", bucket_term, device_category)
                n_bucket = 0
                for t in risk_sources.iter_trials(
                    bucket_term, device_category, limit=per_bucket_limit,
                ):
                    trial_batch.append(_trial_row(t))
                    iv_batch.extend(_intervention_rows(t))
                    fetched += 1
                    n_bucket += 1
                    if len(trial_batch) >= 500:
                        upserted += _flush(conn, trial_batch, iv_batch)
                        trial_batch, iv_batch = [], []
                    if limit is not None and fetched >= limit:
                        break
                log.info("  bucket=%r yielded %d (cum=%d)",
                         bucket_term, n_bucket, fetched)
                if limit is not None and fetched >= limit:
                    break
            if trial_batch:
                upserted += _flush(conn, trial_batch, iv_batch)
            log.info("ClinicalTrials.gov: fetched=%d upserted=%d",
                     fetched, upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            log.exception("ClinicalTrials ingest failed")
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted
