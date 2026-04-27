"""Unified Healthcare Device Risk Intelligence — assembler.

Reads the existing Postgres warehouse (hospitals, measures, MAUDE, GUDID,
bridge, recalls, clinical_trials, open_payments) and produces one
`hospital_device_risk_intelligence` row per (hospital × device_category),
carrying the full blueprint JSON in `payload`.

Risk logic is intentionally transparent so we can iterate:
  maude_score      — 0-10 normalized from (death*3 + injury*2 + malfunction) counts
  recall_override  — True if any Class I recall touches the device_category
  final_score      — max(maude_score, class-based recall floor), capped at 10
  hospital_device_link — 'weak' always (we don't have direct linkage, by design)
  device_risk         — 'strong' if >=50 events OR recall present, else 'moderate'/'weak'
"""

import logging
import math
from typing import Optional

import psycopg2.extras

from . import risk_db

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Device aggregates — cached per-run
# ------------------------------------------------------------------

def fetch_device_categories(conn) -> list[dict]:
    """Return one entry per device_category across the bridge AND the DRG map.

    Categories that only exist in the DRG→device map (e.g. hip_knee_implant,
    cardiac_valve, spinal_fusion_device) don't have HCPCS/product_code rows
    in the bridge — they're DRG-linked only. We still emit them because the
    hospital-level direct linkage via DRG volume is the whole point."""
    sql = """
        WITH bridge_cats AS (
          SELECT device_category,
                 MIN(product_code) AS product_code,
                 ARRAY_AGG(DISTINCT product_code) AS product_codes,
                 ARRAY_AGG(DISTINCT hcpcs_code)   AS hcpcs_codes
            FROM bridge_hcpcs_to_product_code
           WHERE device_category IS NOT NULL
           GROUP BY device_category
        ),
        drg_cats AS (
          SELECT DISTINCT device_category
            FROM drg_to_device_category
        ),
        all_cats AS (
          SELECT device_category FROM bridge_cats
          UNION
          SELECT device_category FROM drg_cats
        ),
        drg_codes AS (
          SELECT device_category, ARRAY_AGG(DISTINCT drg_code ORDER BY drg_code) AS drg_codes
            FROM drg_to_device_category
           GROUP BY device_category
        )
        SELECT a.device_category,
               b.product_code,
               COALESCE(b.product_codes, ARRAY[]::text[]) AS product_codes,
               COALESCE(b.hcpcs_codes,   ARRAY[]::text[]) AS hcpcs_codes,
               COALESCE(dc.drg_codes,    ARRAY[]::text[]) AS drg_codes
          FROM all_cats a
          LEFT JOIN bridge_cats b  USING (device_category)
          LEFT JOIN drg_codes   dc USING (device_category)
         ORDER BY a.device_category
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------
# DIRECT hospital-device linkage (CCN × device_category via DRG)
# ------------------------------------------------------------------

def fetch_hospital_device_volume(conn) -> dict:
    """{(ccn, device_category): {discharges, drg_codes, avg_medicare_payment, weighted_exposure}}.

    This is the real hospital-to-device bridge. For every DRG billed by every
    hospital, we split the discharge count across the device categories that
    DRG maps to (using the DRG map's `weight`), giving a per-(CCN, category)
    exposure estimate grounded in Medicare claims data.

    Example: DRG 469 (major joint replacement) maps 100% to hip_knee_implant,
    so if CCN 123456 had 500 DRG-469 discharges, (123456, hip_knee_implant)
    picks up 500 exposure points. DRG 246 (PCI with DES) maps 70% to
    drug_eluting_stent and 30% to bare_metal_stent, so 100 discharges become
    70 DES + 30 BMS exposure points.
    """
    sql = """
        SELECT v.ccn,
               m.device_category,
               SUM(v.total_discharges * m.weight) AS weighted_exposure,
               SUM(v.total_discharges)            AS total_discharges,
               ARRAY_AGG(DISTINCT v.drg_code ORDER BY v.drg_code) AS drg_codes,
               AVG(v.avg_medicare_payment)        AS avg_medicare_payment,
               MAX(v.year)                        AS data_year
          FROM cms_hospital_drg_volume v
          JOIN drg_to_device_category m ON lpad(m.drg_code, 3, '0') = lpad(v.drg_code, 3, '0')
         WHERE v.total_discharges IS NOT NULL AND v.total_discharges > 0
         GROUP BY v.ccn, m.device_category
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for ccn, cat, we, td, drgs, pay, yr in cur.fetchall():
            out[(ccn, cat)] = {
                "weighted_exposure":    float(we or 0),
                "total_discharges":     int(td or 0),
                "drg_codes":            list(drgs or []),
                "avg_medicare_payment": float(pay) if pay is not None else None,
                "data_year":            int(yr) if yr is not None else None,
            }
    return out


def fetch_hospital_complications(conn) -> dict:
    """{(ccn, device_category): [{measure_id, score, compared_to_national}]}.

    Pulls rows from the hospital_device_complications view so we can attach
    Hospital-Compare complication/readmission/mortality rates to the
    corresponding device category."""
    sql = """
        SELECT ccn, device_category_link, measure_id, measure_name,
               score, score_num, compared_to_national
          FROM hospital_device_complications
         WHERE device_category_link IS NOT NULL
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for ccn, cat, mid, mname, score, score_num, cmp in cur.fetchall():
            out.setdefault((ccn, cat), []).append({
                "measure_id":           mid,
                "measure_name":         mname,
                "score":                score,
                "score_num":            float(score_num) if score_num is not None else None,
                "compared_to_national": cmp,
            })
    return out


def fetch_maude_aggregate_by_category(conn) -> dict:
    """For each device_category in the bridge, aggregate MAUDE events.

    Returns { device_category: {total, death, injury, malfunction, regulatory_class_hint} }.
    Joins fda_maude_devices (product_code) through bridge to device_category,
    and counts events by event_type.
    """
    sql = """
        SELECT
            b.device_category,
            COUNT(DISTINCT e.report_number)                                      AS total_events,
            COUNT(DISTINCT e.report_number) FILTER (WHERE e.event_type = 'Death')      AS death_count,
            COUNT(DISTINCT e.report_number) FILTER (WHERE e.event_type = 'Injury')     AS injury_count,
            COUNT(DISTINCT e.report_number) FILTER (WHERE e.event_type = 'Malfunction') AS malfunction_count
        FROM fda_maude_events e
        JOIN fda_maude_devices d ON d.report_number = e.report_number
        JOIN bridge_hcpcs_to_product_code b ON b.product_code = d.product_code
        WHERE b.device_category IS NOT NULL
        GROUP BY b.device_category
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return {r["device_category"]: dict(r) for r in cur.fetchall()}


def fetch_recalls_by_category(conn) -> dict:
    """Return { device_category: {has_recall, worst_class, recall_count,
                                   match_methods, confidence_mix, recalls:[{...}]} }.

    Each recall in `recalls` now carries the tagger's `match_method` (how the
    device_category was inferred: bridge / regex / manufacturer / llm) and
    `match_confidence`, so a UI reader can tell a regex-inferred recall apart
    from a bridge-verified one. Aggregated counts are also rolled up to the
    category level so the score logic downstream can penalize categories that
    are only weakly matched."""
    sql = """
        SELECT device_category, recall_number, recall_class,
               firm_name, reason_for_recall, product_code,
               match_method, match_confidence, matched_product_code,
               matched_k_number
        FROM fda_recalls
        WHERE device_category IS NOT NULL
    """
    out: dict[str, dict] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        for r in cur.fetchall():
            cat = r["device_category"]
            slot = out.setdefault(cat, {
                "has_recall": True, "worst_class": None,
                "recall_count": 0, "recalls": [],
                "match_methods": {}, "confidence_mix": {},
            })
            slot["recall_count"] += 1
            mm = r["match_method"] or "unknown"
            mc = r["match_confidence"] or "unknown"
            slot["match_methods"][mm] = slot["match_methods"].get(mm, 0) + 1
            slot["confidence_mix"][mc] = slot["confidence_mix"].get(mc, 0) + 1
            # Keep the `recalls` list compact — 25 samples is plenty of JSON.
            if len(slot["recalls"]) < 25:
                slot["recalls"].append({
                    "recall_number":     r["recall_number"],
                    "recall_class":      r["recall_class"],
                    "firm_name":         r["firm_name"],
                    "reason_for_recall": r["reason_for_recall"],
                    "product_code":      r["product_code"],
                    "match_method":      mm,
                    "match_confidence":  mc,
                    "matched_product_code": r["matched_product_code"],
                    "matched_k_number":     r["matched_k_number"],
                })
            slot["worst_class"] = _worst_class(slot["worst_class"], r["recall_class"])
    return out


def fetch_maude_counts_by_k_number(conn) -> dict:
    """{ k_number: {event_count, death_count, injury_count, malfunction_count} }.

    Cross-reference every 510(k) clearance to MAUDE events by:
      - same product_code (required)
      - manufacturer substring match in EITHER direction after stripping
        corporate suffixes (Corp/Inc/Ltd/LLC/GmbH/…) and punctuation.

    A single MAUDE event may be counted under several K-numbers when the
    same applicant has multiple clearances with the same product_code —
    that's intentional; we're identifying every K# the event could plausibly
    be about. For per-hospital aggregates we still de-dupe at the event level.
    """
    # SQL regex: strip corporate suffixes + non-alphanumerics from manufacturer
    # / applicant to get a stable "normalized" name.
    suffix_rx = r'\b(corp(oration)?|inc(orporated)?|ltd|llc|co|company|limited|gmbh|sa|nv|ag|plc)\b\.?'
    sql = f"""
        WITH app_norm AS (
          SELECT k.k_number, k.product_code,
                 btrim(regexp_replace(
                   regexp_replace(lower(coalesce(k.applicant, '')),
                                  '{suffix_rx}', ' ', 'gi'),
                   '[^a-z0-9]+', ' ', 'g')) AS app_norm
            FROM fda_510k k
           WHERE k.product_code IS NOT NULL
        ),
        mfr_norm AS (
          SELECT d.report_number, d.product_code,
                 btrim(regexp_replace(
                   regexp_replace(lower(coalesce(d.manufacturer, '')),
                                  '{suffix_rx}', ' ', 'gi'),
                   '[^a-z0-9]+', ' ', 'g')) AS mfr_norm
            FROM fda_maude_devices d
           WHERE d.product_code IS NOT NULL AND d.manufacturer IS NOT NULL
        )
        SELECT a.k_number,
               COUNT(DISTINCT e.report_number)                                        AS event_count,
               COUNT(DISTINCT e.report_number) FILTER (WHERE e.event_type='Death')     AS death_count,
               COUNT(DISTINCT e.report_number) FILTER (WHERE e.event_type='Injury')    AS injury_count,
               COUNT(DISTINCT e.report_number) FILTER (WHERE e.event_type='Malfunction') AS malfunction_count
          FROM app_norm a
          JOIN mfr_norm m
            ON m.product_code = a.product_code
           AND length(a.app_norm) >= 3
           AND length(m.mfr_norm) >= 3
           AND (position(a.app_norm in m.mfr_norm) > 0
                OR position(m.mfr_norm in a.app_norm) > 0)
          JOIN fda_maude_events e ON e.report_number = m.report_number
         GROUP BY a.k_number
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for k, ev, dc, ic, mc in cur.fetchall():
            out[k] = {
                "event_count":       int(ev or 0),
                "death_count":       int(dc or 0),
                "injury_count":      int(ic or 0),
                "malfunction_count": int(mc or 0),
            }
    return out


def fetch_510k_catalog_by_category(conn, per_category_limit: int = 15,
                                      maude_counts: Optional[dict] = None) -> dict:
    """{ device_category: [ {k_number, applicant, device_name, product_code,
                              decision_date, regulatory_class, maude:{...}} ] }.

    For every bridge device_category, list the N most-recently-cleared 510(k)
    devices — these are the FDA-cleared products a hospital in this category
    is plausibly using. This is the device "catalog" per category.

    If `maude_counts` is passed (from fetch_maude_counts_by_k_number), each
    catalog entry is enriched with a `maude` sub-object containing that K#'s
    actual adverse-event counts — so the UI can surface "this specific
    cleared device has 12 events including 3 deaths."
    """
    # Sort priority:
    #   1. K#s with MAUDE events first (most clinically relevant)
    #   2. Then most-recent decision date
    # The CTE uses the maude_counts Python dict as a dynamic values table so
    # we don't need a temp table. `maude_counts` is keyed by k_number.
    counts_values = []
    for k, stats in (maude_counts or {}).items():
        counts_values.append((k, stats.get("event_count", 0)))
    counts_values_sql = ""
    if counts_values:
        # Build `(k_number, events)` VALUES tuples for a lateral join
        vals = ",".join(
            conn.cursor().mogrify("(%s,%s)", (k, e)).decode()
            for k, e in counts_values
        ) if False else None  # placeholder, we use the Python list below instead
    sql = """
        WITH event_counts(k_number, event_count) AS (
          SELECT k, e FROM unnest(%s::text[], %s::int[]) AS t(k, e)
        ),
        ranked AS (
          SELECT b.device_category,
                 k.k_number, k.applicant, k.device_name, k.product_code,
                 k.decision_date,
                 k.raw->'openfda'->>'device_class' AS device_class,
                 COALESCE(ec.event_count, 0) AS event_count,
                 ROW_NUMBER() OVER (
                     PARTITION BY b.device_category
                     ORDER BY COALESCE(ec.event_count, 0) DESC,
                              k.decision_date DESC NULLS LAST,
                              k.k_number
                 ) AS rk
            FROM fda_510k k
            JOIN bridge_hcpcs_to_product_code b ON b.product_code = k.product_code
            LEFT JOIN event_counts ec ON ec.k_number = k.k_number
        )
        SELECT device_category, k_number, applicant, device_name,
               product_code, decision_date, device_class
          FROM ranked
         WHERE rk <= %s
         ORDER BY device_category, rk
    """
    ks = [k for k, _ in counts_values]
    evs = [e for _, e in counts_values]
    name_map = {"1": "Class I", "2": "Class II", "3": "Class III",
                "U": "Unclassified"}
    out: dict[str, list] = {}
    with conn.cursor() as cur:
        cur.execute(sql, [ks, evs, per_category_limit])
        for cat, k, app, name, pc, dd, dc in cur.fetchall():
            entry = {
                "k_number":         k,
                "applicant":        app,
                "device_name":      name,
                "product_code":     pc,
                "decision_date":    dd.isoformat() if dd else None,
                "regulatory_class": name_map.get(dc),
            }
            if maude_counts is not None:
                entry["maude"] = maude_counts.get(k) or {
                    "event_count": 0, "death_count": 0,
                    "injury_count": 0, "malfunction_count": 0,
                }
            out.setdefault(cat, []).append(entry)
    return out


def fetch_regulatory_class_by_category(conn) -> dict:
    """{ device_category: "Class I" | "Class II" | "Class III" | None }.

    Extracts openfda.device_class from fda_510k.raw and picks the dominant
    (most-common) class for each bridge device_category. No 510k re-ingest
    needed — the data is already in the raw JSONB."""
    sql = """
        WITH counts AS (
          SELECT b.device_category,
                 k.raw->'openfda'->>'device_class' AS device_class,
                 COUNT(*) AS n
            FROM fda_510k k
            JOIN bridge_hcpcs_to_product_code b ON b.product_code = k.product_code
           WHERE k.raw->'openfda'->>'device_class' IN ('1','2','3','U')
           GROUP BY 1, 2
        ),
        ranked AS (
          SELECT device_category, device_class, n,
                 ROW_NUMBER() OVER (PARTITION BY device_category ORDER BY n DESC) AS rk
            FROM counts
        )
        SELECT device_category, device_class
          FROM ranked
         WHERE rk = 1;
    """
    name_map = {"1": "Class I", "2": "Class II", "3": "Class III", "U": "Unclassified"}
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for cat, dc in cur.fetchall():
            out[cat] = name_map.get(dc)
    return out


def fetch_trials_by_category(conn) -> dict:
    """{ bridge_device_category: {trial_count, completed, with_results, active} }.

    Our trial ingestion buckets (e.g. "pacemaker") are coarser than the
    bridge's device_category vocabulary (pacemaker_dual_chamber,
    pacemaker_single_chamber, pacemaker_lead, …). We expand each CTG bucket
    to every bridge category it plausibly covers so the UI/assembler can
    join cleanly on the bridge's category slug."""
    sql = """
        SELECT device_category AS bucket,
               COUNT(*)                                                     AS trial_count,
               COUNT(*) FILTER (WHERE overall_status = 'COMPLETED')          AS completed,
               COUNT(*) FILTER (WHERE has_results IS TRUE)                   AS with_results,
               COUNT(*) FILTER (WHERE overall_status IN (
                   'RECRUITING','ACTIVE_NOT_RECRUITING','ENROLLING_BY_INVITATION'
               ))                                                            AS active
        FROM clinical_trials
        WHERE device_category IS NOT NULL
        GROUP BY device_category
    """
    raw: dict = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        for r in cur.fetchall():
            raw[r["bucket"]] = dict(r)

    # Coarse CTG bucket → concrete bridge categories.
    bucket_to_bridge = {
        "coronary stent":   ["bare_metal_stent", "drug_eluting_stent",
                              "atherectomy_catheter", "ivus_catheter"],
        "hip_knee_implant": ["microprocessor_knee", "microprocessor_knee_addn",
                              "bone_anchor", "bone_anchor_absorbable"],
        "pacemaker":        ["pacemaker_dual_chamber", "pacemaker_single_chamber",
                              "pacemaker_lead", "pacemaker_lead_combo",
                              "pacemaker_lead_vdd", "crt_p"],
        "defibrillator":    ["icd_dual_chamber", "icd_single_chamber",
                              "icd_lead_dual_coil", "icd_lead_single_coil",
                              "crt_d", "lead_coronary_venous"],
        "insulin_pump":     ["insulin_pump", "cgm_receiver", "cgm_supply"],
        "intraocular_lens": ["iol_anterior_chamber", "iol_posterior_chamber"],
        "urinary_catheter": ["urinary_catheter_foley", "foley_tray_2way_latex",
                              "foley_tray_2way_silicone", "indwelling_silicone"],
        "cpap":             ["cpap_device", "rad_no_backup", "rad_with_backup",
                              "home_ventilator_niv", "home_ventilator_invasive"],
        "neurostimulator":  ["neurostim_implant", "neurostim_electrode"],
    }

    out: dict = {}
    for bucket, stats in raw.items():
        # Same stats replicated onto every bridge category the bucket covers.
        # Callers only read counts, never aggregate across categories here,
        # so there's no double-counting concern.
        targets = bucket_to_bridge.get(bucket, [bucket])
        for cat in targets:
            out[cat] = {"trial_count":  stats["trial_count"],
                        "completed":    stats["completed"],
                        "with_results": stats["with_results"],
                        "active":       stats["active"]}
    return out


def fetch_open_payments_by_ccn(conn) -> dict:
    """{ ccn: {total_payments, manufacturer_count} } — crude manufacturer-exposure proxy."""
    sql = """
        SELECT teaching_hospital_ccn AS ccn,
               SUM(COALESCE(payment_total, 0)) AS total_payments,
               COUNT(DISTINCT manufacturer_name) AS manufacturer_count
        FROM cms_open_payments
        WHERE teaching_hospital_ccn IS NOT NULL
          AND teaching_hospital_ccn <> ''
        GROUP BY teaching_hospital_ccn
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        return {r["ccn"]: dict(r) for r in cur.fetchall()}


def fetch_hospitals(conn, limit: Optional[int] = None,
                    state: Optional[str] = None,
                    ccn: Optional[str] = None) -> list[dict]:
    where = []
    params: list = []
    if ccn:
        where.append("facility_id = %s")
        params.append(ccn)
    if state:
        where.append("state = %s")
        params.append(state)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    sql = f"""
        SELECT facility_id, facility_name, city, state, zip_code,
               hospital_type, hospital_ownership, overall_rating
        FROM cms_hospitals
        {where_sql}
        ORDER BY facility_id
        {limit_sql}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------
# Scoring
# ------------------------------------------------------------------

_CLASS_ORDER = {"Class I": 3, "Class II": 2, "Class III": 1}


def _worst_class(current: Optional[str], new: Optional[str]) -> Optional[str]:
    if not new:
        return current
    new_norm = _normalize_class(new)
    if not current:
        return new_norm
    return new_norm if _CLASS_ORDER.get(new_norm, 0) > _CLASS_ORDER.get(current, 0) else current


def _normalize_class(c: Optional[str]) -> Optional[str]:
    if not c:
        return None
    s = str(c).strip()
    upper = s.upper().replace("CLASS ", "")
    if upper.startswith("I") and not upper.startswith("II"):
        return "Class I"
    if upper.startswith("II") and not upper.startswith("III"):
        return "Class II"
    if upper.startswith("III"):
        return "Class III"
    return s


def _maude_score(events: Optional[dict]) -> float:
    """Weighted severity log-score normalized to 0-10."""
    if not events:
        return 0.0
    deaths = int(events.get("death_count") or 0)
    injuries = int(events.get("injury_count") or 0)
    malf = int(events.get("malfunction_count") or 0)
    weighted = deaths * 3 + injuries * 2 + malf
    if weighted <= 0:
        return 0.0
    # log scaling: 10 events -> ~2.3, 100 -> ~4.6, 1000 -> ~6.9, 10000 -> ~9.2
    score = math.log(1 + weighted) / math.log(10) * 2.3
    return round(min(score, 10.0), 2)


def _recall_floor(worst_class: Optional[str]) -> float:
    """Recall severity floor — Class I recalls push final_score to at least 9.0."""
    return {"Class I": 9.0, "Class II": 6.5, "Class III": 4.0}.get(worst_class, 0.0)


def _manufacturer_exposure(op_agg: Optional[dict]) -> str:
    if not op_agg:
        return "none"
    total = float(op_agg.get("total_payments") or 0)
    if total >= 1_000_000:
        return "high"
    if total >= 100_000:
        return "medium"
    if total > 0:
        return "low"
    return "none"


def _device_risk_confidence(events: dict, recall: dict) -> str:
    if recall.get("has_recall"):
        return "strong"
    total = int((events or {}).get("total_events") or 0)
    if total >= 50:
        return "strong"
    if total >= 10:
        return "moderate"
    return "weak"


def _linkage_confidence(exposure_discharges: int, comps: list,
                        op_agg: dict) -> tuple[str, float]:
    """Classify how well we actually know this hospital touches this device,
    and return a damping multiplier for the global device-risk score.

    Naming note: we deliberately avoid the word "strong". Our best evidence
    (hospital billed Medicare under the matching DRGs) tells us the hospital
    does the *procedure*, not which specific *device* was implanted. True
    device-level linkage would need UDI/GUDID capture at time-of-service,
    which we don't have. So the top tier is "procedural", not "strong".

      procedural   (Medicare claims: >= 100 discharges under mapped DRGs) → 0.85
      procedural_low (1–99 DRG discharges — small-volume evidence)        → 0.60
      categorical  (Compare measures or Open-Payments overlap only)       → 0.45
      none         (no CCN-scoped evidence at all)                        → 0.15

    The multiplier is intentionally conservative — even a hospital that
    actively performs the procedure doesn't fully inherit the global device-
    family safety score, because its specific device mix / vendor / vintage
    may differ. We expose exposure_bonus separately so volume still moves
    the score in a transparent, non-multiplicative way.
    """
    has_compare = bool(comps)
    has_payments = bool(op_agg and float(op_agg.get("total_payments") or 0) > 0)
    if exposure_discharges >= 100:
        return ("procedural", 0.85)
    if exposure_discharges > 0:
        return ("procedural_low", 0.60)
    if has_compare or has_payments:
        return ("categorical", 0.45)
    return ("none", 0.15)


def _exposure_bonus(discharges: int) -> float:
    """Additive volume bump that moves the hospital-specific score up
    for high-volume hospitals *without* multiplying the global score.

    Multiplicative scaling (old approach) created a bug where 107 discharges
    × global 9.0 = 10.35 → capped at 10 — indistinguishable from a 10,000-
    discharge specialist. An additive log bonus preserves rank ordering but
    keeps the top of the scale reserved for cases that have both a severe
    global signal AND extreme hospital volume.

    Values:
        20 discharges  → +0.52
        100            → +0.80
        500            → +1.08
        5,000          → +1.48   (capped at 1.50)
    """
    if discharges <= 0:
        return 0.0
    import math
    return round(min(math.log10(1 + discharges) * 0.4, 1.5), 2)


def _quality_score(overall_rating) -> Optional[float]:
    if overall_rating in (None, "", "Not Available", "N/A"):
        return None
    try:
        return float(overall_rating)
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------
# Per-row assembly
# ------------------------------------------------------------------

def build_row(hospital: dict, category: dict,
              maude: dict, recall: dict, trials: dict,
              op: dict, reg_class: dict = None,
              catalog_510k: dict = None,
              device_volume: dict = None,
              complications: dict = None) -> dict:
    cat_slug = category["device_category"]
    ccn      = hospital["facility_id"]
    events = maude.get(cat_slug) or {}
    rec = recall.get(cat_slug) or {"has_recall": False}
    trial = trials.get(cat_slug) or {}
    op_agg = op.get(ccn) or {}
    regulatory_class = (reg_class or {}).get(cat_slug)
    catalog = (catalog_510k or {}).get(cat_slug) or []
    volume   = (device_volume or {}).get((ccn, cat_slug)) or {}
    comps    = (complications or {}).get((ccn, cat_slug)) or []

    # ---- Global device-risk scoring (hospital-agnostic) ----
    maude_sc     = _maude_score(events)
    recall_floor = _recall_floor(rec.get("worst_class"))
    # Global score is the raw device signal from MAUDE + recalls, capped at 10.
    # This is the "what does the world know about this device family" number.
    global_score = round(min(max(maude_sc, recall_floor), 10.0), 2)

    # ---- Hospital-context exposure ----
    exposure_discharges = int(volume.get("total_discharges") or 0)
    exposure_bonus = _exposure_bonus(exposure_discharges)

    # ---- Linkage confidence × damping ----
    # Without evidence that THIS hospital touches this device, the global
    # signal should not transfer at full strength. Damping is multiplicative
    # on the global score; exposure is *additive* on top so a 107-discharge
    # hospital doesn't get the same 10.0 as a 10,000-discharge specialist.
    hosp_link_conf, linkage_multiplier = _linkage_confidence(
        exposure_discharges, comps, op_agg,
    )
    hospital_final_score = round(
        min(global_score * linkage_multiplier + exposure_bonus, 10.0), 2,
    )

    # Pure exposure-adjusted view (what if we *assumed* full linkage?) —
    # so the UI can still rank within a category by pure volume, independent
    # of linkage-confidence damping.
    exposure_adjusted = round(min(global_score + exposure_bonus, 10.0), 2)

    quality = _quality_score(hospital.get("overall_rating"))
    exposure = _manufacturer_exposure(op_agg)
    device_conf = _device_risk_confidence(events, rec)

    linkage_evidence = []
    if exposure_discharges > 0:
        linkage_evidence.append("medicare_inpatient_drg")
    if comps:
        linkage_evidence.append("hospital_compare_measures")
    if op_agg and float(op_agg.get("total_payments") or 0) > 0:
        linkage_evidence.append("cms_open_payments")

    payload = {
        # -------- identity --------
        "hospital": {
            "ccn":           hospital["facility_id"],
            "name":          hospital.get("facility_name"),
            "city":          hospital.get("city"),
            "state":         hospital.get("state"),
            "zip_code":      hospital.get("zip_code"),
            "type":          hospital.get("hospital_type"),
            "ownership":     hospital.get("hospital_ownership"),
            "quality_score": quality,
        },
        "device": {
            "category":         category["device_category"],
            "product_code":     category.get("product_code"),
            "product_codes":    list(category.get("product_codes") or []),
            "hcpcs_codes":      list(category.get("hcpcs_codes") or []),
            "drg_codes":        list(category.get("drg_codes") or []),
            "regulatory_class": regulatory_class,   # dominant class from fda_510k.openfda
        },

        # -------- GLOBAL device-risk signal (same for every hospital) --------
        # This block is intentionally hospital-independent. The numbers here
        # describe the device family's safety profile in the world, not at
        # THIS hospital. Use `hospital_context` + `risk.hospital_final_score`
        # to read hospital-specific risk.
        "device_risk_global": {
            "adverse_events": {
                "total_events":      int(events.get("total_events") or 0),
                "death_count":       int(events.get("death_count") or 0),
                "injury_count":      int(events.get("injury_count") or 0),
                "malfunction_count": int(events.get("malfunction_count") or 0),
            },
            "recall": {
                "has_recall":       bool(rec.get("has_recall")),
                "recall_class":     rec.get("worst_class"),
                "recall_count":     int(rec.get("recall_count") or 0),
                "match_methods":    rec.get("match_methods") or {},
                "confidence_mix":   rec.get("confidence_mix") or {},
                "recalls":          rec.get("recalls") or [],
            },
            "clinical_evidence": {
                "trial_count":  int(trial.get("trial_count") or 0),
                "completed":    int(trial.get("completed") or 0),
                "with_results": int(trial.get("with_results") or 0),
                "active":       int(trial.get("active") or 0),
            },
            "device_risk_confidence": device_conf,
            "global_score":           global_score,
            "scoring": {
                "maude_score":     maude_sc,
                "recall_floor":    recall_floor,
                "recall_override": recall_floor > maude_sc,
            },
        },

        # -------- HOSPITAL-SPECIFIC context (varies per hospital) --------
        # Everything here is CCN-scoped evidence that this hospital actually
        # touches this device category. Strong evidence = Medicare billing
        # under the matching DRGs. Moderate = Hospital-Compare complications
        # or manufacturer Open-Payments overlap. Weak = no direct evidence.
        "hospital_context": {
            "procedure_volume": {
                "has_direct_claims_linkage": bool(volume),
                "discharges":                exposure_discharges,
                "weighted_exposure":         round(float(volume.get("weighted_exposure") or 0), 1),
                "drg_codes_billed":          list(volume.get("drg_codes") or []),
                "avg_medicare_payment":      volume.get("avg_medicare_payment"),
                "data_year":                 volume.get("data_year"),
            },
            "compare_measures": {
                "has_data": bool(comps),
                "measures": comps,
            },
            "manufacturer_overlap": {
                "npi_linked":            bool(op_agg),
                "manufacturer_exposure": exposure,
                "total_payments_usd":    float(op_agg.get("total_payments") or 0.0),
                "manufacturer_count":    int(op_agg.get("manufacturer_count") or 0),
            },
            "linkage_evidence":   linkage_evidence,
            "linkage_confidence": hosp_link_conf,
        },

        # -------- RISK (combines the two above) --------
        "risk": {
            # Global device signal (same for every CCN) — for comparing the
            # device family across hospitals.
            "global_score":        global_score,

            # Damping knobs that turn the global score into a hospital-
            # specific score. Transparent so reviewers can see how we got
            # from global to local.
            #
            #   hospital_final_score = min(global × linkage_multiplier + exposure_bonus, 10)
            #
            # linkage_confidence ∈ {procedural, procedural_low, categorical, none}
            # — see _linkage_confidence() for the exact bands. "procedural"
            # is the top tier; we deliberately avoid the word "strong" because
            # our best evidence (DRG billing) is procedure-level, not device-
            # level — we know the hospital does the operation, not which
            # specific device they used. True device-level linkage needs UDI.
            "linkage_confidence":  hosp_link_conf,
            "linkage_multiplier":  linkage_multiplier,
            "exposure_bonus":      exposure_bonus,
            "hospital_final_score":    hospital_final_score,

            # What the score would be IF we assumed full linkage — so the UI
            # can still rank hospitals within a category by pure volume.
            "exposure_adjusted_score": exposure_adjusted,
        },

        "device_510k_catalog": {
            "count":   len(catalog),
            "devices": catalog,
        },
        "meta": {
            "linkage_type": "direct" if exposure_discharges > 0 else "indirect",
            "sources": [
                "CMS Hospital General Information",
                "CMS Medicare Inpatient PUF (CCN × DRG discharges)",
                "CMS Hospital Compare measures",
                "FDA MAUDE",
                "FDA Device Enforcement (recalls)",
                "FDA 510(k) Clearances",
                "FDA Device Classification",
                "FDA GUDID (UDI-DI, brand, manufacturer)",
                "ClinicalTrials.gov",
                "CMS Open Payments",
                "bridge_hcpcs_to_product_code",
                "drg_to_device_category",
            ],
            "schema_version": 2,
        },
    }

    return {
        "ccn":                hospital["facility_id"],
        "device_category":    category["device_category"],
        "product_code":       category.get("product_code"),
        "hospital_name":      hospital.get("facility_name"),
        "state":              hospital.get("state"),
        "quality_score":      quality,
        "total_events":       payload["device_risk_global"]["adverse_events"]["total_events"],
        "death_count":        payload["device_risk_global"]["adverse_events"]["death_count"],
        "injury_count":       payload["device_risk_global"]["adverse_events"]["injury_count"],
        "malfunction_count":  payload["device_risk_global"]["adverse_events"]["malfunction_count"],
        "has_recall":         payload["device_risk_global"]["recall"]["has_recall"],
        "worst_recall_class": payload["device_risk_global"]["recall"]["recall_class"],
        "trial_count":        payload["device_risk_global"]["clinical_evidence"]["trial_count"],
        "manufacturer_exposure": exposure,
        "maude_score":        maude_sc,
        "recall_override":    payload["device_risk_global"]["scoring"]["recall_override"],
        # `final_score` column is now the hospital-specific (linkage-damped)
        # score so sorting/filtering behaves like a real hospital risk view.
        "final_score":        hospital_final_score,
        "hospital_device_link_confidence": hosp_link_conf,
        "device_risk_confidence":          device_conf,
        "linkage_type":       "direct" if exposure_discharges > 0 else "indirect",
        "payload":            payload,
    }


# ------------------------------------------------------------------
# Driver
# ------------------------------------------------------------------

def assemble(conn, limit_hospitals: Optional[int] = None,
             state: Optional[str] = None, ccn: Optional[str] = None,
             batch_size: int = 500) -> dict:
    """Produce hospital_device_risk_intelligence rows.

    Pre-computes every category-level aggregate once, then iterates hospitals
    and fans out to (hospital × every device_category in the bridge)."""
    # Log open/close runs on fresh short-lived connections so an aborted
    # data-path transaction doesn't poison the audit write.
    with risk_db.connect() as log_conn:
        handle = risk_db.start_log(log_conn, "assemble", params={
            "limit_hospitals": limit_hospitals, "state": state, "ccn": ccn,
        })

    total = 0
    try:
        log.info("fetching device categories + aggregates")
        categories = fetch_device_categories(conn)
        if not categories:
            log.warning("bridge_hcpcs_to_product_code has no device_category rows")
            with risk_db.connect() as log_conn:
                risk_db.finish_log(log_conn, handle, 0, 0, "ok")
            return {"rows": 0, "categories": 0, "hospitals": 0}

        maude = fetch_maude_aggregate_by_category(conn)
        recalls = fetch_recalls_by_category(conn)
        trials = fetch_trials_by_category(conn)
        op = fetch_open_payments_by_ccn(conn)
        reg_class = fetch_regulatory_class_by_category(conn)
        maude_by_k = fetch_maude_counts_by_k_number(conn)
        catalog_510k = fetch_510k_catalog_by_category(conn, maude_counts=maude_by_k)
        device_volume  = fetch_hospital_device_volume(conn)
        complications  = fetch_hospital_complications(conn)
        log.info("aggregates: %d categories | maude=%d recalls=%d trials=%d "
                 "reg_class=%d 510k_catalog=%d K#_with_events=%d op_ccns=%d "
                 "direct_volume_links=%d complication_links=%d",
                 len(categories), len(maude), len(recalls), len(trials),
                 len(reg_class), len(catalog_510k), len(maude_by_k), len(op),
                 len(device_volume), len(complications))

        hospitals = fetch_hospitals(conn, limit=limit_hospitals, state=state, ccn=ccn)
        log.info("assembling for %d hospitals × %d categories = %d rows",
                 len(hospitals), len(categories), len(hospitals) * len(categories))

        batch: list[dict] = []
        for h in hospitals:
            for cat in categories:
                batch.append(build_row(h, cat, maude, recalls, trials, op,
                                         reg_class, catalog_510k,
                                         device_volume=device_volume,
                                         complications=complications))
                if len(batch) >= batch_size:
                    risk_db.upsert_risk_intelligence(conn, batch)
                    total += len(batch)
                    batch = []
        if batch:
            risk_db.upsert_risk_intelligence(conn, batch)
            total += len(batch)

        with risk_db.connect() as log_conn:
            risk_db.finish_log(log_conn, handle, total, total, "ok")
        return {"rows": total, "categories": len(categories), "hospitals": len(hospitals)}
    except Exception as e:
        try:
            with risk_db.connect() as log_conn:
                risk_db.finish_log(log_conn, handle, 0, total, "error", str(e))
        except Exception:
            log.exception("could not persist failure log")
        raise
