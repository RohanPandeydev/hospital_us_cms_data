"""Human-readable crosswalk for CMS measure IDs.

Maps cryptic CMS measure IDs (e.g., "PSI_09", "HAI_1_SIR", "READM-30-AMI-HRRP")
to plain-English names, descriptions, clinical categories, and risk thresholds.
"""

# Domains used for grouping
DOMAIN_READMISSION = "Readmission"
DOMAIN_MORTALITY   = "Mortality"
DOMAIN_COMPLICATION = "Complication"
DOMAIN_INFECTION   = "Infection"
DOMAIN_EXPERIENCE  = "Patient Experience"
DOMAIN_SAFETY      = "Safety"
DOMAIN_EFFICIENCY  = "Efficiency"
DOMAIN_DEVICE      = "Device-Specific"


# Dataset-level decoder
DATASETS = {
    "xubh-q36u": ("Hospital General Information",       "Master list of all 5,426 US hospitals with CMS identifier, address, type, ownership, and overall 5-star rating."),
    "ynj2-r877": ("Complications & Deaths",             "Patient Safety Indicators (PSI), 30-day mortality rates, and complication rates for major procedures."),
    "632h-zaca": ("Unplanned Hospital Visits",          "30-day readmission rates and Excess Days in Acute Care (EDAC) after inpatient stays — leading indicator of device failures."),
    "9n3s-kdb3": ("HRRP Readmissions",                  "Excess Readmission Ratio (ERR) under Medicare's Hospital Readmissions Reduction Program — hospitals with ERR > 1 are penalized."),
    "77hc-ibv8": ("Hospital-Acquired Infections (HAI)", "Standardized Infection Ratios (SIR) for device-caused infections: central line (CLABSI), catheter (CAUTI), MRSA, C. diff, surgical site."),
    "muwa-iene": ("PSI-90 Composite",                   "Medicare composite safety score combining 10+ patient safety indicators into a single risk metric."),
    "dgck-syfz": ("HCAHPS Patient Survey",              "Patient experience survey — how patients rate communication, cleanliness, responsiveness, overall experience."),
    "bs2r-24vh": ("State Benchmarks",                   "State-level averages for complication & mortality measures, used for geographic comparison."),
    "qqw3-t4ie": ("National Benchmarks",                "National averages used as the baseline for every hospital-level measure."),
    "tqkv-mgxq": ("Joint Replacement (CJR) Model",      "Hip/knee arthroplasty composite outcomes — most direct device-specific measure we have."),
    "4jcv-atw7": ("ASC Quality Measures",               "Ambulatory Surgical Center quality + NPI identifiers (bridge to FDA device reports)."),
    "48nr-hqxx": ("ASC Patient Survey",                 "Patient experience at surgery centers."),
    "y9us-9xdf": ("Footnote Crosswalk",                 "Lookup table that explains what each footnote code means (e.g. '1' = 'too few cases to report')."),
}


# Each entry: (plain_name, domain, description, good_direction, threshold)
MEASURES = {
    # --- PSI (Patient Safety Indicators, AHRQ-defined) ---
    "PSI_03":  ("Pressure Ulcer Rate",              DOMAIN_SAFETY,       "Rate of hospital-acquired pressure ulcers per 1,000 discharges.", "lower", None),
    "PSI_04":  ("Death Among Surgical Inpatients",  DOMAIN_MORTALITY,    "Deaths among patients with serious but treatable complications.", "lower", None),
    "PSI_06":  ("Iatrogenic Pneumothorax",          DOMAIN_SAFETY,       "Accidental punctured lung during procedure.", "lower", None),
    "PSI_08":  ("In-Hospital Hip Fracture",         DOMAIN_SAFETY,       "Post-operative hip fracture rate — often device-related.", "lower", None),
    "PSI_09":  ("Perioperative Hemorrhage",         DOMAIN_SAFETY,       "Post-operative bleeding or hematoma — key post-device-implant complication.", "lower", None),
    "PSI_10":  ("Postoperative Kidney Injury",      DOMAIN_SAFETY,       "Post-operative acute kidney injury requiring dialysis.", "lower", None),
    "PSI_11":  ("Postoperative Respiratory Failure", DOMAIN_SAFETY,      "Ventilator complications post-surgery — especially relevant to cardiac device implants.", "lower", None),
    "PSI_12":  ("Postoperative DVT/PE",             DOMAIN_SAFETY,       "Deep vein thrombosis or pulmonary embolism post-surgery (stent thrombosis signal).", "lower", None),
    "PSI_13":  ("Postoperative Sepsis",             DOMAIN_INFECTION,    "Device-site sepsis — correlates with CLABSI/CAUTI infection signals.", "lower", None),
    "PSI_14":  ("Postoperative Wound Dehiscence",   DOMAIN_COMPLICATION, "Surgical wound re-opening.", "lower", None),
    "PSI_15":  ("Accidental Puncture/Laceration",   DOMAIN_SAFETY,       "Procedural injury during device placement.", "lower", None),
    "PSI_90":  ("PSI-90 Composite Safety Score",    DOMAIN_SAFETY,       "Weighted composite of 10 patient safety indicators — CMS's master safety risk metric.", "lower", None),

    # --- Mortality (30-day) ---
    "MORT_30_AMI":      ("30-Day Mortality after Heart Attack",        DOMAIN_MORTALITY, "Deaths within 30 days of heart attack admission. Stent / cardiac catheter outcome.", "lower", None),
    "MORT_30_CABG":     ("30-Day Mortality after Bypass Surgery",      DOMAIN_MORTALITY, "Deaths within 30 days of coronary artery bypass graft surgery.", "lower", None),
    "MORT_30_COPD":     ("30-Day Mortality after COPD",                DOMAIN_MORTALITY, "Deaths within 30 days of COPD admission.", "lower", None),
    "MORT_30_HF":       ("30-Day Mortality after Heart Failure",       DOMAIN_MORTALITY, "Deaths within 30 days of heart failure admission. Cardiac monitoring device outcome.", "lower", None),
    "MORT_30_PN":       ("30-Day Mortality after Pneumonia",           DOMAIN_MORTALITY, "Deaths within 30 days of pneumonia admission.", "lower", None),
    "MORT_30_STK":      ("30-Day Mortality after Stroke",              DOMAIN_MORTALITY, "Deaths within 30 days of stroke admission.", "lower", None),
    "Hybrid_HWM":       ("Hospital-Wide Hybrid Mortality",             DOMAIN_MORTALITY, "All-cause 30-day mortality across conditions, risk-adjusted for patient mix.", "lower", None),

    # --- Readmissions (30-day) ---
    "READM_30_AMI":      ("30-Day Readmission after Heart Attack",     DOMAIN_READMISSION, "Return to hospital within 30 days of heart attack. Cardiac device risk signal.", "lower", None),
    "READM_30_CABG":     ("30-Day Readmission after Bypass",           DOMAIN_READMISSION, "Return to hospital within 30 days of coronary bypass.", "lower", None),
    "READM_30_COPD":     ("30-Day Readmission after COPD",             DOMAIN_READMISSION, "Return within 30 days of COPD admission.", "lower", None),
    "READM_30_HF":       ("30-Day Readmission after Heart Failure",    DOMAIN_READMISSION, "Return within 30 days of heart failure admission.", "lower", None),
    "READM_30_HIP_KNEE": ("30-Day Readmission after Hip/Knee Replacement", DOMAIN_READMISSION, "Return within 30 days of hip/knee implant — primary orthopedic device outcome.", "lower", None),
    "READM_30_HOSP_WIDE":("Hospital-Wide Readmission",                 DOMAIN_READMISSION, "All-cause 30-day readmission rate across conditions.", "lower", None),
    "READM_30_PN":       ("30-Day Readmission after Pneumonia",        DOMAIN_READMISSION, "Return within 30 days of pneumonia admission.", "lower", None),
    "READM_30_STK":      ("30-Day Readmission after Stroke",           DOMAIN_READMISSION, "Return within 30 days of stroke admission.", "lower", None),

    # --- HRRP (Hospital Readmissions Reduction Program — ERR values) ---
    "READM-30-AMI-HRRP":      ("HRRP: Heart Attack Readmission Ratio",   DOMAIN_READMISSION, "Excess Readmission Ratio vs. expected; > 1.0 = penalty. Cardiac device.", "lower", 1.0),
    "READM-30-CABG-HRRP":     ("HRRP: Bypass Readmission Ratio",         DOMAIN_READMISSION, "CABG surgery readmission ratio. > 1.0 triggers Medicare payment reduction.", "lower", 1.0),
    "READM-30-COPD-HRRP":     ("HRRP: COPD Readmission Ratio",           DOMAIN_READMISSION, "COPD readmission ratio — respiratory device risk.", "lower", 1.0),
    "READM-30-HF-HRRP":       ("HRRP: Heart Failure Readmission Ratio",  DOMAIN_READMISSION, "Heart failure readmission ratio — cardiac monitoring device outcome.", "lower", 1.0),
    "READM-30-HIP-KNEE-HRRP": ("HRRP: Hip/Knee Readmission Ratio",       DOMAIN_READMISSION, "Hip/knee readmission ratio. > 1.0 = penalized. Most direct orthopedic device signal.", "lower", 1.0),
    "READM-30-PN-HRRP":       ("HRRP: Pneumonia Readmission Ratio",      DOMAIN_READMISSION, "Pneumonia readmission ratio — ventilator-related.", "lower", 1.0),

    # --- EDAC (Excess Days in Acute Care — leading indicator) ---
    "EDAC_30_AMI":  ("Extra Hospital Days after Heart Attack",  DOMAIN_READMISSION, "Excess days in acute care within 30 days — captures ER visits and obs stays. Early warning.", "lower", 0),
    "EDAC_30_HF":   ("Extra Hospital Days after Heart Failure", DOMAIN_READMISSION, "Excess acute care days — leading indicator of full readmission.", "lower", 0),
    "EDAC_30_PN":   ("Extra Hospital Days after Pneumonia",     DOMAIN_READMISSION, "Excess acute care days — leading indicator of full readmission.", "lower", 0),

    # --- Complications ---
    "COMP_HIP_KNEE": ("Hip/Knee Complication Rate", DOMAIN_COMPLICATION, "Composite complication rate after hip/knee replacement — directly tied to implant success.", "lower", None),

    # --- HAI (CDC Standardized Infection Ratios) ---
    "HAI_1":        ("Central Line Infections (CLABSI)", DOMAIN_INFECTION, "Blood infections from central venous catheters. Direct device-caused.", "lower", 1.0),
    "HAI_1_SIR":    ("Central Line Infections (CLABSI)", DOMAIN_INFECTION, "SIR > 1 = more infections than expected. Direct device-caused.", "lower", 1.0),
    "HAI_1_CILOWER":("CLABSI — Lower CI Bound",          DOMAIN_INFECTION, "95% confidence interval lower bound for CLABSI SIR.", "lower", 1.0),
    "HAI_1_CIUPPER":("CLABSI — Upper CI Bound",          DOMAIN_INFECTION, "95% confidence interval upper bound for CLABSI SIR.", "lower", 1.0),
    "HAI_2":        ("Urinary Catheter Infections (CAUTI)", DOMAIN_INFECTION, "Urinary tract infections from catheters. Direct device-caused.", "lower", 1.0),
    "HAI_2_SIR":    ("Urinary Catheter Infections (CAUTI)", DOMAIN_INFECTION, "SIR > 1 = more UTIs than expected. Direct device-caused.", "lower", 1.0),
    "HAI_3":        ("Colon Surgery Site Infection (SSI)", DOMAIN_INFECTION, "Post-operative infections after colon surgery — surgical device site.", "lower", 1.0),
    "HAI_3_SIR":    ("Colon Surgery Site Infection (SSI)", DOMAIN_INFECTION, "SIR > 1 = more colon SSIs than expected.", "lower", 1.0),
    "HAI_4":        ("Abdominal Hysterectomy SSI",       DOMAIN_INFECTION, "Post-operative surgical site infections.", "lower", 1.0),
    "HAI_4_SIR":    ("Abdominal Hysterectomy SSI",       DOMAIN_INFECTION, "SIR > 1 = more infections than expected.", "lower", 1.0),
    "HAI_5":        ("MRSA Blood Infections",            DOMAIN_INFECTION, "Drug-resistant staph bloodstream infections — device biofilm colonization signal.", "lower", 1.0),
    "HAI_5_SIR":    ("MRSA Blood Infections",            DOMAIN_INFECTION, "SIR > 1 = more MRSA bacteremia than expected.", "lower", 1.0),
    "HAI_6":        ("C. diff Infections",               DOMAIN_INFECTION, "Antibiotic-related gut infection — post-procedure antibiotic disruption.", "lower", 1.0),
    "HAI_6_SIR":    ("C. diff Infections",               DOMAIN_INFECTION, "SIR > 1 = more C. diff infections than expected.", "lower", 1.0),

    # --- HCAHPS (patient experience) ---
    "H_STAR_RATING": ("Overall Star Rating",             DOMAIN_EXPERIENCE, "Composite patient experience score on a 1-5 scale.", "higher", None),
    "H_COMP_1":      ("Nurse Communication",             DOMAIN_EXPERIENCE, "How well nurses communicated with patients.", "higher", None),
    "H_COMP_2":      ("Doctor Communication",            DOMAIN_EXPERIENCE, "How well doctors communicated.", "higher", None),
    "H_COMP_3":      ("Staff Responsiveness",            DOMAIN_EXPERIENCE, "How quickly staff responded to patient needs.", "higher", None),
    "H_COMP_5":      ("Medicine Communication",          DOMAIN_EXPERIENCE, "How well staff explained medications.", "higher", None),
    "H_COMP_6":      ("Discharge Information",           DOMAIN_EXPERIENCE, "Quality of discharge instructions.", "higher", None),
    "H_COMP_7":      ("Care Transition",                 DOMAIN_EXPERIENCE, "How well patients understood their care plan at discharge.", "higher", None),
    "H_CLEAN_HSP":   ("Hospital Cleanliness",            DOMAIN_EXPERIENCE, "Patient rating of room/bathroom cleanliness.", "higher", None),
    "H_QUIET_HSP":   ("Hospital Quietness",              DOMAIN_EXPERIENCE, "Patient rating of hospital quietness at night.", "higher", None),
    "H_RECMND":      ("Would Recommend",                 DOMAIN_EXPERIENCE, "Percentage of patients who would recommend this hospital.", "higher", None),
    "H_HSP_RATING":  ("Overall Hospital Rating",         DOMAIN_EXPERIENCE, "Overall patient rating on a 0-10 scale.", "higher", None),
}


def decode_measure(measure_id):
    """Return (plain_name, domain, description, good_direction, threshold) for a measure_id.
    Handles HCAHPS composite keys (e.g., 'H_COMP_1|Nurses "always" communicated well')
    by splitting on '|' and decoding the base code.
    """
    if not measure_id:
        return (None, None, None, None, None)
    base = measure_id.split("|")[0] if "|" in measure_id else measure_id
    qualifier = measure_id.split("|")[1] if "|" in measure_id else None

    # Try exact match
    entry = MEASURES.get(base)
    if not entry:
        # Try without _SIR / _CILOWER etc suffix
        for suf in ("_SIR", "_CILOWER", "_CIUPPER"):
            if base.endswith(suf):
                entry = MEASURES.get(base[: -len(suf)])
                if entry:
                    break
    if not entry:
        # Try prefix matching for H_COMP_* style
        for prefix in ("H_COMP_1", "H_COMP_2", "H_COMP_3", "H_COMP_5", "H_COMP_6", "H_COMP_7"):
            if base.startswith(prefix):
                entry = MEASURES.get(prefix)
                break
    if not entry:
        return (measure_id, None, None, None, None)

    name, domain, desc, direction, threshold = entry
    if qualifier:
        name = f"{name} — {qualifier}"
    return (name, domain, desc, direction, threshold)


def risk_level(score_num, direction, threshold):
    """Classify a numeric score as 'low', 'med', 'high', or None (unknown)."""
    if score_num is None or direction is None:
        return None
    try:
        score = float(score_num)
    except (TypeError, ValueError):
        return None
    if direction == "lower" and threshold is not None:
        if score > threshold * 1.25:
            return "high"
        if score > threshold:
            return "med"
        return "low"
    if direction == "higher" and threshold is not None:
        if score < threshold * 0.75:
            return "high"
        if score < threshold:
            return "med"
        return "low"
    return None


def decode_dataset(dataset_id):
    """Return (name, description) for a dataset_id."""
    return DATASETS.get(dataset_id, (dataset_id, None))
