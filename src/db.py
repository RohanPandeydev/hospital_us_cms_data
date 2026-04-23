"""ClickHouse helpers: client, schema bootstrap, upsert-style batch inserts.

ClickHouse doesn't have ON CONFLICT. We use ReplacingMergeTree(fetched_at) so
that re-ingesting the same key (e.g. same facility_id) produces a newer row
that supersedes the old one after background merge. Reads that must see the
latest version use SELECT ... FINAL.

The public API mirrors the old psycopg2 module so callers don't change:
    connect()                          -> context-manager-compatible client wrapper
    ensure_database(), apply_schema()
    upsert_hospitals(conn, rows)
    upsert_facility_measures(conn, dataset_id, rows)
    ...
    start_ingest_log(conn, dataset)    -> opaque handle
    finish_ingest_log(conn, handle, fetched, upserted, status, error=None)
"""

import datetime as _dt
import json
import logging
import os
import re
import uuid

import clickhouse_connect

from . import config

log = logging.getLogger(__name__)

SCHEMA_FILE = os.path.join(os.path.dirname(__file__), "..", "schema_clickhouse.sql")


# ---------------------------------------------------------------
# Connection
# ---------------------------------------------------------------

def _raw_client(database=None):
    return clickhouse_connect.get_client(
        host=config.CLICKHOUSE_HOST,
        port=config.CLICKHOUSE_PORT,
        username=config.CLICKHOUSE_USER,
        password=config.CLICKHOUSE_PASSWORD,
        database=database if database is not None else config.CLICKHOUSE_DATABASE,
        secure=config.CLICKHOUSE_SECURE,
        connect_timeout=15,
        send_receive_timeout=300,
    )


class _ConnWrapper:
    """Lets callers use `with db.connect() as conn:` like they did for psycopg2."""
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.client.close()
        except Exception:
            pass

    # pass-through convenience
    def command(self, *a, **kw):
        return self.client.command(*a, **kw)

    def query(self, *a, **kw):
        return self.client.query(*a, **kw)

    def insert(self, *a, **kw):
        return self.client.insert(*a, **kw)


def connect(database=None):
    return _ConnWrapper(_raw_client(database))


def ensure_database():
    """Create the target database if it does not exist.

    Connect to the `default` DB first (always present on managed CH) and then
    issue CREATE DATABASE IF NOT EXISTS for the configured one.
    """
    target = config.CLICKHOUSE_DATABASE
    admin = _raw_client(database="default")
    try:
        admin.command(f"CREATE DATABASE IF NOT EXISTS `{target}`")
        log.info("Database %s ready", target)
    finally:
        admin.close()


_STMT_SPLIT_RE = re.compile(r";\s*(?:--[^\n]*\n)?\s*(?:\n|$)")


def apply_schema():
    with open(SCHEMA_FILE, "r") as f:
        ddl = f.read()
    # Strip line comments so split doesn't choke on trailing "-- ..."
    cleaned = "\n".join(
        line.split("--", 1)[0] for line in ddl.splitlines()
    )
    stmts = [s.strip() for s in cleaned.split(";") if s.strip()]
    client = _raw_client()
    try:
        for stmt in stmts:
            client.command(stmt)
    finally:
        client.close()
    log.info("Schema applied: %d statements", len(stmts))


# ---------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------

def _to_num(v):
    if v in (None, "", "Not Available", "N/A", "NA"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num(v):
    if v in (None, "", "*"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _to_date(v):
    if not v:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return _dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _parse_fda_date(s):
    """openFDA dates are YYYYMMDD strings."""
    if not s or len(s) != 8 or not s.isdigit():
        return None
    try:
        return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError:
        return None


def _first(row, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def _first_in_list(lst, *keys):
    if not lst:
        return None
    for entry in lst:
        if not isinstance(entry, dict):
            continue
        for k in keys:
            v = entry.get(k)
            if v not in (None, ""):
                return v if not isinstance(v, list) else (v[0] if v else None)
    return None


def _join_list_field(lst, key):
    if not lst:
        return None
    out = []
    for entry in lst:
        if not isinstance(entry, dict):
            continue
        v = entry.get(key)
        if isinstance(v, list):
            out.extend(str(x) for x in v if x)
        elif v:
            out.append(str(v))
    return ", ".join(out) if out else None


def _raw_json(row):
    try:
        return json.dumps(row, default=str, ensure_ascii=False)
    except Exception:
        return "{}"


def _dedupe_last(values, key_indices):
    seen = {}
    for v in values:
        key = tuple(v[i] for i in key_indices)
        seen[key] = v
    return list(seen.values())


def _insert(conn, table, columns, rows):
    """Thin wrapper around clickhouse_connect's .insert()."""
    if not rows:
        return 0
    client = conn.client if isinstance(conn, _ConnWrapper) else conn
    client.insert(table, rows, column_names=columns)
    return len(rows)


# ---------------------------------------------------------------
# Hospitals
# ---------------------------------------------------------------

HOSPITAL_COLUMNS = [
    "facility_id", "facility_name", "address", "city", "state",
    "zip_code", "county_name", "telephone", "hospital_type",
    "hospital_ownership", "emergency_services", "birthing_friendly",
    "overall_rating", "raw",
]


def _hospital_tuple(row):
    return (
        row.get("facility_id"),
        row.get("facility_name"),
        row.get("address"),
        row.get("citytown") or row.get("city"),
        row.get("state"),
        row.get("zip_code"),
        row.get("countyparish") or row.get("county_name"),
        row.get("telephone_number") or row.get("phone_number"),
        row.get("hospital_type"),
        row.get("hospital_ownership"),
        row.get("emergency_services"),
        row.get("meets_criteria_for_birthing_friendly_designation"),
        row.get("hospital_overall_rating"),
        _raw_json(row),
    )


def upsert_hospitals(conn, rows):
    values = [_hospital_tuple(r) for r in rows if r.get("facility_id")]
    values = _dedupe_last(values, key_indices=[0])
    return _insert(conn, "cms_hospitals", HOSPITAL_COLUMNS, values)


# ---------------------------------------------------------------
# Facility measures
# ---------------------------------------------------------------

FACILITY_MEASURE_COLUMNS = [
    "dataset_id", "facility_id", "measure_id", "measure_name",
    "score", "score_num",
    "lower_estimate", "lower_estimate_num",
    "higher_estimate", "higher_estimate_num",
    "compared_to_national",
    "denominator", "denominator_num",
    "start_date", "start_date_parsed",
    "end_date", "end_date_parsed",
    "footnote", "raw",
]


def _facility_measure_tuple(dataset_id, row):
    base_measure_id = _first(
        row,
        "measure_id", "measure_cd", "measure_code",
        "hcahps_measure_id",
        "measure_name_abbr", "measure_name",
    )
    qualifier = _first(
        row,
        "hcahps_answer_description", "hcahps_question",
        "answer_description",
    )
    if base_measure_id and qualifier and qualifier != base_measure_id:
        measure_id = f"{base_measure_id}|{qualifier}"
    else:
        measure_id = base_measure_id
    score = _first(
        row,
        "score", "rate", "value",
        "excess_readmission_ratio",
        "standardized_infection_ratio", "sir",
        "hai_1_sir", "hai_2_sir",
        "predicted_readmission_rate",
    )
    lower = _first(row, "lower_estimate", "lower_limit", "ci_lower", "lower_confidence_limit")
    higher = _first(row, "higher_estimate", "upper_limit", "ci_upper", "upper_confidence_limit")
    denom = _first(
        row, "denominator", "number_of_discharges", "volume",
        "number_of_patients", "number_of_procedures", "eligible_cases",
    )
    start_raw = _first(row, "start_date", "measure_start_date", "measure_start_quarter")
    end_raw = _first(row, "end_date", "measure_end_date", "measure_end_quarter")
    return (
        dataset_id,
        row.get("facility_id"),
        measure_id or "",
        _first(row, "measure_name", "measure_id"),
        score, _to_num(score),
        lower, _to_num(lower),
        higher, _to_num(higher),
        _first(row, "compared_to_national", "compared_to_national_rate"),
        denom, _to_num(denom),
        start_raw, _to_date(start_raw),
        end_raw, _to_date(end_raw),
        _first(row, "footnote", "footnote_text", "footnote_cd"),
        _raw_json(row),
    )


def upsert_facility_measures(conn, dataset_id, rows):
    values = [
        _facility_measure_tuple(dataset_id, r)
        for r in rows
        if r.get("facility_id")
    ]
    values = _dedupe_last(values, key_indices=[0, 1, 2])
    return _insert(conn, "cms_hospital_measures", FACILITY_MEASURE_COLUMNS, values)


# ---------------------------------------------------------------
# State & national measures
# ---------------------------------------------------------------

STATE_MEASURE_COLUMNS = [
    "dataset_id", "state", "measure_id", "measure_name",
    "score", "compared_to_national", "raw",
]


def upsert_state_measures(conn, dataset_id, rows):
    values = [
        (
            dataset_id,
            r.get("state"),
            r.get("measure_id") or r.get("measure_cd") or "",
            r.get("measure_name"),
            r.get("score"),
            r.get("compared_to_national"),
            _raw_json(r),
        )
        for r in rows
        if r.get("state")
    ]
    values = _dedupe_last(values, key_indices=[0, 1, 2])
    return _insert(conn, "cms_state_measures", STATE_MEASURE_COLUMNS, values)


NATIONAL_MEASURE_COLUMNS = [
    "dataset_id", "measure_id", "measure_name", "score", "raw",
]


def upsert_national_measures(conn, dataset_id, rows):
    values = [
        (
            dataset_id,
            r.get("measure_id") or r.get("measure_cd") or "",
            r.get("measure_name"),
            r.get("score"),
            _raw_json(r),
        )
        for r in rows
    ]
    values = _dedupe_last(values, key_indices=[0, 1])
    return _insert(conn, "cms_national_measures", NATIONAL_MEASURE_COLUMNS, values)


# ---------------------------------------------------------------
# Wide-format facility snapshots
# ---------------------------------------------------------------

WIDE_FACILITY_COLUMNS = [
    "dataset_id", "facility_id", "npi", "facility_name",
    "state", "zip_code", "year", "raw",
]


def upsert_wide_facility(conn, dataset_id, rows):
    values = []
    for r in rows:
        fid = r.get("facility_id")
        if not fid:
            continue
        values.append((
            dataset_id,
            fid,
            r.get("npi"),
            r.get("facility_name"),
            r.get("state"),
            r.get("zip_code"),
            str(r.get("year") or r.get("measure_year") or ""),
            _raw_json(r),
        ))
    values = _dedupe_last(values, key_indices=[0, 1, 6])
    return _insert(conn, "cms_wide_facility_snapshots", WIDE_FACILITY_COLUMNS, values)


# ---------------------------------------------------------------
# Footnote crosswalk
# ---------------------------------------------------------------

def upsert_footnote_crosswalk(conn, rows):
    values = [
        (r.get("footnote"), r.get("footnote_text"), _raw_json(r))
        for r in rows if r.get("footnote")
    ]
    values = _dedupe_last(values, key_indices=[0])
    return _insert(conn, "cms_footnote_crosswalk",
                   ["footnote", "footnote_text", "raw"], values)


# ---------------------------------------------------------------
# FDA MAUDE
# ---------------------------------------------------------------

FDA_EVENT_COLUMNS = [
    "report_number", "event_type", "date_received", "date_of_event",
    "event_location", "report_source_code", "mdr_report_key",
    "manufacturer_name", "facility_name", "facility_state", "facility_zip",
    "patient_outcomes", "device_problems", "mdr_text", "raw",
]


def _fda_event_tuple(row):
    devices = row.get("device") or []
    patients = row.get("patient") or []
    texts = row.get("mdr_text") or []
    mdr_text = _join_list_field(texts, "text")
    if mdr_text and len(mdr_text) > 4000:
        mdr_text = mdr_text[:4000]
    return (
        row.get("report_number"),
        row.get("event_type"),
        _parse_fda_date(row.get("date_received")),
        _parse_fda_date(row.get("date_of_event")),
        row.get("event_location"),
        row.get("report_source_code"),
        row.get("mdr_report_key"),
        _first_in_list(devices, "manufacturer_d_name", "manufacturer_g1_name"),
        _first_in_list(devices, "manufacturer_d_name"),
        row.get("reporter_occupation_code"),
        _first_in_list(devices, "manufacturer_d_postal_code"),
        _join_list_field(patients, "patient_outcome"),
        _join_list_field(devices, "device_problems")
            or _join_list_field(devices, "device_report_product_code"),
        mdr_text,
        _raw_json(row),
    )


FDA_DEVICE_COLUMNS = [
    "report_number", "seq", "product_code", "brand_name", "generic_name",
    "manufacturer", "model_number", "catalog_number", "lot_number",
    "udi_di", "udi_public", "device_age", "device_availability", "raw",
]


def _replace_fda_devices(conn, events):
    """Clear existing device rows for these report_numbers, then insert fresh.

    DELETE on ClickHouse is a mutation — slow under heavy write load but fine
    for our cadence (ingests run periodically, not continuously).
    """
    report_numbers = [e.get("report_number") for e in events if e.get("report_number")]
    if not report_numbers:
        return

    client = conn.client if isinstance(conn, _ConnWrapper) else conn
    # Build an IN list safely via clickhouse_connect parameters
    client.command(
        "ALTER TABLE fda_maude_devices DELETE WHERE report_number IN {rns:Array(String)}",
        parameters={"rns": report_numbers},
    )

    values = []
    for event in events:
        rn = event.get("report_number")
        if not rn:
            continue
        for i, dev in enumerate(event.get("device") or [], start=1):
            values.append((
                rn, i,
                dev.get("device_report_product_code"),
                dev.get("brand_name"),
                dev.get("generic_name"),
                dev.get("manufacturer_d_name") or dev.get("manufacturer_g1_name"),
                dev.get("model_number"),
                dev.get("catalog_number"),
                dev.get("lot_number"),
                dev.get("udi_di") if isinstance(dev.get("udi_di"), str)
                    else _first_in_list([dev], "udi_di"),
                dev.get("udi_public"),
                dev.get("device_age_text") or dev.get("device_age"),
                dev.get("device_availability"),
                _raw_json(dev),
            ))
    _insert(conn, "fda_maude_devices", FDA_DEVICE_COLUMNS, values)


def upsert_fda_events(conn, rows):
    values = [_fda_event_tuple(r) for r in rows if r.get("report_number")]
    values = _dedupe_last(values, key_indices=[0])
    if not values:
        return 0
    _insert(conn, "fda_maude_events", FDA_EVENT_COLUMNS, values)
    _replace_fda_devices(conn, rows)
    return len(values)


# ---------------------------------------------------------------
# FDA GUDID
# ---------------------------------------------------------------

GUDID_COLUMNS = [
    "primary_di", "product_code", "product_codes_all",
    "brand_name", "company_name", "device_description",
    "gmdn_pt_name", "catalog_number", "version_model",
    "is_kit", "is_combination", "public_version_date", "raw",
]


def _gudid_tuple(row):
    identifiers = row.get("identifiers") or []
    primary_di = None
    for ident in identifiers:
        if isinstance(ident, dict) and ident.get("type") == "Primary":
            primary_di = ident.get("id")
            break
    if not primary_di and identifiers:
        primary_di = identifiers[0].get("id") if isinstance(identifiers[0], dict) else None

    product_codes = row.get("product_codes") or []
    pc_list = []
    for pc in product_codes:
        if isinstance(pc, dict):
            code = pc.get("code")
            if code:
                pc_list.append(code)
    primary_pc = pc_list[0] if pc_list else None
    pc_all = ",".join(pc_list) if pc_list else None

    gmdn = (row.get("gmdn_terms") or [{}])[0] if row.get("gmdn_terms") else {}
    raw_date = row.get("public_version_date")
    pub_date = _to_date(raw_date) if raw_date else None

    return (
        primary_di,
        primary_pc,
        pc_all,
        row.get("brand_name"),
        row.get("company_name"),
        row.get("device_description"),
        gmdn.get("name") if isinstance(gmdn, dict) else None,
        row.get("catalog_number"),
        row.get("version_or_model_number"),
        1 if row.get("is_kit") else 0,
        1 if row.get("is_combination_product") else 0,
        pub_date,
        _raw_json(row),
    )


def upsert_gudid(conn, rows):
    values = [_gudid_tuple(r) for r in rows]
    values = [v for v in values if v[0]]
    values = _dedupe_last(values, key_indices=[0])
    return _insert(conn, "fda_gudid_devices", GUDID_COLUMNS, values)


# ---------------------------------------------------------------
# CMS provider summary (Medicare utilization, DMEPOS)
# ---------------------------------------------------------------

CMS_SUMMARY_COLUMNS = [
    "dataset_id", "year", "ccn", "npi", "referring_npi",
    "hcpcs_code", "drg_code", "drg_description",
    "provider_state", "provider_city", "provider_name",
    "total_services", "total_beneficiaries", "total_payment_amt", "raw",
]


def _cms_summary_tuple(dataset_id, row):
    ccn = _first(row, "Rndrng_Prvdr_CCN", "Prvdr_CCN", "rndrng_prvdr_ccn",
                      "prvdr_ccn", "provider_ccn", "Facility_ID")
    npi = _first(row, "Rndrng_NPI", "Rfrg_NPI", "rndrng_npi", "rfrg_npi",
                      "Prvdr_NPI", "npi", "provider_npi", "referring_npi")
    ref_npi = _first(row, "Rfrg_NPI", "rfrg_npi", "Referring_NPI")
    hcpcs = _first(row, "HCPCS_Cd", "HCPCS_CD", "hcpcs_cd", "hcpcs_code",
                        "Betos_Cd")
    drg = _first(row, "DRG_Cd", "drg_cd", "MS_DRG_Cd", "ms_drg", "DRG_Code")
    drg_desc = _first(row, "DRG_Desc", "drg_desc", "DRG_Description")
    state = _first(row, "Rndrng_Prvdr_State_Abrvtn", "Prvdr_State_Abrvtn",
                        "rndrng_prvdr_state_abrvtn", "Rfrg_Prvdr_State_Abrvtn",
                        "state", "Rndrng_Prvdr_State_FIPS", "Sbmttd_State_Cd")
    city = _first(row, "Rndrng_Prvdr_City", "rndrng_prvdr_city",
                       "Prvdr_City", "city")
    name = _first(row, "Rndrng_Prvdr_Org_Name", "rndrng_prvdr_org_name",
                       "Prvdr_Org_Name", "Rndrng_Prvdr_Last_Org_Name",
                       "provider_name", "Facility_Name")
    year = _first(row, "Year", "year")
    try:
        year_int = int(year) if year else None
    except (ValueError, TypeError):
        year_int = None
    svc_count = _num(_first(row, "Tot_Dschrgs", "Tot_Srvcs", "tot_srvcs",
                                  "Srvc_Count", "Total_Services"))
    bene_count = _num(_first(row, "Tot_Benes", "tot_benes", "Total_Beneficiaries"))
    payment = _num(_first(row, "Avg_Tot_Pymt_Amt", "Avg_Mdcr_Pymt_Amt",
                                "Tot_Mdcr_Pymt_Amt", "avg_tot_pymt_amt",
                                "Avg_Submtd_Chrg"))
    return (
        dataset_id, year_int, ccn, npi, ref_npi,
        hcpcs, drg, drg_desc,
        state, city, name,
        svc_count, bene_count, payment, _raw_json(row),
    )


def upsert_cms_summary(conn, dataset_id, rows):
    values = [_cms_summary_tuple(dataset_id, r) for r in rows]
    return _insert(conn, "cms_provider_summary", CMS_SUMMARY_COLUMNS, values)


# ---------------------------------------------------------------
# Open Payments
# ---------------------------------------------------------------

OPEN_PAYMENTS_COLUMNS = [
    "dataset_id", "year", "record_id",
    "physician_npi", "physician_name", "physician_specialty",
    "teaching_hospital_ccn", "teaching_hospital_name",
    "manufacturer_name", "product_name", "product_category",
    "nature_of_payment", "payment_total", "payment_date", "raw",
]


def _op_tuple(dataset_id, row):
    year = _first(row, "program_year", "Program_Year")
    try:
        year_int = int(str(year)[:4]) if year else None
    except (ValueError, TypeError):
        year_int = None
    payment_total = _num(_first(
        row,
        "total_amount_of_payment_usdollars",
        "Total_Amount_of_Payment_USDollars",
    ))
    raw_date = _first(row, "date_of_payment", "Date_of_Payment")
    payment_date = None
    if raw_date:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                payment_date = _dt.datetime.strptime(raw_date[:10], fmt).date()
                break
            except ValueError:
                continue

    first = _first(row, "covered_recipient_first_name", "Covered_Recipient_First_Name") or ""
    last = _first(row, "covered_recipient_last_name", "Covered_Recipient_Last_Name") or ""
    full = (first + " " + last).strip() or None

    return (
        dataset_id,
        year_int,
        _first(row, "record_id", "Record_ID"),
        _first(row, "covered_recipient_npi", "Covered_Recipient_NPI", "Physician_NPI"),
        full,
        _first(row, "covered_recipient_primary_type_1", "covered_recipient_specialty_1",
                    "Covered_Recipient_Specialty_1"),
        _first(row, "teaching_hospital_ccn", "Teaching_Hospital_CCN"),
        _first(row, "teaching_hospital_name", "Teaching_Hospital_Name"),
        _first(row, "applicable_manufacturer_or_applicable_gpo_making_payment_name",
                    "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
                    "submitting_applicable_manufacturer_or_applicable_gpo_name"),
        _first(row, "name_of_drug_or_biological_or_device_or_medical_supply_1",
                    "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1"),
        _first(row, "product_category_or_therapeutic_area_1",
                    "Product_Category_or_Therapeutic_Area_1"),
        _first(row, "nature_of_payment_or_transfer_of_value",
                    "Nature_of_Payment_or_Transfer_of_Value"),
        payment_total,
        payment_date,
        _raw_json(row),
    )


def upsert_open_payments(conn, dataset_id, rows):
    values = [_op_tuple(dataset_id, r) for r in rows]
    return _insert(conn, "cms_open_payments", OPEN_PAYMENTS_COLUMNS, values)


# ---------------------------------------------------------------
# Ingestion audit log
# ---------------------------------------------------------------

INGEST_LOG_COLUMNS = [
    "run_id", "dataset_id", "dataset_name", "started_at", "finished_at",
    "rows_fetched", "rows_upserted", "status", "error",
]


def start_ingest_log(conn, dataset):
    """Return an in-memory handle. The row lands in ClickHouse only on finish.

    ClickHouse mutations for UPDATE are expensive, and the running row isn't
    really useful to anyone (the tqdm bar covers live progress). So we buffer
    start state and write once.
    """
    return {
        "run_id": str(uuid.uuid4()),
        "dataset_id": dataset["id"],
        "dataset_name": dataset.get("name"),
        "started_at": _dt.datetime.now(_dt.timezone.utc),
    }


def finish_ingest_log(conn, handle, rows_fetched, rows_upserted, status, error=None):
    row = (
        handle["run_id"],
        handle["dataset_id"],
        handle["dataset_name"],
        handle["started_at"],
        _dt.datetime.now(_dt.timezone.utc),
        int(rows_fetched or 0),
        int(rows_upserted or 0),
        status,
        error,
    )
    _insert(conn, "cms_ingestion_log", INGEST_LOG_COLUMNS, [row])
