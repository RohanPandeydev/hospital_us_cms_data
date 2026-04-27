"""Postgres connector for the Unified Risk Intelligence flow.

The primary warehouse (src/db.py) has been migrated to ClickHouse, but the
Risk Intelligence output is written to the local Postgres DB `cms_hospitals`
per the user's .env config. This module is the thin psycopg2 wrapper used
by risk_sources.py and risk_assembler.py — intentionally decoupled from the
ClickHouse code path so we don't break existing ingest.
"""

import contextlib
import datetime as _dt
import json
import logging
import os
import uuid

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

RISK_SCHEMA_FILE = os.path.join(os.path.dirname(__file__), "..", "risk_schema.sql")


def _conn_kwargs():
    return {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "user": os.getenv("PGUSER", "rohankumarpandey"),
        "password": os.getenv("PGPASSWORD", "") or None,
        "dbname": os.getenv("PGDATABASE", "cms_hospitals"),
    }


@contextlib.contextmanager
def connect():
    kw = {k: v for k, v in _conn_kwargs().items() if v is not None or k == "password"}
    conn = psycopg2.connect(**{k: v for k, v in kw.items() if v is not None})
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_database():
    """Connect to the 'postgres' admin DB and CREATE DATABASE if missing."""
    target = os.getenv("PGDATABASE", "cms_hospitals")
    admin_kw = {k: v for k, v in _conn_kwargs().items() if v is not None}
    admin_kw["dbname"] = "postgres"
    conn = psycopg2.connect(**admin_kw)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{target}"')
                log.info("Created database %s", target)
            else:
                log.info("Database %s already exists", target)
    finally:
        conn.close()


def apply_risk_schema():
    with open(RISK_SCHEMA_FILE, "r") as f:
        ddl = f.read()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(ddl)
    log.info("Risk schema applied")


# ---------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------

def _dedupe_last(rows, key_fn):
    """Keep the last occurrence of each key — mirrors ClickHouse ReplacingMergeTree."""
    seen = {}
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        seen[k] = r
    return list(seen.values())


def upsert_maude(conn, rows):
    """Upsert MAUDE adverse-event rows into fda_maude_events + fda_maude_devices.

    `rows` are the raw openFDA /device/event.json dicts. Each row has a
    top-level `report_number` and a `device` list (one device per row is
    typical but we store every entry). The event-level fields mirror the
    column layout of fda_maude_events; device-level fields go into
    fda_maude_devices keyed on (report_number, seq)."""
    if not rows:
        return 0
    rows = _dedupe_last(rows, lambda r: r.get("report_number"))

    event_sql = """
        INSERT INTO fda_maude_events (
            report_number, event_type, date_received, date_of_event,
            event_location, report_source_code, mdr_report_key,
            manufacturer_name, facility_name, facility_state, facility_zip,
            patient_outcomes, device_problems, mdr_text, raw
        ) VALUES %s
        ON CONFLICT (report_number) DO UPDATE SET
            event_type = EXCLUDED.event_type,
            date_received = EXCLUDED.date_received,
            date_of_event = EXCLUDED.date_of_event,
            event_location = EXCLUDED.event_location,
            report_source_code = EXCLUDED.report_source_code,
            mdr_report_key = EXCLUDED.mdr_report_key,
            manufacturer_name = EXCLUDED.manufacturer_name,
            facility_name = EXCLUDED.facility_name,
            facility_state = EXCLUDED.facility_state,
            facility_zip = EXCLUDED.facility_zip,
            patient_outcomes = EXCLUDED.patient_outcomes,
            device_problems = EXCLUDED.device_problems,
            mdr_text = EXCLUDED.mdr_text,
            raw = EXCLUDED.raw,
            fetched_at = now()
    """
    event_values = []
    device_values = []
    for r in rows:
        rn = r.get("report_number")
        if not rn:
            continue
        # patient outcomes / device problems / mdr text are arrays in raw JSON
        outcomes = r.get("patient") or []
        outcome_str = None
        if isinstance(outcomes, list):
            flat = []
            for p in outcomes:
                if isinstance(p, dict):
                    flat.extend(p.get("sequence_number_outcome") or [])
            outcome_str = ";".join(x for x in flat if x) or None
        dev_problems = r.get("device_problems") or []
        dev_problems_str = ";".join(dev_problems) if isinstance(dev_problems, list) else str(dev_problems or "") or None
        mdr_texts = r.get("mdr_text") or []
        mdr_text_str = None
        if isinstance(mdr_texts, list):
            mdr_text_str = " | ".join(
                (m.get("text") or "")[:2000] for m in mdr_texts if isinstance(m, dict)
            )[:20000] or None
        # openFDA redacts actual facility_name; store None for that field.
        # The useful location cue is the manufacturer_g1_* fallbacks.
        event_values.append((
            rn,
            r.get("event_type"),
            _to_date(r.get("date_received")),
            _to_date(r.get("date_of_event")),
            r.get("event_location"),
            r.get("report_source_code"),
            r.get("mdr_report_key"),
            r.get("manufacturer_name"),
            None,
            r.get("manufacturer_g1_state") or r.get("reporter_state"),
            r.get("manufacturer_g1_postal_code"),
            outcome_str,
            dev_problems_str,
            mdr_text_str,
            json.dumps(r, default=str),
        ))

        # Device list — one row per device in the report
        for i, d in enumerate(r.get("device") or []):
            if not isinstance(d, dict):
                continue
            openfda = d.get("openfda") or {}
            device_values.append((
                rn, i,
                d.get("device_report_product_code") or
                    (openfda.get("product_code") if isinstance(openfda.get("product_code"), str)
                     else (openfda.get("product_code") or [None])[0]),
                d.get("brand_name"),
                d.get("generic_name"),
                d.get("manufacturer_d_name") or d.get("manufacturer_name"),
                d.get("model_number"),
                d.get("catalog_number"),
                d.get("lot_number"),
                d.get("udi_di"),
                d.get("udi_public"),
                d.get("device_age_text"),
                d.get("device_availability"),
                json.dumps(d, default=str),
            ))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, event_sql, event_values, page_size=200)

        if device_values:
            # Replace the device rows for any report_number we just upserted —
            # cleaner than trying to dedupe per (report_number, seq) across
            # a single batch with partial overlap.
            rns = list({v[0] for v in device_values})
            cur.execute("DELETE FROM fda_maude_devices WHERE report_number = ANY(%s)",
                        (rns,))
            dev_sql = """
                INSERT INTO fda_maude_devices (
                    report_number, seq, product_code, brand_name, generic_name,
                    manufacturer, model_number, catalog_number, lot_number,
                    udi_di, udi_public, device_age, device_availability, raw
                ) VALUES %s
            """
            psycopg2.extras.execute_values(cur, dev_sql, device_values, page_size=200)

    return len(event_values)


def upsert_recalls(conn, rows):
    """Upsert FDA recall enforcement rows."""
    if not rows:
        return 0
    rows = _dedupe_last(rows, lambda r: r.get("recall_number"))
    sql = """
        INSERT INTO fda_recalls (
            recall_number, event_id, product_code, firm_name, brand_name,
            product_description, reason_for_recall, recall_class, status,
            recall_initiation_date, center_classification_date, termination_date,
            country, state, city, distribution_pattern, product_quantity,
            voluntary_mandated, raw
        ) VALUES %s
        ON CONFLICT (recall_number) DO UPDATE SET
            event_id = EXCLUDED.event_id,
            product_code = EXCLUDED.product_code,
            firm_name = EXCLUDED.firm_name,
            brand_name = EXCLUDED.brand_name,
            product_description = EXCLUDED.product_description,
            reason_for_recall = EXCLUDED.reason_for_recall,
            recall_class = EXCLUDED.recall_class,
            status = EXCLUDED.status,
            recall_initiation_date = EXCLUDED.recall_initiation_date,
            center_classification_date = EXCLUDED.center_classification_date,
            termination_date = EXCLUDED.termination_date,
            raw = EXCLUDED.raw,
            fetched_at = now()
    """
    values = [
        (
            r["recall_number"], r.get("event_id"), r.get("product_code"),
            r.get("firm_name"), r.get("brand_name"),
            r.get("product_description"), r.get("reason_for_recall"),
            r.get("recall_class"), r.get("status"),
            _to_date(r.get("recall_initiation_date")),
            _to_date(r.get("center_classification_date")),
            _to_date(r.get("termination_date")),
            r.get("country"), r.get("state"), r.get("city"),
            r.get("distribution_pattern"), r.get("product_quantity"),
            r.get("voluntary_mandated"),
            json.dumps(r.get("raw") or r, default=str),
        )
        for r in rows if r.get("recall_number")
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, values, page_size=200)
    return len(values)


def upsert_510k(conn, rows):
    """Upsert fda_510k rows from risk_sources.iter_510k."""
    if not rows:
        return 0
    rows = _dedupe_last(rows, lambda r: r.get("k_number"))
    sql = """
        INSERT INTO fda_510k (
            k_number, applicant, device_name, product_code,
            decision_date, decision_description, clearance_type,
            statement_or_summary, country_code, postal_code, state, raw
        ) VALUES %s
        ON CONFLICT (k_number) DO UPDATE SET
            applicant = EXCLUDED.applicant,
            device_name = EXCLUDED.device_name,
            product_code = EXCLUDED.product_code,
            decision_date = EXCLUDED.decision_date,
            decision_description = EXCLUDED.decision_description,
            clearance_type = EXCLUDED.clearance_type,
            statement_or_summary = EXCLUDED.statement_or_summary,
            raw = EXCLUDED.raw,
            fetched_at = now()
    """
    values = [
        (
            r["k_number"], r.get("applicant"), r.get("device_name"),
            r.get("product_code"),
            _to_date(r.get("decision_date")),
            r.get("decision_description"), r.get("clearance_type"),
            r.get("statement_or_summary"),
            r.get("country_code"), r.get("postal_code"), r.get("state"),
            json.dumps(r.get("raw") or r, default=str),
        )
        for r in rows if r.get("k_number")
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, values, page_size=200)
    return len(values)


def upsert_trials(conn, rows):
    if not rows:
        return 0
    rows = _dedupe_last(rows, lambda r: r.get("nct_id"))
    sql = """
        INSERT INTO clinical_trials (
            nct_id, brief_title, official_title, overall_status, phase,
            study_type, condition, enrollment_count, has_results, why_stopped,
            start_date, completion_date, last_update_date, lead_sponsor,
            device_category, raw
        ) VALUES %s
        ON CONFLICT (nct_id) DO UPDATE SET
            brief_title = EXCLUDED.brief_title,
            overall_status = EXCLUDED.overall_status,
            phase = EXCLUDED.phase,
            enrollment_count = EXCLUDED.enrollment_count,
            has_results = EXCLUDED.has_results,
            last_update_date = EXCLUDED.last_update_date,
            device_category = EXCLUDED.device_category,
            raw = EXCLUDED.raw,
            fetched_at = now()
    """
    values = [
        (
            r["nct_id"], r.get("brief_title"), r.get("official_title"),
            r.get("overall_status"), r.get("phase"),
            r.get("study_type"), r.get("condition"),
            r.get("enrollment_count"), bool(r.get("has_results")),
            r.get("why_stopped"),
            _to_date(r.get("start_date")), _to_date(r.get("completion_date")),
            _to_date(r.get("last_update_date")),
            r.get("lead_sponsor"), r.get("device_category"),
            json.dumps(r.get("raw") or r, default=str),
        )
        for r in rows if r.get("nct_id")
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, values, page_size=200)
    return len(values)


def upsert_trial_interventions(conn, rows):
    if not rows:
        return 0
    rows = _dedupe_last(rows, lambda r: (r.get("nct_id"), r.get("intervention_name")))
    sql = """
        INSERT INTO clinical_trial_interventions (
            nct_id, intervention_name, intervention_type, description
        ) VALUES %s
        ON CONFLICT (nct_id, intervention_name) DO UPDATE SET
            intervention_type = EXCLUDED.intervention_type,
            description = EXCLUDED.description
    """
    values = [
        (r["nct_id"], r["intervention_name"],
         r.get("intervention_type"), r.get("description"))
        for r in rows if r.get("nct_id") and r.get("intervention_name")
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, values, page_size=200)
    return len(values)


def upsert_open_payments(conn, rows):
    """Mirrors the ClickHouse version but lands in Postgres cms_open_payments."""
    if not rows:
        return 0
    sql = """
        INSERT INTO cms_open_payments (
            dataset_id, year, record_id, physician_npi, physician_name,
            physician_specialty, teaching_hospital_ccn, teaching_hospital_name,
            manufacturer_name, product_name, product_category,
            nature_of_payment, payment_total, payment_date, raw
        ) VALUES %s
        ON CONFLICT DO NOTHING
    """
    values = []
    for r in rows:
        values.append((
            r.get("dataset_id"),
            r.get("year"),
            r.get("record_id"),
            r.get("physician_npi"),
            r.get("physician_name"),
            r.get("physician_specialty"),
            r.get("teaching_hospital_ccn"),
            r.get("teaching_hospital_name"),
            r.get("manufacturer_name"),
            r.get("product_name"),
            r.get("product_category"),
            r.get("nature_of_payment"),
            r.get("payment_total"),
            _to_date(r.get("payment_date")),
            json.dumps(r.get("raw") or r, default=str),
        ))
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, values, page_size=200)
    return len(values)


def upsert_risk_intelligence(conn, rows):
    """rows: list of dicts matching hospital_device_risk_intelligence columns.
    `payload` field should already be a Python dict — serialized here."""
    if not rows:
        return 0
    sql = """
        INSERT INTO hospital_device_risk_intelligence (
            ccn, device_category, product_code, hospital_name, state, quality_score,
            total_events, death_count, injury_count, malfunction_count,
            has_recall, worst_recall_class, trial_count, manufacturer_exposure,
            maude_score, recall_override, final_score,
            hospital_device_link_confidence, device_risk_confidence,
            linkage_type, payload, generated_at
        ) VALUES %s
        ON CONFLICT (ccn, device_category) DO UPDATE SET
            hospital_name = EXCLUDED.hospital_name,
            product_code = EXCLUDED.product_code,
            state = EXCLUDED.state,
            quality_score = EXCLUDED.quality_score,
            total_events = EXCLUDED.total_events,
            death_count = EXCLUDED.death_count,
            injury_count = EXCLUDED.injury_count,
            malfunction_count = EXCLUDED.malfunction_count,
            has_recall = EXCLUDED.has_recall,
            worst_recall_class = EXCLUDED.worst_recall_class,
            trial_count = EXCLUDED.trial_count,
            manufacturer_exposure = EXCLUDED.manufacturer_exposure,
            maude_score = EXCLUDED.maude_score,
            recall_override = EXCLUDED.recall_override,
            final_score = EXCLUDED.final_score,
            hospital_device_link_confidence = EXCLUDED.hospital_device_link_confidence,
            device_risk_confidence = EXCLUDED.device_risk_confidence,
            linkage_type = EXCLUDED.linkage_type,
            payload = EXCLUDED.payload,
            generated_at = now()
    """
    values = [
        (
            r["ccn"], r["device_category"], r.get("product_code"),
            r.get("hospital_name"), r.get("state"), r.get("quality_score"),
            r.get("total_events", 0), r.get("death_count", 0),
            r.get("injury_count", 0), r.get("malfunction_count", 0),
            bool(r.get("has_recall", False)), r.get("worst_recall_class"),
            r.get("trial_count", 0), r.get("manufacturer_exposure"),
            r.get("maude_score"), bool(r.get("recall_override", False)),
            r.get("final_score"),
            r.get("hospital_device_link_confidence"),
            r.get("device_risk_confidence"),
            r.get("linkage_type", "indirect"),
            json.dumps(r["payload"], default=str),
            _dt.datetime.now(_dt.timezone.utc),
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, values, page_size=100)
    return len(values)


# ---------------------------------------------------------------
# Audit log helpers
# ---------------------------------------------------------------

def start_log(conn, source, params=None):
    run_id = str(uuid.uuid4())
    started = _dt.datetime.now(_dt.timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO risk_ingestion_log (run_id, source, started_at, params, status)
               VALUES (%s, %s, %s, %s, 'running') RETURNING id""",
            (run_id, source, started, json.dumps(params or {})),
        )
        log_id = cur.fetchone()[0]
    return {"id": log_id, "run_id": run_id, "started_at": started}


def finish_log(conn, handle, rows_fetched, rows_upserted, status, error=None):
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE risk_ingestion_log
               SET finished_at = %s, rows_fetched = %s, rows_upserted = %s,
                   status = %s, error = %s
               WHERE id = %s""",
            (
                _dt.datetime.now(_dt.timezone.utc),
                int(rows_fetched or 0),
                int(rows_upserted or 0),
                status, error, handle["id"],
            ),
        )


# ---------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------

def _to_date(v):
    if v in (None, "", "NA", "N/A"):
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    s = str(v).strip()
    # openFDA often emits YYYYMMDD
    if len(s) == 8 and s.isdigit():
        try:
            return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))
        except ValueError:
            return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m"):
        try:
            return _dt.datetime.strptime(s[:len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    try:
        return _dt.datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
