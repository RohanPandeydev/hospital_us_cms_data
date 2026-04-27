"""DRG → device_category seed map and loader.

CMS Medicare-Severity DRG (MS-DRG) codes bundle a diagnosis with a procedure.
Many DRGs are device-specific (e.g. DRG 469 = hip/knee replacement, every
patient discharged under this DRG received an orthopedic implant). This file
encodes the known DRG ↔ device-category relationships so we can turn
hospital-level DRG volume into hospital-level device exposure.

Sources used to build this map:
  - CMS Medicare IPPS final rule appendices (DRG descriptions)
  - AAOS and AATS clinical practice guidelines (which DRGs imply which implants)
  - FDA product-code classification panels
  - Public literature on DRG-implant bundling

Some DRGs are ambiguous (e.g. "OTHER CARDIAC VALVE PROCEDURES" could mean
surgical or transcatheter). We err on the side of mapping to the likely
primary device and use `weight` to express confidence (1.0 = certain,
0.5 = split between categories).
"""

# DRG → list of (device_category, weight, rationale)
# Weight < 1.0 means the DRG covers multiple device types and we split the
# discharge count proportionally.
DRG_DEVICE_MAP: dict[str, list[tuple[str, float, str]]] = {
    # --- Cardiac valves (surgical + transcatheter) ---
    "212": [("cardiac_valve", 1.0, "Concomitant aortic + mitral valve procedures")],
    "216": [("cardiac_valve", 1.0, "Cardiac valve + major cardiothoracic with cath, MCC")],
    "217": [("cardiac_valve", 1.0, "Cardiac valve + major cardiothoracic with cath, CC")],
    "218": [("cardiac_valve", 1.0, "Cardiac valve + major cardiothoracic with cath")],
    "219": [("cardiac_valve", 1.0, "Cardiac valve + major cardiothoracic without cath, MCC")],
    "220": [("cardiac_valve", 1.0, "Cardiac valve + major cardiothoracic without cath, CC")],
    "221": [("cardiac_valve", 1.0, "Cardiac valve + major cardiothoracic without cath")],
    "266": [("cardiac_valve", 1.0, "Endovascular (TAVR/TMVR) cardiac valve replacement, MCC")],
    "267": [("cardiac_valve", 1.0, "Endovascular (TAVR/TMVR) cardiac valve replacement")],
    "319": [("cardiac_valve", 1.0, "Other endovascular cardiac valve procedures, MCC")],
    "320": [("cardiac_valve", 1.0, "Other endovascular cardiac valve procedures")],

    # --- Coronary bypass (CABG) ---
    "231": [("cabg_conduit", 1.0, "Coronary bypass with PTCA, MCC")],
    "232": [("cabg_conduit", 1.0, "Coronary bypass with PTCA")],
    "233": [("cabg_conduit", 1.0, "Coronary bypass with cath, MCC")],
    "234": [("cabg_conduit", 1.0, "Coronary bypass with cath")],
    "235": [("cabg_conduit", 1.0, "Coronary bypass without cath, MCC")],
    "236": [("cabg_conduit", 1.0, "Coronary bypass without cath")],

    # --- Percutaneous coronary intervention (stents) ---
    "246": [("drug_eluting_stent", 0.7, "PCI with drug-eluting stent, MCC — DES dominant"),
            ("bare_metal_stent",   0.3, "PCI with drug-eluting stent, MCC — some BMS")],
    "247": [("drug_eluting_stent", 0.7, "PCI with drug-eluting stent"),
            ("bare_metal_stent",   0.3, "PCI with drug-eluting stent — some BMS")],
    "248": [("bare_metal_stent",   0.9, "PCI with non-drug-eluting stent, MCC"),
            ("drug_eluting_stent", 0.1, "may include mixed DES/BMS")],
    "249": [("bare_metal_stent",   0.9, "PCI with non-drug-eluting stent"),
            ("drug_eluting_stent", 0.1, "may include mixed DES/BMS")],
    "250": [("drug_eluting_stent", 0.5, "PCI without coronary artery stent, MCC — may still involve device"),
            ("bare_metal_stent",   0.2, "mixed"),
            ("atherectomy_catheter", 0.3, "atherectomy often coded here")],
    "251": [("drug_eluting_stent", 0.5, "PCI without coronary artery stent"),
            ("bare_metal_stent",   0.2, "mixed"),
            ("atherectomy_catheter", 0.3, "atherectomy often coded here")],
    "273": [("atherectomy_catheter", 0.5, "Percutaneous intracardiac procedures, MCC"),
            ("catheter_guidewire",   0.5, "uses catheter/guidewire")],
    "274": [("atherectomy_catheter", 0.5, "Percutaneous intracardiac procedures"),
            ("catheter_guidewire",   0.5, "uses catheter/guidewire")],
    "323": [("drug_eluting_stent", 0.5, "Coronary IVL with intraluminal device, MCC"),
            ("ivus_catheter",      0.5, "IVL uses IVUS-adjacent tech")],
    "324": [("drug_eluting_stent", 0.5, "Coronary IVL with intraluminal device"),
            ("ivus_catheter",      0.5, "IVL uses IVUS-adjacent tech")],

    # --- Carotid artery stents ---
    "034": [("bare_metal_stent", 1.0, "Carotid artery stent procedures, MCC")],
    "035": [("bare_metal_stent", 1.0, "Carotid artery stent procedures, CC")],
    "036": [("bare_metal_stent", 1.0, "Carotid artery stent procedures")],

    # --- Pacemakers (permanent implant) ---
    "242": [("pacemaker_dual_chamber",   0.6, "Permanent pacemaker implant, MCC — dual chamber dominant"),
            ("pacemaker_single_chamber", 0.3, "single chamber"),
            ("pacemaker_lead",           0.1, "lead replacement only")],
    "243": [("pacemaker_dual_chamber",   0.6, "Permanent pacemaker implant, CC"),
            ("pacemaker_single_chamber", 0.3, "single chamber"),
            ("pacemaker_lead",           0.1, "lead replacement only")],
    "244": [("pacemaker_dual_chamber",   0.6, "Permanent pacemaker implant"),
            ("pacemaker_single_chamber", 0.3, "single chamber"),
            ("pacemaker_lead",           0.1, "lead replacement only")],
    "258": [("pacemaker_dual_chamber",   0.7, "Cardiac pacemaker device replacement, MCC"),
            ("pacemaker_single_chamber", 0.3, "single chamber")],
    "259": [("pacemaker_dual_chamber",   0.7, "Cardiac pacemaker device replacement"),
            ("pacemaker_single_chamber", 0.3, "single chamber")],
    "260": [("pacemaker_lead", 1.0, "Cardiac pacemaker revision except device replacement, MCC")],
    "261": [("pacemaker_lead", 1.0, "Cardiac pacemaker revision except device replacement, CC")],
    "262": [("pacemaker_lead", 1.0, "Cardiac pacemaker revision except device replacement")],

    # --- ICD / defibrillators ---
    "222": [("icd_dual_chamber", 0.6, "Cardiac defib with MCC"),
            ("icd_single_chamber", 0.4, "single chamber")],
    "223": [("icd_dual_chamber", 0.6, "Cardiac defib"),
            ("icd_single_chamber", 0.4, "single chamber")],
    "224": [("icd_dual_chamber", 0.6, "Cardiac defib with cath, MCC"),
            ("icd_single_chamber", 0.4, "single chamber")],
    "225": [("icd_dual_chamber", 0.6, "Cardiac defib with cath"),
            ("icd_single_chamber", 0.4, "single chamber")],
    "226": [("icd_dual_chamber", 0.6, "Cardiac defib without cath, MCC"),
            ("icd_single_chamber", 0.4, "single chamber")],
    "227": [("icd_dual_chamber", 0.6, "Cardiac defib without cath"),
            ("icd_single_chamber", 0.4, "single chamber")],
    "275": [("icd_dual_chamber", 0.6, "Cardiac defib implant with cath, MCC"),
            ("icd_single_chamber", 0.4, "single chamber")],
    "276": [("icd_dual_chamber", 0.6, "Cardiac defib implant, MCC or carotid sinus neurostim"),
            ("icd_single_chamber", 0.3, "single chamber"),
            ("neurostim_implant", 0.1, "carotid sinus neurostim subset")],
    "277": [("icd_dual_chamber", 0.6, "Cardiac defib implant"),
            ("icd_single_chamber", 0.4, "single chamber")],

    # --- Heart assist devices ---
    "001": [("heart_assist_device", 1.0, "Heart transplant or assist system implant, MCC")],
    "002": [("heart_assist_device", 1.0, "Heart transplant or assist system implant")],
    "215": [("heart_assist_device", 1.0, "Other heart assist system implant")],

    # --- Joint replacement (hip, knee) ---
    "462": [("hip_knee_implant", 1.0, "Bilateral or multiple major joint procedures of lower extremity")],
    "466": [("hip_knee_implant", 1.0, "Revision of hip or knee replacement, MCC")],
    "467": [("hip_knee_implant", 1.0, "Revision of hip or knee replacement, CC")],
    "468": [("hip_knee_implant", 1.0, "Revision of hip or knee replacement")],
    "469": [("hip_knee_implant", 1.0, "Major hip and knee joint replacement, MCC")],
    "470": [("hip_knee_implant", 1.0, "Major hip and knee joint replacement")],
    "521": [("hip_knee_implant", 1.0, "Hip replacement with fracture, MCC")],
    "522": [("hip_knee_implant", 1.0, "Hip replacement with fracture")],

    # --- Spinal devices ---
    "028": [("spinal_fusion_device", 1.0, "Spinal procedures, MCC")],
    "029": [("spinal_fusion_device", 0.7, "Spinal procedures, CC — also includes spinal neurostim"),
            ("neurostim_implant",    0.3, "spinal cord stimulators")],
    "030": [("spinal_fusion_device", 1.0, "Spinal procedures")],
    "402": [("spinal_fusion_device", 1.0, "Single-level combined anterior/posterior spinal fusion")],
    "426": [("spinal_fusion_device", 1.0, "Multi-level combined ant/post spinal fusion, MCC")],
    "427": [("spinal_fusion_device", 1.0, "Multi-level combined ant/post spinal fusion, CC")],
    "428": [("spinal_fusion_device", 1.0, "Multi-level combined ant/post spinal fusion")],
    "430": [("spinal_fusion_device", 1.0, "Combined anterior/posterior cervical spinal fusion")],
    "448": [("spinal_fusion_device", 1.0, "Multiple level spinal fusion")],
    "451": [("spinal_fusion_device", 1.0, "Single-level spinal fusion")],
    "453": [("spinal_fusion_device", 1.0, "Combined anterior/posterior spinal fusion, MCC")],
    "454": [("spinal_fusion_device", 1.0, "Combined anterior/posterior spinal fusion, CC")],
    "455": [("spinal_fusion_device", 1.0, "Combined anterior/posterior spinal fusion")],
    "456": [("spinal_fusion_device", 1.0, "Spinal fusion with curvature/malignancy/infection")],
    "457": [("spinal_fusion_device", 1.0, "Spinal fusion with curvature/malignancy/infection, CC")],
    "458": [("spinal_fusion_device", 1.0, "Spinal fusion with curvature/malignancy/infection")],

    # --- Dialysis access ---
    # Hemodialysis catheter insertion typically appears in DRG 682-684 (renal failure)
    # but those are diagnostic-heavy not procedure-specific. Leaving out for now to
    # avoid noise.

    # --- Craniotomy with device implant (neurostim, shunts) ---
    "023": [("neurostim_implant", 1.0, "Craniotomy with major device implant or acute complex CNS, MCC")],
    "024": [("neurostim_implant", 1.0, "Craniotomy with major device implant")],
    "025": [("neurostim_implant", 0.5, "Craniotomy/endovasc intracranial proc, MCC"),
            ("catheter_guidewire", 0.5, "endovasc uses catheter/guidewire")],
    "026": [("neurostim_implant", 0.5, "Craniotomy/endovasc intracranial proc, CC"),
            ("catheter_guidewire", 0.5, "endovasc")],
    "027": [("neurostim_implant", 0.5, "Craniotomy/endovasc intracranial proc"),
            ("catheter_guidewire", 0.5, "endovasc")],

    # --- Respiratory / ventilator / CPAP bundled cases ---
    # DRGs 189, 207, 208 involve MV > 96 hrs — implies ventilator exposure
    "207": [("home_ventilator_invasive", 1.0, "Respiratory system diagnosis with mechanical ventilation >96 hrs")],
    "208": [("home_ventilator_niv", 1.0, "Respiratory system diagnosis with mechanical ventilation <96 hrs")],
    "003": [("home_ventilator_invasive", 1.0, "ECMO or tracheostomy with MV >96 hrs")],
}


def seed_drg_device_map(conn) -> int:
    """Populate drg_to_device_category from the static map above."""
    rows = []
    for drg, entries in DRG_DEVICE_MAP.items():
        for cat, weight, rationale in entries:
            rows.append((drg, cat, weight, rationale))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM drg_to_device_category")
        cur.executemany(
            """INSERT INTO drg_to_device_category
                   (drg_code, device_category, weight, rationale)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (drg_code, device_category) DO UPDATE
                   SET weight = EXCLUDED.weight,
                       rationale = EXCLUDED.rationale""",
            rows,
        )
    return len(rows)
