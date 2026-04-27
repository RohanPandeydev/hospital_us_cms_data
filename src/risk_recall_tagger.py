"""Recall → device_category tagger.

openFDA's enforcement endpoint leaves openfda.product_code empty for device
recalls, so we can't join directly. This module infers device_category via
keyword match on `product_description` (and `firm_name` / `reason_for_recall`
as backups) against a keyword map derived from the bridge's device_category
vocabulary. The mapping is coarse by design — when nothing matches we leave
device_category NULL, which the assembler treats as "no recall" for that cell.
"""

import logging
import re
from typing import Optional

from . import risk_db, risk_llm

log = logging.getLogger(__name__)

# Ordered keyword rules — first match wins. Specific patterns go before generic.
# Each rule: (regex, device_category)
# Using word boundaries so "stent" doesn't catch "consistent".
_RULES = [
    (r"\bdrug[- ]eluting stent\b",                "drug_eluting_stent"),
    (r"\bbare[- ]metal stent\b",                  "bare_metal_stent"),
    (r"\bstent\b",                                "bare_metal_stent"),
    (r"\batherectomy\b",                          "atherectomy_catheter"),
    (r"\bguide ?wire\b",                          "catheter_guidewire"),
    (r"\bintravascular ultrasound|ivus\b",        "ivus_catheter"),
    (r"\bivc filter\b",                           "ivc_filter"),
    (r"\bcentral venous catheter|cvc\b",          "central_venous_catheter"),
    (r"\bhemodialysis catheter|dialysis catheter\b", "hemodialysis_catheter"),
    (r"\belectrophysiolog(y|ical) catheter|ep catheter\b", "ep_catheter_guiding"),
    (r"\bfoley\b.*\b(two[- ]way|2[- ]?way)\b.*\bsilicone\b",  "foley_tray_2way_silicone"),
    (r"\bfoley\b.*\b(two[- ]way|2[- ]?way)\b",    "foley_tray_2way_latex"),
    (r"\bfoley\b",                                "urinary_catheter_foley"),
    (r"\burinary catheter\b",                     "urinary_catheter_foley"),
    (r"\bindwelling\b.*\bsilicone\b",             "indwelling_silicone"),
    (r"\bintraocular lens|iol\b.*\banterior\b",   "iol_anterior_chamber"),
    (r"\bintraocular lens|iol\b.*\bposterior\b",  "iol_posterior_chamber"),
    (r"\bintraocular lens|iol\b",                 "iol_posterior_chamber"),
    (r"\binsulin pump\b",                         "insulin_pump"),
    (r"\bcontinuous glucose monitor|cgm receiver\b", "cgm_receiver"),
    (r"\bcgm\b",                                  "cgm_supply"),
    (r"\bcpap\b",                                 "cpap_device"),
    (r"\brespiratory assist device|bipap|bi[- ]pap\b.*\bbackup\b", "rad_with_backup"),
    (r"\brespiratory assist device|bipap|bi[- ]pap\b", "rad_no_backup"),
    (r"\b(home )?ventilator\b.*\b(invasive|tracheo)\b", "home_ventilator_invasive"),
    (r"\b(home )?ventilator\b",                   "home_ventilator_niv"),
    (r"\bimplantable cardioverter defibrillator|icd\b.*\bdual\b", "icd_dual_chamber"),
    (r"\bimplantable cardioverter defibrillator|icd\b.*\bsingle\b", "icd_single_chamber"),
    (r"\bimplantable cardioverter defibrillator|icd\b", "icd_dual_chamber"),
    (r"\bcrt[- ]?d\b|cardiac resynchronization.*defib", "crt_d"),
    (r"\bcrt[- ]?p\b|cardiac resynchronization.*pace",  "crt_p"),
    (r"\bpacemaker\b.*\bdual\b",                  "pacemaker_dual_chamber"),
    (r"\bpacemaker\b.*\bsingle\b",                "pacemaker_single_chamber"),
    (r"\bpacemaker\b",                            "pacemaker_dual_chamber"),
    (r"\bpacing lead\b|\bpacemaker lead\b",       "pacemaker_lead"),
    (r"\bcoronary venous lead\b|cs lead\b",       "lead_coronary_venous"),
    (r"\bicd lead\b.*\b(dual coil)\b",            "icd_lead_dual_coil"),
    (r"\bicd lead\b",                             "icd_lead_single_coil"),
    (r"\bneurostimulat(or|ion)\b.*\belectrode\b", "neurostim_electrode"),
    (r"\bneurostimulat(or|ion)\b|\bspinal cord stim\b|\bdeep brain stim\b", "neurostim_implant"),
    (r"\bbone anchor\b.*\babsorbable\b",          "bone_anchor_absorbable"),
    (r"\bbone anchor\b|\bsuture anchor\b",        "bone_anchor"),
    (r"\bmicroprocessor knee\b",                  "microprocessor_knee"),
    (r"\bgastrostomy\b.*\blow[- ]profile\b",      "gastrostomy_low_profile"),
    (r"\bostomy\b.*\b(skin barrier|wafer)\b",     "ostomy_skin_barrier"),
    (r"\balginate\b.*\bdressing\b|\bdressing\b.*\balginate\b", "alginate_dressing"),
    (r"\bmanual wheelchair\b",                    "manual_wheelchair"),
    (r"\bpower wheelchair\b",                     "power_wheelchair_grp2"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), cat) for p, cat in _RULES]


def infer_category(text: str) -> Optional[str]:
    if not text:
        return None
    for rx, cat in _COMPILED:
        if rx.search(text):
            return cat
    return None


def _bridge_categories(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT device_category FROM bridge_hcpcs_to_product_code "
            "WHERE device_category IS NOT NULL ORDER BY 1"
        )
        return [r[0] for r in cur.fetchall()]


def tag_all(conn, use_llm: bool = True, llm_batch_size: int = 10,
            llm_max_rows: Optional[int] = None) -> dict:
    """Tag every fda_recalls row with NULL device_category using the layered
    device matcher (K# → product_code → manufacturer → regex → LLM).

    Persists every match dimension on the row:
      - matched_k_number
      - matched_product_code
      - device_category
      - match_method       ('k_number' | 'product_code' | 'manufacturer' | 'regex' | 'llm')
      - match_confidence   ('high' | 'medium' | 'low')

    Regex is always-on (manual rules); LLM contribution is additive and
    gated on GROQ_API_KEY + `use_llm`. On Groq failure, deterministic results
    still stand — nothing is lost."""
    # Local import so we avoid a circular dep at module load time (matcher
    # reuses `infer_category` from this file).
    from . import risk_device_matcher as _matcher

    with conn.cursor() as cur:
        cur.execute("""
            SELECT recall_number,
                   COALESCE(product_description, '') || ' ' ||
                   COALESCE(firm_name, '')           || ' ' ||
                   COALESCE(reason_for_recall, '')   AS haystack,
                   firm_name,
                   raw
            FROM fda_recalls
            WHERE device_category IS NULL
        """)
        fetched = cur.fetchall()

    if not fetched:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), COUNT(*) FILTER (WHERE device_category IS NOT NULL)
                FROM fda_recalls
            """)
            total, tagged = cur.fetchone()
        log.info("No untagged recalls.")
        return {"regex_updates": 0, "llm_updates": 0, "llm_used": False,
                "total": total, "tagged": tagged,
                "by_method": {}, "by_confidence": {}}

    # Decide LLM mode up-front for clear logging.
    if not use_llm:
        log.info("LLM disabled (--no-llm) — deterministic layers only")
        llm_on = False
    elif not risk_llm.is_enabled():
        log.info("GROQ_API_KEY not set — deterministic layers only")
        llm_on = False
    else:
        llm_on = True

    inputs = [
        {"_id": r[0], "text": r[1], "firm_name": r[2], "raw": r[3]}
        for r in fetched
    ]
    matches = _matcher.match_batch(
        conn, inputs,
        use_llm=llm_on,
        llm_batch_size=llm_batch_size,
        llm_max_rows=llm_max_rows,
    )

    # Persist. Always write all four provenance columns so match history is
    # auditable; writing NULL for unresolved rows is fine.
    updates = []
    for m in matches:
        if m.get("device_category") is None:
            continue
        updates.append((
            m.get("k_number"),
            m.get("product_code"),
            m.get("device_category"),
            m.get("method"),
            m.get("confidence"),
            m["_id"],
        ))
    if updates:
        with conn.cursor() as cur:
            cur.executemany(
                """UPDATE fda_recalls SET
                     matched_k_number      = %s,
                     matched_product_code  = %s,
                     device_category       = %s,
                     match_method          = %s,
                     match_confidence      = %s
                   WHERE recall_number = %s""",
                updates,
            )

    # Tallies
    by_method: dict = {}
    by_confidence: dict = {}
    for m in matches:
        if m.get("device_category"):
            by_method[m.get("method")]       = by_method.get(m.get("method"), 0) + 1
            by_confidence[m.get("confidence")] = by_confidence.get(m.get("confidence"), 0) + 1
    regex_updates = by_method.get("regex", 0)
    llm_updates   = by_method.get("llm", 0)

    # Coverage summary
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*), COUNT(*) FILTER (WHERE device_category IS NOT NULL)
            FROM fda_recalls
        """)
        total, tagged = cur.fetchone()

    log.info("Recall tagger: by_method=%s, by_confidence=%s, %d/%d total tagged (%.1f%%)",
             by_method, by_confidence, tagged, total,
             (tagged / total * 100 if total else 0.0))
    return {
        "regex_updates": regex_updates,
        "llm_updates":   llm_updates,
        "llm_used":      llm_on and llm_updates > 0,
        "total":         total,
        "tagged":        tagged,
        "by_method":     by_method,
        "by_confidence": by_confidence,
    }
