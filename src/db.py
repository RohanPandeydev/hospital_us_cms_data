"""Postgres helpers: connection, DB/schema bootstrap."""

import os
import logging
import psycopg2
from psycopg2.extras import Json, execute_values

from . import config

log = logging.getLogger(__name__)

SCHEMA_FILE = os.path.join(os.path.dirname(__file__), "..", "schema.sql")


def connect(database=None):
    return psycopg2.connect(config.pg_dsn(database))


def ensure_database():
    """Create the target database if it does not exist."""
    conn = psycopg2.connect(config.pg_dsn(database="postgres"))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (config.PGDATABASE,))
            if cur.fetchone() is None:
                log.info("Creating database %s", config.PGDATABASE)
                cur.execute(f'CREATE DATABASE "{config.PGDATABASE}"')
            else:
                log.info("Database %s already exists", config.PGDATABASE)
    finally:
        conn.close()


def apply_schema():
    with open(SCHEMA_FILE, "r") as f:
        ddl = f.read()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
    log.info("Schema applied")


# ---- upsert helpers ----

def _dedupe_last(values, key_indices):
    """Keep the last occurrence of each unique key-tuple in a batch.

    Prevents Postgres "ON CONFLICT cannot affect row a second time" when
    upstream CMS data contains duplicate keys within a single INSERT batch.
    """
    seen = {}
    for v in values:
        key = tuple(v[i] for i in key_indices)
        seen[key] = v
    return list(seen.values())


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
        Json(row),
    )


def upsert_hospitals(conn, rows):
    if not rows:
        return 0
    values = [_hospital_tuple(r) for r in rows if r.get("facility_id")]
    values = _dedupe_last(values, key_indices=[0])  # facility_id
    if not values:
        return 0
    placeholders = "(" + ", ".join(["%s"] * len(HOSPITAL_COLUMNS)) + ")"
    sql = f"""
        INSERT INTO cms_hospitals ({", ".join(HOSPITAL_COLUMNS)})
        VALUES %s
        ON CONFLICT (facility_id) DO UPDATE SET
            facility_name       = EXCLUDED.facility_name,
            address             = EXCLUDED.address,
            city                = EXCLUDED.city,
            state               = EXCLUDED.state,
            zip_code            = EXCLUDED.zip_code,
            county_name         = EXCLUDED.county_name,
            telephone           = EXCLUDED.telephone,
            hospital_type       = EXCLUDED.hospital_type,
            hospital_ownership  = EXCLUDED.hospital_ownership,
            emergency_services  = EXCLUDED.emergency_services,
            birthing_friendly   = EXCLUDED.birthing_friendly,
            overall_rating      = EXCLUDED.overall_rating,
            raw                 = EXCLUDED.raw,
            fetched_at          = now();
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values, template=placeholders)
    return len(values)


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


def _to_num(v):
    if v in (None, "", "Not Available", "N/A", "NA"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_date(v):
    """Best-effort parse of CMS date strings (MM/DD/YYYY or YYYY-MM-DD)."""
    if not v:
        return None
    import datetime as _dt
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return _dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _first(row, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def _facility_measure_tuple(dataset_id, row):
    base_measure_id = _first(
        row,
        "measure_id", "measure_cd", "measure_code",
        "hcahps_measure_id",
        "measure_name_abbr", "measure_name",
    )
    # Some datasets (HCAHPS, etc.) emit many rows per measure_id distinguished
    # only by answer_description/question. Fold the qualifier into the key so
    # no data is silently dropped by dedupe/ON CONFLICT.
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
        measure_id,
        _first(row, "measure_name", "measure_id"),
        score, _to_num(score),
        lower, _to_num(lower),
        higher, _to_num(higher),
        _first(row, "compared_to_national", "compared_to_national_rate"),
        denom, _to_num(denom),
        start_raw, _to_date(start_raw),
        end_raw, _to_date(end_raw),
        _first(row, "footnote", "footnote_text", "footnote_cd"),
        Json(row),
    )


def upsert_facility_measures(conn, dataset_id, rows):
    if not rows:
        return 0
    values = [
        _facility_measure_tuple(dataset_id, r)
        for r in rows
        if r.get("facility_id")
    ]
    # dedupe on (dataset_id, facility_id, measure_id)
    values = _dedupe_last(values, key_indices=[0, 1, 2])
    if not values:
        return 0
    sql = f"""
        INSERT INTO cms_hospital_measures ({", ".join(FACILITY_MEASURE_COLUMNS)})
        VALUES %s
        ON CONFLICT (dataset_id, facility_id, measure_id) DO UPDATE SET
            measure_name         = EXCLUDED.measure_name,
            score                = EXCLUDED.score,
            score_num            = EXCLUDED.score_num,
            lower_estimate       = EXCLUDED.lower_estimate,
            lower_estimate_num   = EXCLUDED.lower_estimate_num,
            higher_estimate      = EXCLUDED.higher_estimate,
            higher_estimate_num  = EXCLUDED.higher_estimate_num,
            compared_to_national = EXCLUDED.compared_to_national,
            denominator          = EXCLUDED.denominator,
            denominator_num      = EXCLUDED.denominator_num,
            start_date           = EXCLUDED.start_date,
            start_date_parsed    = EXCLUDED.start_date_parsed,
            end_date             = EXCLUDED.end_date,
            end_date_parsed      = EXCLUDED.end_date_parsed,
            footnote             = EXCLUDED.footnote,
            raw                  = EXCLUDED.raw,
            fetched_at           = now();
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    return len(values)


STATE_MEASURE_COLUMNS = [
    "dataset_id", "state", "measure_id", "measure_name",
    "score", "compared_to_national", "raw",
]


def upsert_state_measures(conn, dataset_id, rows):
    if not rows:
        return 0
    values = [
        (
            dataset_id,
            r.get("state"),
            r.get("measure_id") or r.get("measure_cd"),
            r.get("measure_name"),
            r.get("score"),
            r.get("compared_to_national"),
            Json(r),
        )
        for r in rows
        if r.get("state")
    ]
    values = _dedupe_last(values, key_indices=[0, 1, 2])  # dataset_id, state, measure_id
    if not values:
        return 0
    sql = f"""
        INSERT INTO cms_state_measures ({", ".join(STATE_MEASURE_COLUMNS)})
        VALUES %s
        ON CONFLICT (dataset_id, state, measure_id) DO UPDATE SET
            measure_name         = EXCLUDED.measure_name,
            score                = EXCLUDED.score,
            compared_to_national = EXCLUDED.compared_to_national,
            raw                  = EXCLUDED.raw,
            fetched_at           = now();
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    return len(values)


NATIONAL_MEASURE_COLUMNS = [
    "dataset_id", "measure_id", "measure_name", "score", "raw",
]


# ---- wide-format facility snapshots (CJR, ASC Quality, etc.) ----

def upsert_wide_facility(conn, dataset_id, rows):
    if not rows:
        return 0
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
            r.get("year") or r.get("measure_year"),
            Json(r),
        ))
    # dedupe on (dataset_id, facility_id, year) — matches UNIQUE constraint
    values = _dedupe_last(values, key_indices=[0, 1, 6])
    if not values:
        return 0
    sql = """
        INSERT INTO cms_wide_facility_snapshots
            (dataset_id, facility_id, npi, facility_name, state, zip_code, year, raw)
        VALUES %s
        ON CONFLICT (dataset_id, facility_id, year) DO UPDATE SET
            npi           = EXCLUDED.npi,
            facility_name = EXCLUDED.facility_name,
            state         = EXCLUDED.state,
            zip_code      = EXCLUDED.zip_code,
            raw           = EXCLUDED.raw,
            fetched_at    = now();
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    return len(values)


# ---- footnote crosswalk ----

OPEN_PAYMENTS_COLUMNS = [
    "dataset_id", "year", "record_id",
    "physician_npi", "physician_name", "physician_specialty",
    "teaching_hospital_ccn", "teaching_hospital_name",
    "manufacturer_name", "product_name", "product_category",
    "nature_of_payment", "payment_total", "payment_date", "raw",
]


def _op_tuple(dataset_id, row):
    # Open Payments field names (2020+ schema)
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
    from datetime import datetime as _dt
    raw_date = _first(row, "date_of_payment", "Date_of_Payment")
    payment_date = None
    if raw_date:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                payment_date = _dt.strptime(raw_date[:10], fmt).date()
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
        Json(row),
    )


def upsert_open_payments(conn, dataset_id, rows):
    if not rows:
        return 0
    values = [_op_tuple(dataset_id, r) for r in rows]
    if not values:
        return 0
    sql = f"""
        INSERT INTO cms_open_payments ({", ".join(OPEN_PAYMENTS_COLUMNS)})
        VALUES %s
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    return len(values)


CMS_SUMMARY_COLUMNS = [
    "dataset_id", "year", "ccn", "npi", "referring_npi",
    "hcpcs_code", "drg_code", "drg_description",
    "provider_state", "provider_city", "provider_name",
    "total_services", "total_beneficiaries", "total_payment_amt", "raw",
]


def _num(v):
    if v in (None, "", "*"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _cms_summary_tuple(dataset_id, row):
    # Keys vary per dataset; try many common variants so the surfacing works
    # across Inpatient / Outpatient / DMEPOS / Physician.
    ccn = _first(row, "Rndrng_Prvdr_CCN", "Prvdr_CCN", "rndrng_prvdr_ccn",
                      "prvdr_ccn", "provider_ccn", "Facility_ID")
    npi = _first(row, "Rndrng_NPI", "Rfrg_NPI", "rndrng_npi", "rfrg_npi",
                      "Prvdr_NPI", "npi", "provider_npi", "referring_npi")
    ref_npi = _first(row, "Rfrg_NPI", "rfrg_npi", "Referring_NPI")
    hcpcs = _first(row, "HCPCS_Cd", "HCPCS_CD", "hcpcs_cd", "hcpcs_code",
                        "Betos_Cd")
    drg = _first(row, "DRG_Cd", "drg_cd", "MS_DRG_Cd", "ms_drg",
                       "DRG_Code")
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
        svc_count, bene_count, payment, Json(row),
    )


GUDID_COLUMNS = [
    "primary_di", "product_code", "product_codes_all",
    "brand_name", "company_name", "device_description",
    "gmdn_pt_name", "catalog_number", "version_model",
    "is_kit", "is_combination", "public_version_date", "raw",
]


def _gudid_tuple(row):
    # openFDA GUDID records are nested — flatten the bits we care about.
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
        bool(row.get("is_kit")),
        bool(row.get("is_combination_product")),
        pub_date,
        Json(row),
    )


def upsert_gudid(conn, rows):
    if not rows:
        return 0
    values = [_gudid_tuple(r) for r in rows]
    values = [v for v in values if v[0]]  # must have primary_di
    values = _dedupe_last(values, key_indices=[0])
    if not values:
        return 0
    sql = f"""
        INSERT INTO fda_gudid_devices ({", ".join(GUDID_COLUMNS)})
        VALUES %s
        ON CONFLICT (primary_di) DO UPDATE SET
            product_code        = EXCLUDED.product_code,
            product_codes_all   = EXCLUDED.product_codes_all,
            brand_name          = EXCLUDED.brand_name,
            company_name        = EXCLUDED.company_name,
            device_description  = EXCLUDED.device_description,
            gmdn_pt_name        = EXCLUDED.gmdn_pt_name,
            catalog_number      = EXCLUDED.catalog_number,
            version_model       = EXCLUDED.version_model,
            is_kit              = EXCLUDED.is_kit,
            is_combination      = EXCLUDED.is_combination,
            public_version_date = EXCLUDED.public_version_date,
            raw                 = EXCLUDED.raw,
            fetched_at          = now();
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    return len(values)


def upsert_cms_summary(conn, dataset_id, rows):
    if not rows:
        return 0
    values = [_cms_summary_tuple(dataset_id, r) for r in rows]
    if not values:
        return 0
    sql = f"""
        INSERT INTO cms_provider_summary ({", ".join(CMS_SUMMARY_COLUMNS)})
        VALUES %s
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    return len(values)


def _parse_fda_date(s):
    """openFDA dates are YYYYMMDD strings."""
    if not s or len(s) != 8 or not s.isdigit():
        return None
    import datetime as _dt
    try:
        return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError:
        return None


def _first_in_list(lst, *keys):
    """Pull first non-empty value from a list of dicts by any of the given keys."""
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
    """Collect a comma-joined string of values at lst[*][key]."""
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
        _first_in_list(devices, "manufacturer_d_name"),  # fallback for facility_name (MAUDE does not always have it)
        row.get("reporter_occupation_code"),
        _first_in_list(devices, "manufacturer_d_postal_code"),
        _join_list_field(patients, "patient_outcome"),
        _join_list_field(devices, "device_problems")
            or _join_list_field(devices, "device_report_product_code"),
        mdr_text,
        Json(row),
    )


def upsert_fda_events(conn, rows):
    if not rows:
        return 0
    values = [_fda_event_tuple(r) for r in rows if r.get("report_number")]
    values = _dedupe_last(values, key_indices=[0])  # report_number
    if not values:
        return 0
    sql = f"""
        INSERT INTO fda_maude_events ({", ".join(FDA_EVENT_COLUMNS)})
        VALUES %s
        ON CONFLICT (report_number) DO UPDATE SET
            event_type         = EXCLUDED.event_type,
            date_received      = EXCLUDED.date_received,
            date_of_event      = EXCLUDED.date_of_event,
            event_location     = EXCLUDED.event_location,
            report_source_code = EXCLUDED.report_source_code,
            mdr_report_key     = EXCLUDED.mdr_report_key,
            manufacturer_name  = EXCLUDED.manufacturer_name,
            facility_name      = EXCLUDED.facility_name,
            facility_state     = EXCLUDED.facility_state,
            facility_zip       = EXCLUDED.facility_zip,
            patient_outcomes   = EXCLUDED.patient_outcomes,
            device_problems    = EXCLUDED.device_problems,
            mdr_text           = EXCLUDED.mdr_text,
            raw                = EXCLUDED.raw,
            fetched_at         = now();
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values)

    # Now insert per-device rows (one event may have multiple devices)
    _insert_fda_devices(conn, rows)
    return len(values)


FDA_DEVICE_COLUMNS = [
    "report_number", "seq", "product_code", "brand_name", "generic_name",
    "manufacturer", "model_number", "catalog_number", "lot_number",
    "udi_di", "udi_public", "device_age", "device_availability", "raw",
]


def _insert_fda_devices(conn, events):
    """Replace-then-insert device rows for each event (events are unique by report_number)."""
    report_numbers = [e.get("report_number") for e in events if e.get("report_number")]
    if not report_numbers:
        return

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM fda_maude_devices WHERE report_number = ANY(%s)",
            (report_numbers,),
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
                (dev.get("udi_di") if isinstance(dev.get("udi_di"), str)
                 else _first_in_list([dev], "udi_di")),
                dev.get("udi_public"),
                dev.get("device_age_text") or dev.get("device_age"),
                dev.get("device_availability"),
                Json(dev),
            ))
    if not values:
        return
    sql = f"""
        INSERT INTO fda_maude_devices ({", ".join(FDA_DEVICE_COLUMNS)})
        VALUES %s
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values)


def upsert_footnote_crosswalk(conn, rows):
    if not rows:
        return 0
    values = [
        (r.get("footnote"), r.get("footnote_text"), Json(r))
        for r in rows if r.get("footnote")
    ]
    values = _dedupe_last(values, key_indices=[0])  # footnote
    if not values:
        return 0
    sql = """
        INSERT INTO cms_footnote_crosswalk (footnote, footnote_text, raw)
        VALUES %s
        ON CONFLICT (footnote) DO UPDATE SET
            footnote_text = EXCLUDED.footnote_text,
            raw           = EXCLUDED.raw,
            fetched_at    = now();
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    return len(values)


def upsert_national_measures(conn, dataset_id, rows):
    if not rows:
        return 0
    values = [
        (
            dataset_id,
            r.get("measure_id") or r.get("measure_cd"),
            r.get("measure_name"),
            r.get("score"),
            Json(r),
        )
        for r in rows
    ]
    values = _dedupe_last(values, key_indices=[0, 1])  # dataset_id, measure_id
    if not values:
        return 0
    sql = f"""
        INSERT INTO cms_national_measures ({", ".join(NATIONAL_MEASURE_COLUMNS)})
        VALUES %s
        ON CONFLICT (dataset_id, measure_id) DO UPDATE SET
            measure_name = EXCLUDED.measure_name,
            score        = EXCLUDED.score,
            raw          = EXCLUDED.raw,
            fetched_at   = now();
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    return len(values)


# ---- audit log helpers ----

def start_ingest_log(conn, dataset):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cms_ingestion_log (dataset_id, dataset_name, status)
            VALUES (%s, %s, 'running')
            RETURNING id
            """,
            (dataset["id"], dataset["name"]),
        )
        log_id = cur.fetchone()[0]
    conn.commit()
    return log_id


def finish_ingest_log(conn, log_id, rows_fetched, rows_upserted, status, error=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE cms_ingestion_log
               SET finished_at = now(),
                   rows_fetched = %s,
                   rows_upserted = %s,
                   status = %s,
                   error = %s
             WHERE id = %s
            """,
            (rows_fetched, rows_upserted, status, error, log_id),
        )
    conn.commit()
