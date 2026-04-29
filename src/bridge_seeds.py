"""Static seed data for manual crosswalks.

HCPCS ↔ FDA product_code: no authoritative public crosswalk exists, so this is
a hand-curated starter set covering the highest-volume device families.
"""

import logging

log = logging.getLogger(__name__)


# (hcpcs_code, product_code, device_category, confidence, source_notes)
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

    # =====================================================================
    # CPT codes — these appear in Medicare Part B Physician/Practitioner data
    # (HCPCS Level I). The C-/A-/L-/E-/V- codes above are mostly Hospital
    # Outpatient PPS (HCPCS Level II). Both flow through the same bridge.
    # =====================================================================

    # --- CRM: pacemakers & ICDs (CPT 33xxx) ---
    ("33206", "DTB", "pacemaker_atrial_only",   "high",   "Pacemaker insertion, atrial only"),
    ("33207", "DTB", "pacemaker_ventricular",   "high",   "Pacemaker insertion, ventricular only"),
    ("33208", "DTB", "pacemaker_dual_chamber",  "high",   "Pacemaker insertion, dual chamber"),
    ("33212", "DTB", "pacemaker_gen_repl",      "high",   "Pacemaker pulse generator replacement"),
    ("33213", "DTB", "pacemaker_gen_repl",      "high",   "Pacemaker generator replacement, dual"),
    ("33214", "LOM", "crt_p_replacement",       "high",   "Upgrade to bi-ventricular pacing system"),
    ("33216", "DXY", "pacemaker_lead_single",   "high",   "Pacemaker lead, single transvenous"),
    ("33217", "DXY", "pacemaker_lead_dual",     "high",   "Pacemaker lead, two transvenous"),
    ("33240", "LWS", "icd_gen_repl",            "high",   "ICD pulse generator replacement"),
    ("33249", "LWS", "icd_insertion",           "high",   "ICD insertion (doc CPT)"),
    ("33270", "LWS", "icd_subcutaneous",        "high",   "Subcutaneous ICD insertion"),
    ("33274", "QKJ", "leadless_pacemaker",      "high",   "Leadless pacemaker insertion (doc CPT)"),

    # --- Structural Heart: TAVR / TMVR (CPT 33xxx) ---
    ("33361", "OZA", "tavr_femoral",            "high",   "TAVR percutaneous femoral (doc CPT)"),
    ("33362", "OZA", "tavr_open_femoral",       "high",   "TAVR open femoral"),
    ("33363", "OZA", "tavr_open_axillary",      "high",   "TAVR open axillary"),
    ("33364", "OZA", "tavr_open_iliac",         "high",   "TAVR open iliac"),
    ("33365", "OZA", "tavr_transaortic",        "high",   "TAVR transaortic"),
    ("33366", "OZA", "tavr_transapical",        "high",   "TAVR transapical"),
    ("33418", "NPI", "tmvr_repair",             "high",   "Transcatheter mitral valve repair"),
    ("33419", "NPI", "tmvr_repair_addnl",       "medium", "Transcatheter mitral valve, additional"),
    ("33477", "OZA", "tpvr",                    "high",   "Transcatheter pulmonary valve"),

    # --- Coronary intervention / stents (CPT 92xxx) ---
    ("92920", "MAF", "pci_no_stent",            "medium", "PCI without stent"),
    ("92928", "NIQ", "pci_des",                 "high",   "PCI with drug-eluting stent"),
    ("92929", "NIQ", "pci_des_addnl",           "high",   "PCI with DES, additional vessel"),
    ("92933", "NIQ", "pci_des_atherectomy",     "high",   "PCI atherectomy + DES"),
    ("92937", "NIQ", "pci_bypass_graft",        "high",   "PCI bypass graft"),
    ("92941", "NIQ", "pci_acute_mi",            "high",   "PCI acute MI"),

    # --- Orthopedics: Hip (CPT 27xxx) ---
    ("27130", "KYF", "total_hip_arthroplasty",  "high",   "Total hip arthroplasty (doc CPT)"),
    ("27132", "KYF", "tha_conversion",          "high",   "THA conversion"),
    ("27134", "KYF", "tha_revision_both",       "high",   "THA revision, both components"),
    ("27137", "KYF", "tha_revision_acetab",     "high",   "THA acetabular revision"),
    ("27138", "KYF", "tha_revision_femoral",    "high",   "THA femoral revision"),

    # --- Orthopedics: Knee (CPT 274xx) ---
    ("27447", "KRO", "total_knee_arthroplasty", "high",   "Total knee arthroplasty"),
    ("27486", "KRO", "tka_revision_one",        "high",   "TKA revision, one component"),
    ("27487", "KRO", "tka_revision_both",       "high",   "TKA revision, both components"),

    # --- Spinal fusion (CPT 22xxx) ---
    ("22551", "KWQ", "cervical_fusion_anterior","high",   "Anterior cervical fusion"),
    ("22612", "KWP", "lumbar_fusion_post",      "high",   "Lumbar fusion, posterior"),
    ("22630", "KWP", "lumbar_interbody_post",   "high",   "Lumbar interbody fusion, posterior"),
    ("22633", "KWP", "lumbar_combined_fusion",  "high",   "Lumbar combined posterolateral + interbody"),

    # --- Endoscopy: duodenoscope/bronchoscope/colonoscope ---
    ("43260", "FCY", "ercp_duodenoscope",       "high",   "ERCP w/ duodenoscope (doc CPT)"),
    ("43261", "FCY", "ercp_biopsy",             "high",   "ERCP w/ biopsy"),
    ("43262", "FCY", "ercp_sphincterotomy",     "high",   "ERCP w/ sphincterotomy"),
    ("43263", "FCY", "ercp_sphincter_pressure", "high",   "ERCP w/ sphincter of Oddi"),
    ("31622", "EOQ", "bronchoscopy_diag",       "medium", "Bronchoscopy, diagnostic"),
    ("31628", "EOQ", "bronchoscopy_biopsy",     "medium", "Bronchoscopy w/ transbronchial biopsy"),
    ("45378", "FAJ", "colonoscopy_diag",        "medium", "Colonoscopy, diagnostic"),
    ("45385", "FAJ", "colonoscopy_polypectomy", "medium", "Colonoscopy w/ polypectomy"),

    # --- Ophthalmology: cataract / IOL (CPT 669xx) ---
    ("66982", "HQL", "cataract_complex",        "high",   "Complex cataract w/ IOL"),
    ("66984", "HQL", "cataract_routine",        "high",   "Routine cataract w/ IOL insertion"),
    ("66985", "HQL", "iol_subsequent",          "medium", "IOL insertion subsequent"),

    # --- Neurostim: SCS / DBS / VNS ---
    ("63650", "MHY", "scs_lead_perc",           "high",   "Spinal cord stim lead, percutaneous"),
    ("63655", "MHY", "scs_lead_laminect",       "high",   "Spinal cord stim lead, laminectomy"),
    ("63685", "MHY", "scs_pulse_gen",           "high",   "Spinal cord stim pulse generator"),
    ("63688", "MHY", "scs_revision_remove",     "medium", "Spinal cord stim revision/removal"),
    ("61885", "GZB", "dbs_pulse_gen_one",       "high",   "DBS pulse generator, one array"),
    ("61886", "GZB", "dbs_pulse_gen_two",       "high",   "DBS pulse generator, two arrays"),

    # --- Cochlear implant ---
    ("69930", "ESJ", "cochlear_implant",        "high",   "Cochlear device implantation"),

    # --- Septal / atrial closure ---
    ("93580", "NPI", "asd_closure",             "medium", "ASD closure transcatheter"),
    ("93581", "NPI", "vsd_closure",             "medium", "VSD closure transcatheter"),
]


COLUMNS = [
    "hcpcs_code", "product_code", "device_category",
    "match_method", "confidence", "source_notes",
]


def seed_hcpcs_to_product_code(conn):
    """Insert the static HCPCS↔product_code seed. Idempotent via ReplacingMergeTree."""
    rows = [
        (h, p, cat, "manual_seed", conf, note)
        for (h, p, cat, conf, note) in HCPCS_TO_PRODUCT_CODE_SEED
    ]
    conn.insert("bridge_hcpcs_to_product_code", rows, column_names=COLUMNS)
    log.info("seeded bridge_hcpcs_to_product_code: %d rows", len(rows))
    return len(rows)


def seed_all(conn):
    """Run every manual seed loader."""
    total = 0
    total += seed_hcpcs_to_product_code(conn)
    return total
