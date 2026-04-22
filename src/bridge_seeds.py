"""Static seed data for manual crosswalks.

HCPCS ↔ FDA product_code: no authoritative public crosswalk exists, so this is
a hand-curated starter set covering the highest-volume device families. Extend
over time via GUDID description matching and brand fuzzy-match (tracked as
match_method values other than 'manual_seed').

Confidence levels:
  high   — unambiguous 1:1 mapping published by FDA/CMS references
  medium — category-level mapping, multiple product_codes may be valid
  low    — inference from description similarity
"""

import logging
from psycopg2.extras import execute_values

log = logging.getLogger(__name__)


# (hcpcs_code, product_code, device_category, confidence, source_notes)
# Hand-curated from FDA CDRH Product Classification Database + CMS HCPCS file.
# Where multiple product_codes could apply to one HCPCS, we include each pair
# as a separate row (the bridge is many-to-many by design).
HCPCS_TO_PRODUCT_CODE_SEED = [
    # --- Cardiac rhythm: ICDs / pacemakers / leads ---
    ("C1721", "LWS", "icd_dual_chamber",        "high",   "AICD dual chamber → implantable defibrillator"),
    ("C1722", "LWS", "icd_single_chamber",      "high",   "AICD single chamber → implantable defibrillator"),
    ("C1882", "MRM", "crt_d",                   "high",   "AICD other than single/dual → CRT-D"),
    ("C1785", "DTB", "pacemaker_dual_chamber",  "high",   "Dual-chamber pacemaker pulse generator"),
    ("C1786", "DTB", "pacemaker_single_chamber","high",   "Single-chamber pacemaker pulse generator"),
    ("C1784", "LOM", "crt_p",                   "high",   "CRT-P (biventricular pacemaker)"),
    ("C1898", "DXY", "pacemaker_lead",          "high",   "Pacemaker lead, transvenous"),
    ("C1899", "DXY", "pacemaker_lead_combo",    "medium", "Lead pacemaker/AICD combination"),
    ("C1779", "NVY", "pacemaker_lead_vdd",      "high",   "Single-pass VDD pacemaker lead"),
    ("C1895", "NPL", "icd_lead_dual_coil",      "high",   "Endocardial dual coil ICD lead"),
    ("C1896", "LWP", "icd_lead_single_coil",    "medium", "Single coil ICD lead"),
    ("C1900", "DXY", "lead_coronary_venous",    "medium", "Coronary venous lead for CRT"),

    # --- Vascular: stents / filters / atherectomy / IVUS ---
    ("C1874", "NIQ", "drug_eluting_stent",      "high",   "Covered/coated stent w/ delivery → DES"),
    ("C1875", "NIQ", "drug_eluting_stent",      "high",   "Covered/coated stent w/o delivery → DES"),
    ("C1876", "MAF", "bare_metal_stent",        "high",   "Non-coated stent w/ delivery → BMS"),
    ("C1877", "MAF", "bare_metal_stent",        "high",   "Non-coated stent w/o delivery → BMS"),
    ("C1880", "DSY", "ivc_filter",              "high",   "Vena cava filter"),
    ("C1725", "DQY", "atherectomy_catheter",    "high",   "Transluminal atherectomy directional"),
    ("C1753", "FRN", "ivus_catheter",           "medium", "Intravascular ultrasound catheter"),
    ("C1887", "LWS", "ep_catheter_guiding",     "medium", "EP guiding catheter, intracardiac"),

    # --- Orthopedic / bone anchors ---
    ("C1713", "KZH", "bone_anchor",             "high",   "Bone anchor/screw → KZH anchor, bone"),
    ("C1741", "KZH", "bone_anchor_absorbable",  "high",   "Absorbable bone anchor → KZH"),
    ("L5856", "ISY", "microprocessor_knee",     "high",   "Microprocessor knee prosthesis"),
    ("L5857", "ISY", "microprocessor_knee",     "high",   "Microprocessor knee, addition"),
    ("L5859", "ISY", "microprocessor_knee_addn","medium", "Powered prosthetic knee"),

    # --- Infusion / vascular catheters ---
    ("C1751", "FRN", "central_venous_catheter", "high",   "PICC / CVC → long-term intravascular catheter"),
    ("C1752", "KOC", "hemodialysis_catheter",   "high",   "Hemodialysis catheter, long-term"),
    ("C1755", "DQX", "catheter_guidewire",      "medium", "Catheter guide wire"),

    # --- Urinary catheters / trays ---
    ("A4338", "FAQ", "urinary_catheter_foley",  "high",   "Indwelling Foley catheter"),
    ("A4314", "FAQ", "foley_tray_2way_latex",   "high",   "Insertion tray w/ Foley 2-way latex"),
    ("A4316", "FAQ", "foley_tray_2way_silicone","high",   "Insertion tray w/ Foley 2-way silicone"),
    ("A4344", "FAQ", "indwelling_silicone",     "medium", "Indwelling catheter, silicone"),

    # --- Respiratory ---
    ("E0601", "BZD", "cpap_device",             "high",   "Continuous positive airway pressure"),
    ("E0470", "NOU", "rad_no_backup",           "high",   "RAD without backup rate"),
    ("E0471", "NOU", "rad_with_backup",         "high",   "RAD with backup rate"),
    ("E0465", "NOU", "home_ventilator_invasive","medium", "Home mechanical ventilator, invasive"),
    ("E0466", "NOU", "home_ventilator_niv",     "medium", "Home mechanical ventilator, non-invasive"),

    # --- Diabetic / infusion pumps ---
    ("E0784", "LZG", "insulin_pump",            "high",   "External insulin infusion pump"),
    ("K0553", "QBJ", "cgm_supply",              "medium", "CGM receiver/transmitter supplies"),
    ("K0554", "QBJ", "cgm_receiver",            "medium", "CGM therapeutic receiver"),

    # --- Ophthalmologic ---
    ("V2632", "HQL", "iol_posterior_chamber",   "high",   "Posterior chamber intraocular lens"),
    ("V2630", "HQL", "iol_anterior_chamber",    "high",   "Anterior chamber intraocular lens"),

    # --- Feeding / GI ---
    ("B4088", "KNT", "gastrostomy_low_profile", "medium", "Low-profile G-tube"),

    # --- Ostomy / wound care ---
    ("A4411", "FRE", "ostomy_skin_barrier",     "medium", "Ostomy skin barrier, paste"),
    ("A6196", "FRO", "alginate_dressing",       "high",   "Alginate dressing, wound"),

    # --- Mobility ---
    ("K0001", "IOR", "manual_wheelchair",       "medium", "Standard manual wheelchair"),
    ("K0822", "IPL", "power_wheelchair_grp2",   "medium", "Group 2 standard power wheelchair"),

    # --- Spinal cord / neuro stim ---
    ("L8679", "MHY", "neurostim_implant",       "medium", "Implantable neurostim pulse generator"),
    ("L8680", "MHY", "neurostim_electrode",     "medium", "Implantable neurostim electrode"),
]


UPSERT_SQL = """
    INSERT INTO bridge_hcpcs_to_product_code
        (hcpcs_code, product_code, device_category, match_method, confidence, source_notes)
    VALUES %s
    ON CONFLICT (hcpcs_code, product_code) DO UPDATE SET
        device_category = EXCLUDED.device_category,
        match_method    = EXCLUDED.match_method,
        confidence      = EXCLUDED.confidence,
        source_notes    = EXCLUDED.source_notes;
"""


def seed_hcpcs_to_product_code(conn):
    """Upsert the static HCPCS↔product_code seed. Idempotent."""
    rows = [
        (h, p, cat, "manual_seed", conf, note)
        for (h, p, cat, conf, note) in HCPCS_TO_PRODUCT_CODE_SEED
    ]
    with conn.cursor() as cur:
        execute_values(cur, UPSERT_SQL, rows, page_size=200)
    conn.commit()
    log.info("seeded bridge_hcpcs_to_product_code: %d rows", len(rows))
    return len(rows)


def seed_all(conn):
    """Run every manual seed loader. Extend here as more bridges are added."""
    total = 0
    total += seed_hcpcs_to_product_code(conn)
    return total
