"""HCPCS Explorer — single-purpose UI.

Routes:
  GET  /                  → home: search + top HCPCS codes
  GET  /search?q=…        → matches by code or description
  GET  /hcpcs/{code}      → full cross-source view for one HCPCS code
  GET  /hospitals         → all 5,852 hospitals + readmission/quality measures
  GET  /hospital/{ccn}    → single-hospital quality profile (all measures)

Per HCPCS code:  master + OPPS Addendum B + Stark DHS + CMS billing
                 + Open Payments (fuzzy) + linked clinical trials.
Per hospital:    address/rating + every measure (HRRP readmissions, HAI,
                 HCAHPS, complications, PSI) with national-benchmark
                 comparison.

Public CMS data note: there's no row with BOTH a CCN and a HCPCS code,
so we can't directly link "hospital X billed HCPCS Y." The /hospital
pages stand alone for hospital-quality questions; HCPCS pages stand
alone for code-level questions.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import db  # noqa: E402
from src.measure_dict import decode_measure, decode_dataset  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Expose the measure decoder to templates so we can render plain-English
# names + descriptions next to every CMS measure ID.
TEMPLATES.env.globals["decode_measure"] = decode_measure
TEMPLATES.env.globals["decode_dataset"] = decode_dataset


def explain_score(measure_id: str, score, compared: str) -> dict:
    """Return a dict {verdict, color, hint} interpreting a measure's score."""
    label, _, _, direction, threshold = decode_measure(measure_id)
    c = (compared or "").lower()
    if c:
        if "better" in c or "fewer" in c or "less" in c:
            return {"verdict": "Better", "color": "good", "hint": "Better than the U.S. average."}
        if "worse" in c or "more" in c or "greater" in c:
            return {"verdict": "Worse", "color": "bad", "hint": "Worse than the U.S. average."}
        if "no different" in c:
            return {"verdict": "Average", "color": "neutral", "hint": "In line with the U.S. national average."}
    if score is not None and threshold is not None:
        try:
            v = float(score)
            if direction == "lower":
                if v < threshold:
                    return {"verdict": "Better", "color": "good", "hint": f"Score {v:.2f} is below threshold {threshold} — good."}
                if v > threshold:
                    return {"verdict": "Worse", "color": "bad", "hint": f"Score {v:.2f} is above threshold {threshold} — worse than expected."}
        except (TypeError, ValueError):
            pass
    return {"verdict": "—", "color": "neutral", "hint": ""}


def format_score(measure_id: str, score, denominator) -> dict:
    """Return a dict that explains the raw score in plain English.

    Different measure families use different score scales:
    - Raw % rates (READM_30_*, MORT_30_*, OP_*)  → "X% — about N of M patients"
    - HRRP ratios (*-HRRP)                       → "ratio of 1.00 = exactly expected"
    - PSI rates                                  → "per 1,000 discharges"
    - HAI SIRs                                   → "infection ratio: 1.0 = expected"
    - EDAC                                       → "extra/fewer days per 100 discharges"
    """
    if score is None:
        return {"display": "—", "explain": "", "estimate": ""}
    try:
        v = float(score)
    except (TypeError, ValueError):
        return {"display": str(score), "explain": "", "estimate": ""}

    mid = measure_id or ""
    try:
        denom = int(denominator) if denominator not in (None, "", "Not Available", "Not Applicable", "N/A") else None
    except (TypeError, ValueError):
        denom = None

    # HRRP ratios — 1.0 = expected, >1.0 worse, <1.0 better
    if "HRRP" in mid:
        if v < 1.0:
            pct_better = (1.0 - v) * 100
            return {
                "display": f"{v:.2f}",
                "explain": f"Ratio of actual ÷ expected readmissions. {v:.2f} means this hospital readmits about {pct_better:.0f}% fewer patients than CMS predicts. Anything below 1.0 is better than expected.",
                "estimate": "",
            }
        elif v > 1.0:
            pct_worse = (v - 1.0) * 100
            return {
                "display": f"{v:.2f}",
                "explain": f"Ratio of actual ÷ expected readmissions. {v:.2f} means about {pct_worse:.0f}% MORE patients readmitted than CMS predicts. >1.0 = Medicare reduces payment.",
                "estimate": "",
            }
        else:
            return {
                "display": f"{v:.2f}",
                "explain": "Actual readmissions exactly match the CMS-predicted rate for this hospital's case mix.",
                "estimate": "",
            }

    # Raw % readmission / mortality / outpatient-visit rates
    if mid.startswith("READM_30_") or mid.startswith("MORT_30_") or mid in ("Hybrid_HWR", "Hybrid_HWM", "OP_32"):
        if denom:
            count = round(v / 100 * denom)
            verb = "readmitted" if "READM" in mid else "died" if "MORT" in mid else "had unplanned visits"
            return {
                "display": f"{v:.1f}%",
                "explain": f"{v:.1f}% of {denom:,} patients {verb} within 30 days.",
                "estimate": f"≈ {count:,} of {denom:,} patients",
            }
        return {
            "display": f"{v:.1f}%",
            "explain": f"{v:.1f}% of qualifying patients had this outcome.",
            "estimate": "",
        }

    # EDAC — excess days per 100 discharges (negative = fewer than average)
    if mid.startswith("EDAC_30_"):
        sign = "more" if v > 0 else "fewer"
        return {
            "display": f"{v:+.1f}",
            "explain": f"On average, {abs(v):.1f} {sign} days in acute care per 100 discharges than the national benchmark, summed across the 30 days post-discharge.",
            "estimate": "",
        }

    # PSI — rate per 1,000 discharges (mostly)
    if mid.startswith("PSI_") and mid != "PSI_04":
        return {
            "display": f"{v:.2f}",
            "explain": f"{v:.2f} events per 1,000 at-risk patients. Lower is better.",
            "estimate": "",
        }
    if mid == "PSI_04":
        return {
            "display": f"{v:.1f}",
            "explain": f"{v:.1f} deaths per 1,000 surgical patients with serious treatable complications. Lower is better.",
            "estimate": "",
        }
    if mid == "PSI_90":
        return {
            "display": f"{v:.2f}",
            "explain": f"Composite safety risk score. 1.0 = national average; lower is safer.",
            "estimate": "",
        }

    # HAI Standardized Infection Ratios
    if mid.startswith("HAI_"):
        return {
            "display": f"{v:.2f}",
            "explain": f"Infection ratio (actual ÷ predicted). 1.0 = expected; <1.0 better; >1.0 worse.",
            "estimate": "",
        }

    # Default
    return {"display": f"{v:.2f}", "explain": "", "estimate": ""}


TEMPLATES.env.globals["explain_score"] = explain_score
TEMPLATES.env.globals["format_score"] = format_score


# OPPS Status Indicator codes — what CMS uses to decide how to pay.
# https://www.cms.gov/files/document/r12-payment-status-indicators.pdf
OPPS_SI = {
    "A":  ("Paid under another fee schedule",         "Not OPPS — paid via the Physician Fee Schedule, Lab Fee Schedule, or DME — typically for codes used in non-hospital settings."),
    "B":  ("Not paid under OPPS",                     "Generally excluded from OPPS payment (e.g. inpatient-only or pkg into another payment system)."),
    "C":  ("Inpatient-only",                          "Procedure can only be billed when performed in an inpatient setting."),
    "E1": ("Not covered by Medicare",                 "Medicare does not pay for this item or service."),
    "E2": ("Not paid by Medicare under any system",   "Item is not separately payable; no APC, no fee schedule."),
    "F":  ("Acquisition of corneal tissue",           "Paid at hospital's reasonable cost — not under OPPS."),
    "G":  ("Pass-through drugs and biologicals",      "New drugs/biologicals get separate temporary payment until tech moves to standard APC."),
    "H":  ("Pass-through device categories",          "New high-cost device categories — separate transitional payment."),
    "J1": ("Comprehensive APC (C-APC)",               "One bundled OPPS payment covers the primary procedure plus all related services on the same claim."),
    "J2": ("Comprehensive APC for observation",       "Bundled C-APC for hospital observation services."),
    "K":  ("Non-pass-through drugs",                  "Drug billed separately under OPPS — paid at its own APC rate."),
    "L":  ("Influenza & PPV vaccines",                "Paid at hospital's reasonable cost — separate from OPPS APCs."),
    "M":  ("Items / services not billable to MAC",    "Items rarely paid through the OPPS system."),
    "N":  ("Packaged into another payment",           "Cost is bundled into the parent procedure's APC payment. Hospitals don't get paid separately for this code."),
    "P":  ("Partial hospitalization",                 "Per-diem APC payment for psychiatric/partial-hospitalization services."),
    "Q1": ("STV-packaged codes",                      "Paid separately if billed alone; packaged when billed with an S/T/V procedure."),
    "Q2": ("T-packaged codes",                        "Paid separately if billed alone; packaged when billed with a T procedure."),
    "Q3": ("Composite APCs",                          "Triggers a bundled APC when specific code combinations are billed together."),
    "Q4": ("Conditionally packaged labs",             "Lab tests paid via clinical lab fee schedule when billed alone; packaged into hospital OPPS otherwise."),
    "R":  ("Blood and blood products",                "Paid at separate APC rate for blood products."),
    "S":  ("Significant procedures, not discounted",  "Separately payable surgical procedure under OPPS."),
    "T":  ("Significant procedures, multi-procedure discounted","Separately payable surgical procedure; multiple procedures on same day get a reduction."),
    "U":  ("Brachytherapy sources",                   "Separate APC payment specific to brachytherapy seeds/devices."),
    "V":  ("Clinic / ED visits",                      "Visit-based codes — outpatient evaluation & management."),
    "Y":  ("Non-implantable DME (paid under DME fee schedule)", "Durable medical equipment paid via the DMEPOS fee schedule — not under hospital OPPS. Suppliers, not hospitals, bill these."),
}


def explain_si(si: str | None) -> dict:
    if not si:
        return {"label": "—", "explain": ""}
    si = si.strip()
    if si in OPPS_SI:
        label, explain = OPPS_SI[si]
        return {"label": label, "explain": explain}
    return {"label": si, "explain": "Unrecognized status indicator."}


TEMPLATES.env.globals["explain_si"] = explain_si


# Decode device-family bucket labels from Groq clinical-trial mapping
DEVICE_BUCKETS = {
    "cpap":                 "CPAP / sleep apnea",
    "cabg_conduit":         "Coronary bypass graft",
    "cardiac_valve":        "Heart valve",
    "coronary stent":       "Coronary stent",
    "drug_eluting_stent":   "Drug-eluting coronary stent",
    "bare_metal_stent":     "Bare metal coronary stent",
    "biliary_stent":        "Biliary stent",
    "ercp_stent":           "ERCP biliary stent",
    "ercp_lams":            "ERCP lumen-apposing metal stent",
    "defibrillator":        "Implantable cardioverter-defibrillator (ICD)",
    "pacemaker":            "Pacemaker",
    "heart_assist_device":  "Ventricular assist / heart pump",
    "laa_closure":          "Left atrial appendage closure",
    "pulse_field_ablation": "Pulse-field cardiac ablation",
    "hip_knee_implant":     "Hip/knee replacement",
    "spinal_fusion_device": "Spinal fusion hardware",
    "neurostim_implant":    "Implantable neurostimulator",
    "neurostimulator":      "Neurostimulator",
    "insulin_pump":         "Insulin pump",
    "intraocular_lens":     "Intraocular lens (cataract)",
    "urinary_catheter":     "Urinary catheter",
    "hemodialysis_catheter":"Hemodialysis catheter",
    "cochlear_implant":     "Cochlear implant",
    "breast_implant":       "Breast implant",
    "surgical_mesh":        "Surgical mesh",
    "home_ventilator":      "Mechanical / home ventilator",
    "rezum_bph":            "Rezum BPH water-vapor therapy",
    "greenlight_bph":       "GreenLight BPH laser therapy",
    "endoscopic_suturing":  "Endoscopic suturing",
    "single_use_bronchoscope":"Single-use bronchoscope",
}


def device_bucket_label(b: str | None) -> str:
    return DEVICE_BUCKETS.get(b or "", b or "")


TEMPLATES.env.globals["device_bucket_label"] = device_bucket_label


app = FastAPI(title="HCPCS Explorer")


def _ch():
    """Fresh ClickHouse connection per request (cheap, avoids stale TCP)."""
    return db._raw_client()


def q(sql: str, parameters: dict | None = None):
    """Run SQL, return (columns, rows)."""
    res = _ch().query(sql, parameters=parameters or {})
    return res.column_names, res.result_rows


def q_rows(sql: str, parameters: dict | None = None):
    return q(sql, parameters)[1]


# ============================================================
# /  — landing: search + top codes
# ============================================================

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    summary = q_rows("""
        SELECT
            (SELECT count() FROM hcpcs_master FINAL)                AS hcpcs_codes,
            (SELECT count() FROM cms_provider_summary
              WHERE hcpcs_code != '')                               AS billing_rows,
            (SELECT count() FROM opps_addendum_b)                   AS opps_rows,
            (SELECT count() FROM stark_dhs_codes)                   AS stark_rows,
            (SELECT count() FROM clinical_trial_interventions FINAL
              WHERE hcpcs_code IS NOT NULL)                         AS trial_links,
            (SELECT count() FROM cms_open_payments)                 AS op_rows
    """)[0]

    top_codes = q_rows("""
        WITH
          claims AS (
            SELECT hcpcs_code, count() AS billing_rows
            FROM cms_provider_summary
            WHERE hcpcs_code != '' GROUP BY hcpcs_code
          ),
          trials AS (
            SELECT hcpcs_code, countDistinct(nct_id) AS n_trials
            FROM (SELECT * FROM clinical_trial_interventions FINAL)
            WHERE hcpcs_code IS NOT NULL
            GROUP BY hcpcs_code
          ),
          opps AS (
            SELECT hcpcs_code, max(payment_rate) AS pay_rate
            FROM opps_addendum_b GROUP BY hcpcs_code
          )
        SELECT m.hcpcs_code, m.short_desc, m.code_family,
               ifNull(claims.billing_rows, 0)   AS billing_rows,
               ifNull(trials.n_trials, 0)       AS n_trials,
               ifNull(opps.pay_rate, 0)         AS pay_rate
        FROM (SELECT * FROM hcpcs_master FINAL) AS m
        LEFT JOIN claims USING hcpcs_code
        LEFT JOIN trials USING hcpcs_code
        LEFT JOIN opps   USING hcpcs_code
        WHERE m.is_device = 1
        ORDER BY n_trials DESC, billing_rows DESC
        LIMIT 25
    """)

    return TEMPLATES.TemplateResponse("hcpcs_home.html", {
        "request": request,
        "summary": summary,
        "top_codes": top_codes,
    })


# ============================================================
# /search?q=… — search helper
# ============================================================

@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    term = (q or "").strip()
    if not term:
        return RedirectResponse(url="/")

    if len(term) == 5 and term[0].isalpha() and term[1:].isdigit():
        return RedirectResponse(url=f"/hcpcs/{term.upper()}")
    if len(term) == 5 and term.isdigit():
        return RedirectResponse(url=f"/hcpcs/{term}")

    upper = term.upper()
    rows = q_rows("""
        SELECT hcpcs_code, short_desc, code_family, is_device
        FROM hcpcs_master FINAL
        WHERE hcpcs_code LIKE {prefix:String}
           OR upperUTF8(short_desc) LIKE {fuzzy:String}
        ORDER BY (hcpcs_code LIKE {prefix:String}) DESC, hcpcs_code
        LIMIT 100
    """, {"prefix": f"{upper}%", "fuzzy": f"%{upper}%"})

    return TEMPLATES.TemplateResponse("hcpcs_search.html", {
        "request": request, "term": term, "rows": rows,
    })


# ============================================================
# /hcpcs/{code}  — full cross-source detail
# ============================================================

@app.get("/hcpcs/{code}", response_class=HTMLResponse)
def hcpcs_detail(request: Request, code: str):
    code = code.upper().strip()

    master_rows = q_rows("""
        SELECT hcpcs_code, short_desc, long_desc, betos_code,
               pricing_indicator, coverage_code, asc_payment_grp,
               type_of_service, code_family, is_device, effective_qtr
        FROM hcpcs_master FINAL
        WHERE hcpcs_code = {c:String}
    """, {"c": code})
    master = master_rows[0] if master_rows else None

    # FALLBACK: CPT codes (5-digit numeric, e.g. 33340 LAA closure) aren't
    # in the public CMS HCPCS file because they're AMA-licensed. But we DO
    # have their Medicare description in opps_addendum_b. Synthesize a
    # master tuple so the rest of the page can render.
    if master is None:
        opps_desc = q_rows("""
            SELECT any(short_descriptor) FROM opps_addendum_b
            WHERE hcpcs_code = {c:String}
        """, {"c": code})
        synthetic_desc = opps_desc[0][0] if opps_desc and opps_desc[0][0] else None
        if synthetic_desc:
            family = code[0] if code and code[0].isalpha() else (
                "CPT" if code.isdigit() else None
            )
            is_device = 0
            # Tuple shape mirrors the SELECT above so the template works:
            # (code, short_desc, long_desc, betos, pricing, coverage,
            #  asc_grp, tos, family, is_device, effective_qtr)
            master = (code, synthetic_desc, None, None, None, None,
                      None, None, family, is_device, "from OPPS Addendum B")

    stark = q_rows("""
        SELECT effective_year, dhs_category, short_description
        FROM stark_dhs_codes
        WHERE hcpcs_code = {c:String}
        ORDER BY effective_year DESC, dhs_category
    """, {"c": code})

    opps = q_rows("""
        SELECT effective_quarter, status_indicator, apc_code,
               relative_weight, payment_rate, national_copayment,
               pass_through_expiry_year
        FROM opps_addendum_b
        WHERE hcpcs_code = {c:String}
        ORDER BY effective_quarter DESC
    """, {"c": code})

    billing_summary = q_rows("""
        SELECT count() AS rows,
               countDistinct(npi) AS providers,
               sum(total_services) AS svcs,
               sum(total_beneficiaries) AS benes,
               sum(total_payment_amt) AS paid,
               groupUniqArray(dataset_id) AS sources
        FROM cms_provider_summary
        WHERE hcpcs_code = {c:String}
    """, {"c": code})[0]

    top_providers = q_rows("""
        SELECT provider_name, npi, provider_state, provider_city,
               sum(total_services) AS svcs,
               sum(total_payment_amt) AS paid,
               any(dataset_id) AS source
        FROM cms_provider_summary
        WHERE hcpcs_code = {c:String} AND provider_name != ''
        GROUP BY provider_name, npi, provider_state, provider_city
        ORDER BY svcs DESC NULLS LAST
        LIMIT 25
    """, {"c": code})

    top_states = q_rows("""
        SELECT provider_state,
               countDistinct(npi) AS providers,
               sum(total_services) AS svcs
        FROM cms_provider_summary
        WHERE hcpcs_code = {c:String} AND provider_state != ''
        GROUP BY provider_state
        ORDER BY svcs DESC NULLS LAST
        LIMIT 10
    """, {"c": code})

    # Open Payments — fuzzy product-name match (uses first word of description)
    op_rows = []
    if master and master[1]:
        desc_token = (master[1].split()[0] if master[1] else "").lower()
        if desc_token and len(desc_token) >= 4:
            op_rows = q_rows("""
                SELECT manufacturer_name, product_name,
                       count() AS payments,
                       round(sum(payment_total), 2) AS usd
                FROM cms_open_payments
                WHERE lowerUTF8(product_name) LIKE {tok:String}
                GROUP BY manufacturer_name, product_name
                ORDER BY usd DESC NULLS LAST
                LIMIT 15
            """, {"tok": f"%{desc_token}%"})

    trials = q_rows("""
        SELECT t.nct_id, t.brief_title, t.lead_sponsor,
               t.overall_status, t.device_category, t.start_date,
               i.intervention_name, i.intervention_type, i.hcpcs_confidence
        FROM (SELECT * FROM clinical_trial_interventions FINAL) AS i
        INNER JOIN (SELECT * FROM clinical_trials FINAL) AS t
            ON i.nct_id = t.nct_id
        WHERE i.hcpcs_code = {c:String}
        ORDER BY t.start_date DESC NULLS LAST
        LIMIT 30
    """, {"c": code})

    # DIRECT bridge: HCPCS → NPI (Physician PUF) → CCN (affiliations) → hospital.
    # Aggregates physician PUF rows by their affiliated hospitals — much more
    # precise than the APC bundle bridge.
    hospitals_direct = q_rows("""
        WITH
          npi_billed AS (
            SELECT npi,
                   sum(total_services) AS svcs,
                   sum(total_payment_amt) AS paid,
                   count() AS billing_rows
            FROM cms_provider_summary
            WHERE hcpcs_code = {c:String}
              AND npi != ''
              AND dataset_id IN ('medicare_physician_by_provider_service',
                                 'medicare_dmepos_by_supplier_service')
            GROUP BY npi
          ),
          npi_ccn AS (
            SELECT npi, facility_ccn
            FROM (SELECT * FROM npi_facility_affiliation FINAL)
          ),
          stars AS (
            SELECT facility_id, facility_name, state, hospital_type, overall_rating
            FROM cms_hospitals FINAL
          ),
          hf_readm AS (
            SELECT facility_id, score_num
            FROM cms_hospital_measures FINAL
            WHERE measure_id = 'READM-30-HF-HRRP'
          )
        SELECT npi_ccn.facility_ccn AS ccn,
               any(stars.facility_name) AS name,
               any(stars.state) AS state,
               any(stars.hospital_type) AS htype,
               any(stars.overall_rating) AS rating,
               countDistinct(npi_billed.npi) AS affiliated_billers,
               sum(npi_billed.svcs) AS total_svcs,
               sum(npi_billed.paid) AS total_paid,
               any(hf_readm.score_num) AS hf_readm
        FROM npi_billed
        INNER JOIN npi_ccn USING npi
        LEFT JOIN stars    ON npi_ccn.facility_ccn = stars.facility_id
        LEFT JOIN hf_readm ON npi_ccn.facility_ccn = hf_readm.facility_id
        GROUP BY ccn
        ORDER BY total_svcs DESC NULLS LAST
        LIMIT 25
    """, {"c": code})

    # APC bridge (looser — keeps APC-bundle context)
    apc_codes = [row[2] for row in opps if row[2]]
    hospitals_billing = []
    apc_label = None
    if apc_codes:
        apc_label = apc_codes[0]
        hospitals_billing = q_rows("""
            WITH hf_readm AS (
                SELECT facility_id, score_num
                FROM cms_hospital_measures FINAL
                WHERE measure_id = 'READM-30-HF-HRRP'
            ),
            stars AS (
                SELECT facility_id, facility_name, state, hospital_type, overall_rating
                FROM cms_hospitals FINAL
            )
            SELECT cps.ccn,
                   stars.facility_name,
                   stars.state,
                   stars.hospital_type,
                   stars.overall_rating,
                   toFloat64OrNull(JSONExtractString(cps.raw, 'CAPC_Srvcs')) AS svcs,
                   hf_readm.score_num AS hf_readm_rate
            FROM cms_provider_summary AS cps
            LEFT JOIN stars    ON cps.ccn = stars.facility_id
            LEFT JOIN hf_readm ON cps.ccn = hf_readm.facility_id
            WHERE cps.dataset_id = 'medicare_outpatient_by_provider_service'
              AND JSONExtractString(cps.raw, 'APC_Cd') = {apc:String}
            ORDER BY svcs DESC NULLS LAST
            LIMIT 25
        """, {"apc": apc_label})

    return TEMPLATES.TemplateResponse("hcpcs_detail_only.html", {
        "request": request,
        "code": code,
        "master": master,
        "stark": stark,
        "opps": opps,
        "billing_summary": billing_summary,
        "top_providers": top_providers,
        "top_states": top_states,
        "open_payments": op_rows,
        "trials": trials,
        "hospitals_billing": hospitals_billing,
        "apc_label": apc_label,
        "hospitals_direct": hospitals_direct,
    })


# ============================================================
# /hospitals  — hospital roster + readmission/quality
# ============================================================

@app.get("/hospitals", response_class=HTMLResponse)
def hospitals(request: Request,
              state: str = "",
              measure: str = "READM-30-HF-HRRP",
              limit: int = 100):
    """Hospital roster joined with a chosen readmission/quality measure.

    Default measure is 30-day readmission for heart failure (HRRP).
    Other useful options: READM-30-PN-HRRP, READM-30-CABG-HRRP,
    READM-30-AMI-HRRP, READM-30-COPD-HRRP, READM-30-HIP-KNEE-HRRP.
    """
    state_clause = ""
    params = {"measure": measure, "lim": int(limit)}
    if state:
        state_clause = "AND h.state = {state:String}"
        params["state"] = state.upper()

    measures_available = q_rows("""
        SELECT measure_id, any(measure_name), count() AS n_hospitals
        FROM cms_hospital_measures
        WHERE measure_id LIKE '%READM%'
           OR measure_id LIKE 'HAI%'
           OR measure_id LIKE 'PSI%'
           OR measure_id LIKE '%MORT%'
           OR measure_id IN ('Hybrid_HWR','MSPB_1','H_STAR_RATING')
        GROUP BY measure_id ORDER BY n_hospitals DESC LIMIT 30
    """)

    rows = q_rows(f"""
        SELECT h.facility_id, h.facility_name, h.state, h.city,
               h.hospital_type, h.hospital_ownership, h.overall_rating,
               m.score, m.score_num, m.compared_to_national,
               m.denominator
        FROM (SELECT * FROM cms_hospitals FINAL) AS h
        LEFT JOIN (
            SELECT facility_id, score, score_num, compared_to_national, denominator
            FROM cms_hospital_measures FINAL
            WHERE measure_id = {{measure:String}}
        ) AS m ON h.facility_id = m.facility_id
        WHERE 1=1 {state_clause}
        ORDER BY m.score_num DESC NULLS LAST, h.facility_name
        LIMIT {{lim:UInt32}}
    """, params)

    summary = q_rows("""
        SELECT count() AS hospitals,
               countDistinct(state) AS states,
               avg(toFloat64OrNull(overall_rating)) AS avg_rating
        FROM cms_hospitals FINAL
    """)[0]

    return TEMPLATES.TemplateResponse("hospitals.html", {
        "request": request,
        "rows": rows,
        "state": state,
        "measure": measure,
        "measures_available": measures_available,
        "summary": summary,
    })


# ============================================================
# /hospital/{ccn}  — single hospital quality profile
# ============================================================

@app.get("/hospital/{ccn}", response_class=HTMLResponse)
def hospital_detail(request: Request, ccn: str):
    info_rows = q_rows("""
        SELECT facility_id, facility_name, address, city, state, zip_code,
               county_name, telephone, hospital_type, hospital_ownership,
               emergency_services, overall_rating
        FROM cms_hospitals FINAL
        WHERE facility_id = {ccn:String}
    """, {"ccn": ccn})
    info = info_rows[0] if info_rows else None

    measures = q_rows("""
        SELECT dataset_id, measure_id, measure_name, score, score_num,
               compared_to_national, denominator,
               start_date, end_date, footnote
        FROM cms_hospital_measures FINAL
        WHERE facility_id = {ccn:String}
        ORDER BY dataset_id, measure_id
    """, {"ccn": ccn})
    readmissions = [m for m in measures if 'READM' in (m[1] or '')]

    # Top outpatient APCs this hospital billed, with the HCPCS codes that
    # roll up into each APC (via opps_addendum_b).
    top_apcs = q_rows("""
        SELECT JSONExtractString(raw, 'APC_Cd') AS apc,
               JSONExtractString(raw, 'APC_Desc') AS apc_desc,
               sum(toFloat64OrNull(JSONExtractString(raw, 'CAPC_Srvcs'))) AS svcs
        FROM cms_provider_summary
        WHERE dataset_id = 'medicare_outpatient_by_provider_service'
          AND ccn = {ccn:String}
        GROUP BY apc, apc_desc
        HAVING apc != ''
        ORDER BY svcs DESC NULLS LAST
        LIMIT 15
    """, {"ccn": ccn})

    apc_codes = [a[0] for a in top_apcs if a[0]]
    hcpcs_by_apc = {}
    if apc_codes:
        rows = q_rows("""
            SELECT b.apc_code, b.hcpcs_code, m.short_desc, b.status_indicator, b.payment_rate
            FROM opps_addendum_b AS b
            LEFT JOIN (SELECT hcpcs_code, short_desc FROM hcpcs_master FINAL) AS m
                USING hcpcs_code
            WHERE b.effective_quarter = '2026Q1'
              AND b.apc_code IN {apcs:Array(String)}
              AND b.status_indicator IN ('J1','J2','T','S','Q1','Q2','Q3','R','U','V')
            ORDER BY b.apc_code, b.payment_rate DESC
        """, {"apcs": apc_codes})
        for r in rows:
            hcpcs_by_apc.setdefault(r[0], []).append(r[1:])

    # Top inpatient DRGs this hospital billed
    top_drgs = q_rows("""
        SELECT drg_code, max(drg_description) AS drg_desc,
               sum(total_services) AS discharges,
               sum(total_payment_amt) AS paid
        FROM cms_provider_summary
        WHERE dataset_id = 'medicare_inpatient_by_provider_service'
          AND ccn = {ccn:String}
          AND drg_code != ''
        GROUP BY drg_code
        ORDER BY discharges DESC NULLS LAST
        LIMIT 15
    """, {"ccn": ccn})

    return TEMPLATES.TemplateResponse("hospital_detail.html", {
        "request": request,
        "ccn": ccn,
        "info": info,
        "measures": measures,
        "readmissions": readmissions,
        "top_apcs": top_apcs,
        "hcpcs_by_apc": hcpcs_by_apc,
        "top_drgs": top_drgs,
    })
