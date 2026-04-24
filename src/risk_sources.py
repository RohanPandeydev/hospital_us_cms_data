"""Fetchers for the three risk-intelligence layers that aren't yet in the DB.

  * FDA recalls      — openFDA /device/enforcement.json
  * Clinical trials  — clinicaltrials.gov /api/v2/studies
  * Open Payments    — CMS DKAN datastore (openpaymentsdata.cms.gov)

These sit alongside the existing fda_client / cms_client so they stay
easy to run in isolation (`python run.py risk-ingest --source recalls`).
"""

import logging
import time
from typing import Iterable, Optional

import requests

from . import config

log = logging.getLogger(__name__)

# Default browser-style UA. Some CMS endpoints (notably the Open Payments
# DKAN datastore at openpaymentsdata.cms.gov) block bot-looking UAs with
# 403 Forbidden. Using a Mozilla/5.0 token gets us through while still
# being honest with a contact fragment.
_DEFAULT_UA = ("Mozilla/5.0 (compatible; rp360-risk-ingest/0.2; "
               "+https://github.com/rp360)")


# ------------------------------------------------------------------
# openFDA enforcement (device recalls)
# ------------------------------------------------------------------

def _http_get(session, url, params, max_retries=5, timeout=60):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 404:
                return {"results": [], "meta": {}}
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                log.warning("HTTP %s on %s (attempt %d/%d)",
                            r.status_code, url, attempt, max_retries)
                time.sleep(min(2 ** attempt, 30))
                continue
            r.raise_for_status()
        except requests.RequestException as e:
            last_err = e
            log.warning("Request error (%d/%d): %s", attempt, max_retries, e)
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"HTTP failed after {max_retries} attempts: {last_err}")


def iter_recalls(search: str = "", limit: Optional[int] = None) -> Iterable[dict]:
    """Yield FDA device recall enforcement rows (flattened for risk_db.upsert_recalls).

    Endpoint: https://api.fda.gov/device/enforcement.json
    Each record maps to one recall_number. openFDA skip limit (25k) applies —
    use `search` date ranges for bigger pulls.
    """
    url = f"{config.FDA_API_BASE}/device/enforcement.json"
    session = requests.Session()
    session.headers["User-Agent"] = _DEFAULT_UA
    page_size = min(config.FDA_PAGE_SIZE, 1000)
    skip = 0
    yielded = 0
    while True:
        if skip >= 25000:
            log.info("openFDA enforcement skip cap hit at %d", skip)
            return
        page = page_size
        if limit is not None:
            remaining = limit - yielded
            if remaining <= 0:
                return
            page = min(page, remaining)
        params = {"limit": page, "skip": skip}
        if search:
            params["search"] = search
        if config.FDA_API_KEY:
            params["api_key"] = config.FDA_API_KEY
        data = _http_get(session, url, params)
        rows = data.get("results") or []
        if not rows:
            return
        for r in rows:
            yield _flatten_recall(r)
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        skip += len(rows)
        if len(rows) < page:
            return


def _flatten_recall(r: dict) -> dict:
    openfda = r.get("openfda") or {}
    return {
        "recall_number":             r.get("recall_number"),
        "event_id":                  r.get("event_id"),
        # openFDA enforcement buries product_code in the `openfda` sub-object
        # (enriched from the 510(k)/PMA master) — fall back to the flat field
        # in the rare older-format rows that still carry it.
        "product_code":              _first_nonempty(openfda.get("product_code"))
                                     or r.get("product_code"),
        "firm_name":                 r.get("recalling_firm") or r.get("firm_name"),
        "brand_name":                _first_nonempty(openfda.get("brand_name"))
                                     or r.get("brand_name"),
        "product_description":       r.get("product_description"),
        "reason_for_recall":         r.get("reason_for_recall"),
        "recall_class":              r.get("classification") or r.get("recall_class"),
        "status":                    r.get("status"),
        "recall_initiation_date":    r.get("recall_initiation_date"),
        "center_classification_date": r.get("center_classification_date"),
        "termination_date":          r.get("termination_date"),
        "country":                   r.get("country"),
        "state":                     r.get("state"),
        "city":                      r.get("city"),
        "distribution_pattern":      r.get("distribution_pattern"),
        "product_quantity":          r.get("product_quantity"),
        "voluntary_mandated":        r.get("voluntary_mandated"),
        "raw":                       r,
    }


def _first_nonempty(v):
    if isinstance(v, list):
        for x in v:
            if x:
                return x
    return v or None


# ------------------------------------------------------------------
# openFDA 510(k) clearances — authoritative K#↔PC↔applicant table
# ------------------------------------------------------------------

def iter_510k(search: str = "", limit: Optional[int] = None) -> Iterable[dict]:
    """Yield openFDA /device/510k.json rows flattened for fda_510k.

    Every 510(k) has a single product_code and applicant (manufacturer).
    That's exactly what we need to resolve a recall's device identity when
    we know its K-number OR the applicant's name.
    """
    url = f"{config.FDA_API_BASE}/device/510k.json"
    session = requests.Session()
    session.headers["User-Agent"] = _DEFAULT_UA
    page_size = min(config.FDA_PAGE_SIZE, 1000)
    skip = 0
    yielded = 0
    while True:
        if skip >= 25000:
            log.info("openFDA 510k skip cap hit at %d", skip)
            return
        page = page_size
        if limit is not None:
            remaining = limit - yielded
            if remaining <= 0:
                return
            page = min(page, remaining)
        params = {"limit": page, "skip": skip}
        if search:
            params["search"] = search
        if config.FDA_API_KEY:
            params["api_key"] = config.FDA_API_KEY
        data = _http_get(session, url, params)
        rows = data.get("results") or []
        if not rows:
            return
        for r in rows:
            yield _flatten_510k(r)
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        skip += len(rows)
        if len(rows) < page:
            return


def _flatten_510k(r: dict) -> dict:
    """Pick the fields we need, keep the rest in `raw`."""
    return {
        "k_number":             r.get("k_number"),
        "applicant":            r.get("applicant"),
        "device_name":          r.get("device_name"),
        "product_code":         r.get("product_code"),
        "decision_date":        r.get("decision_date"),
        "decision_description": r.get("decision_description"),
        "clearance_type":       r.get("clearance_type"),
        "statement_or_summary": r.get("statement_or_summary"),
        "country_code":         r.get("country_code"),
        "postal_code":          r.get("postal_code"),
        "state":                r.get("state"),
        "raw":                  r,
    }


# ------------------------------------------------------------------
# ClinicalTrials.gov v2
# ------------------------------------------------------------------

CTG_BASE = "https://clinicaltrials.gov/api/v2"

# Device-family search buckets. Each ClinicalTrials.gov query is scoped
# to query.intr=bucket_term, and every row we ingest is pre-tagged with
# device_category so it joins directly to the bridge / risk aggregator.
# Categories here mirror the names used in drg_to_device_category so
# risk-build picks them up without a separate crosswalk step.
CTG_DEVICE_BUCKETS = [
    # Cardiovascular
    ("coronary stent",                 "coronary stent"),
    ("drug eluting stent",             "drug_eluting_stent"),
    ("bare metal stent",               "bare_metal_stent"),
    ("transcatheter aortic valve",     "cardiac_valve"),
    ("heart valve",                    "cardiac_valve"),
    ("mitral valve",                   "cardiac_valve"),
    ("cardiac pacemaker",              "pacemaker"),
    ("implantable cardioverter",       "defibrillator"),
    ("ventricular assist device",      "heart_assist_device"),
    ("total artificial heart",         "heart_assist_device"),
    ("impella",                        "heart_assist_device"),
    ("coronary artery bypass",         "cabg_conduit"),

    # Orthopedic / spinal
    ("hip prosthesis",                 "hip_knee_implant"),
    ("knee prosthesis",                "hip_knee_implant"),
    ("total hip replacement",          "hip_knee_implant"),
    ("total knee replacement",         "hip_knee_implant"),
    ("spinal fusion",                  "spinal_fusion_device"),
    ("pedicle screw",                  "spinal_fusion_device"),
    ("intervertebral disc",            "spinal_fusion_device"),

    # Neuro
    ("deep brain stimulation",         "neurostim_implant"),
    ("spinal cord stimulator",         "neurostim_implant"),
    ("vagus nerve stimulator",         "neurostim_implant"),
    ("sacral nerve stimulator",        "neurostim_implant"),

    # Other high-volume device families
    ("insulin pump",                   "insulin_pump"),
    ("intraocular lens",               "intraocular_lens"),
    ("urinary catheter",               "urinary_catheter"),
    ("CPAP",                           "cpap"),
    ("mechanical ventilator",          "home_ventilator"),
    ("hemodialysis",                   "hemodialysis_catheter"),
    ("cochlear implant",               "cochlear_implant"),
    ("breast implant",                 "breast_implant"),
    ("surgical mesh",                  "surgical_mesh"),
]


# ------------------------------------------------------------------
# openFDA MAUDE device adverse events — year-sliced so we can go past
# the openFDA 25k skip cap. Re-uses FDAClient under the hood.
# ------------------------------------------------------------------

def iter_maude_year_sliced(year_from: int, year_to: int,
                           per_year_limit: Optional[int] = None) -> Iterable[dict]:
    """Yield MAUDE device events year by year.

    openFDA caps skip at 25,000 for any single search. To pull deeper
    history we slice by date_received and loop year-by-year. Records
    are yielded as-is so the existing upsert_fda_events wiring in
    ingest.py / db.py can handle them unchanged.
    """
    from .fda_client import FDAClient
    client = FDAClient()
    for year in range(year_from, year_to + 1):
        # NB: spaces between the brackets, not "+". requests encodes a
        # literal "+" as "%2B" which openFDA treats as a plus character
        # and then throws a 500. Using spaces lets requests encode them
        # to "+" on the wire, which openFDA parses correctly.
        search = f"date_received:[{year}-01-01 TO {year}-12-31]"
        log.info("MAUDE slice year=%d", year)
        n = 0
        for row in client.iter_events(search=search, limit=per_year_limit):
            yield row
            n += 1
        log.info("  year=%d yielded %d", year, n)


# ------------------------------------------------------------------
# openFDA recalls, year-sliced. Same rationale as MAUDE.
# ------------------------------------------------------------------

def iter_recalls_year_sliced(year_from: int, year_to: int,
                             per_year_limit: Optional[int] = None) -> Iterable[dict]:
    for year in range(year_from, year_to + 1):
        search = f"recall_initiation_date:[{year}0101 TO {year}1231]"
        log.info("recalls slice year=%d", year)
        n = 0
        for row in iter_recalls(search=search, limit=per_year_limit):
            yield row
            n += 1
        log.info("  year=%d yielded %d", year, n)


def iter_trials(bucket_term: str, device_category: str,
                limit: Optional[int] = None) -> Iterable[dict]:
    """Yield ClinicalTrials.gov studies matching a device family.

    API v2 supports cursor pagination via pageToken. Each page up to 1000
    records. We use the "query.intr" (intervention) filter to scope to the
    device, and extract a compact set of fields.
    """
    session = requests.Session()
    session.headers["User-Agent"] = _DEFAULT_UA

    next_token = None
    yielded = 0
    page_size = 200
    url = f"{CTG_BASE}/studies"

    while True:
        if limit is not None and yielded >= limit:
            return
        params = {
            "query.intr": bucket_term,
            "pageSize": page_size,
            "countTotal": "false",
            "format": "json",
        }
        if next_token:
            params["pageToken"] = next_token
        data = _http_get(session, url, params)
        studies = data.get("studies") or []
        if not studies:
            return
        for study in studies:
            yield _flatten_trial(study, device_category)
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        next_token = data.get("nextPageToken")
        if not next_token:
            return


def _flatten_trial(study: dict, device_category: str) -> dict:
    proto = study.get("protocolSection", {}) or {}
    ident = proto.get("identificationModule", {}) or {}
    status_mod = proto.get("statusModule", {}) or {}
    design = proto.get("designModule", {}) or {}
    cond = proto.get("conditionsModule", {}) or {}
    sponsor = proto.get("sponsorCollaboratorsModule", {}) or {}
    arms = proto.get("armsInterventionsModule", {}) or {}
    results = study.get("hasResults") or bool(study.get("resultsSection"))

    phases = design.get("phases") or []
    conditions = cond.get("conditions") or []
    interventions = []
    for intv in (arms.get("interventions") or []):
        name = intv.get("name")
        if not name:
            continue
        interventions.append({
            "nct_id":            ident.get("nctId"),
            "intervention_name": name,
            "intervention_type": intv.get("type"),
            "description":       intv.get("description"),
        })

    enrollment = (design.get("enrollmentInfo") or {}).get("count")

    return {
        "nct_id":           ident.get("nctId"),
        "brief_title":      ident.get("briefTitle"),
        "official_title":   ident.get("officialTitle"),
        "overall_status":   status_mod.get("overallStatus"),
        "phase":            phases[0] if phases else None,
        "study_type":       design.get("studyType"),
        "condition":        conditions[0] if conditions else None,
        "enrollment_count": enrollment,
        "has_results":      results,
        "why_stopped":      status_mod.get("whyStopped"),
        "start_date":       (status_mod.get("startDateStruct") or {}).get("date"),
        "completion_date":  (status_mod.get("completionDateStruct") or {}).get("date"),
        "last_update_date": (status_mod.get("lastUpdateSubmitDate")
                             or (status_mod.get("lastUpdatePostDateStruct") or {}).get("date")),
        "lead_sponsor":     ((sponsor.get("leadSponsor") or {}).get("name")),
        "device_category":  device_category,
        "interventions":    interventions,
        "raw":              study,
    }


# ------------------------------------------------------------------
# Open Payments (CMS DKAN — openpaymentsdata.cms.gov)
# ------------------------------------------------------------------

OPEN_PAYMENTS_BASE = "https://openpaymentsdata.cms.gov/api/1/datastore/query"


def iter_open_payments(dataset_id: str, limit: Optional[int] = None,
                       page_size: int = 500) -> Iterable[dict]:
    """Yield rows from an Open Payments DKAN dataset.

    Dataset IDs change annually — pass the UUID for the General Payments
    dataset for the year you want.
    """
    session = requests.Session()
    session.headers["User-Agent"] = _DEFAULT_UA
    url = f"{OPEN_PAYMENTS_BASE}/{dataset_id}/0"
    offset = 0
    yielded = 0
    while True:
        if limit is not None and yielded >= limit:
            return
        page = page_size if limit is None else min(page_size, limit - yielded)
        data = _http_get(session, url, {"limit": page, "offset": offset})
        rows = data.get("results") or []
        if not rows:
            return
        for r in rows:
            yield _flatten_open_payment(r, dataset_id)
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        offset += len(rows)
        if len(rows) < page:
            return


def _flatten_open_payment(r: dict, dataset_id: str) -> dict:
    first = (r.get("Covered_Recipient_First_Name")
             or r.get("covered_recipient_first_name") or "")
    last = (r.get("Covered_Recipient_Last_Name")
            or r.get("covered_recipient_last_name") or "")
    full = (first + " " + last).strip() or None
    year = (r.get("Program_Year") or r.get("program_year"))
    try:
        year_int = int(str(year)[:4]) if year else None
    except (ValueError, TypeError):
        year_int = None
    try:
        pay = float(str(r.get("Total_Amount_of_Payment_USDollars")
                        or r.get("total_amount_of_payment_usdollars")
                        or "").replace(",", "") or 0)
    except (ValueError, TypeError):
        pay = None
    return {
        "dataset_id":             dataset_id,
        "year":                   year_int,
        "record_id":              r.get("Record_ID") or r.get("record_id"),
        "physician_npi":          r.get("Covered_Recipient_NPI") or r.get("covered_recipient_npi"),
        "physician_name":         full,
        "physician_specialty":    r.get("Covered_Recipient_Specialty_1")
                                  or r.get("covered_recipient_specialty_1"),
        "teaching_hospital_ccn":  r.get("Teaching_Hospital_CCN") or r.get("teaching_hospital_ccn"),
        "teaching_hospital_name": r.get("Teaching_Hospital_Name") or r.get("teaching_hospital_name"),
        "manufacturer_name":      r.get("Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name")
                                  or r.get("applicable_manufacturer_or_applicable_gpo_making_payment_name"),
        "product_name":           r.get("Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1")
                                  or r.get("name_of_drug_or_biological_or_device_or_medical_supply_1"),
        "product_category":       r.get("Product_Category_or_Therapeutic_Area_1")
                                  or r.get("product_category_or_therapeutic_area_1"),
        "nature_of_payment":      r.get("Nature_of_Payment_or_Transfer_of_Value")
                                  or r.get("nature_of_payment_or_transfer_of_value"),
        "payment_total":          pay,
        "payment_date":           r.get("Date_of_Payment") or r.get("date_of_payment"),
        "raw":                    r,
    }
