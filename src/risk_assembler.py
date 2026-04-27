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
    # Strict-joins-only mode: we only include recalls whose device_category
    # was resolved via a structured FDA identifier (matched_product_code or
    # matched_k_number). Rows tagged only by regex/LLM are excluded from
    # hospital risk aggregation — they stay queryable in fda_recalls but
    # do not count as evidence for this device family at this hospital.
    sql = """
        SELECT device_category, recall_number, recall_class,
               firm_name, reason_for_recall, product_code,
               match_method, match_confidence, matched_product_code,
               matched_k_number
        FROM fda_recalls
        WHERE device_category IS NOT NULL
          AND (matched_product_code IS NOT NULL OR matched_k_number IS NOT NULL)
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
           AND a.app_norm = m.mfr_norm   -- strict normalized equality, no substring/fuzzy
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


def fetch_sparcs_ppc_by_ccn(conn) -> dict:
    """{(ccn, device_category): [{year, ppc_group, adjusted_rate, significance, weight}, ...]}.

    NY-only (for now — can extend to PA/CA). Joins SPARCS PPC rates to our
    device_category vocabulary via the curated `sparcs_ppc_to_device_category`
    map. The linkage hospital→CCN is structural (ny_pfi_to_ccn), not fuzzy."""
    sql = """
        SELECT s.ccn, m.device_category,
               s.discharge_year, s.ppc_group_name,
               s.observed_rate, s.adjusted_rate, s.significance,
               m.weight
          FROM sparcs_ppc_rate s
          JOIN sparcs_ppc_to_device_category m
            ON m.ppc_group_name = s.ppc_group_name
         WHERE s.ccn IS NOT NULL
           AND s.adjusted_rate IS NOT NULL
           AND s.discharge_year >= 2020
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for ccn, cat, yr, grp, obs, adj, sig, w in cur.fetchall():
            key = (ccn, cat)
            out.setdefault(key, []).append({
                "year":          int(yr),
                "ppc_group":     grp,
                "observed_rate": float(obs) if obs is not None else None,
                "adjusted_rate": float(adj) if adj is not None else None,
                "significance":  sig,
                "weight":        float(w),
            })
    return out


_MFR_NORM_SQL = """
    lower(regexp_replace(
        regexp_replace(
          regexp_replace($src$,
            '\\m(corp(oration)?|inc(orporated)?|ltd|llc|co|company|limited|gmbh|sa|nv|ag|plc|lp)\\M\\.?',
            '', 'gi'),
          '\\musa\\M', '', 'gi'),
        '[^A-Za-z0-9]+', '', 'g'))
"""


def _mfr_norm_expr(col: str) -> str:
    """Return the SQL expression that normalizes a manufacturer/applicant
    column. Strips corporate suffixes + country markers + punctuation so
    that 'DePuy Synthes Products, Inc.' and 'DePuy Synthes Products' and
    'DePUY SYNTHES PRODUCTS LLC' all collapse to one key.

    Still rule-based/structural — no fuzzy, no LLM."""
    return _MFR_NORM_SQL.replace("$src$", col)


def fetch_manufacturer_to_categories(conn) -> dict:
    """{parent_name: set(device_category)}.

    Combines manufacturer_to_product_code (FDA 510k → product_code →
    device_category) with manufacturer_alias (parent_company resolver), so
    'Medtronic Vascular', 'Medtronic Sofamor Danek', 'Medtronic Inc' all
    collapse to a single 'Medtronic' key whose category set is the UNION
    of every subsidiary's filings. Open Payments lookups hit this same
    parent map. Zero regex, zero fuzzy — equality match on normalized
    strings both for the 510k applicant → parent and OP mfr → parent.
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT
                   coalesce(a.parent_name, m.manufacturer_norm) AS parent,
                   m.device_category
              FROM manufacturer_to_product_code m
         LEFT JOIN manufacturer_alias a ON a.alias_norm = m.manufacturer_norm
             WHERE m.device_category IS NOT NULL
        """)
        for parent, cat in cur.fetchall():
            if parent:
                out.setdefault(parent, set()).add(cat)
    return out


def fetch_op_manufacturers_by_ccn(conn) -> dict:
    """{ccn: [parent_name, ...]}.

    Normalizes Open Payments manufacturer_name the same way, then resolves
    to parent_name via manufacturer_alias when possible. Unaliased names
    pass through as their own normalized form — they still join on
    equality to whatever's in fda_510k under the same normalization, so
    coverage degrades gracefully.
    """
    sql = f"""
        SELECT DISTINCT
               teaching_hospital_ccn,
               coalesce(a.parent_name, {_mfr_norm_expr('manufacturer_name')}) AS parent
          FROM cms_open_payments op
     LEFT JOIN manufacturer_alias a
            ON a.alias_norm = {_mfr_norm_expr('manufacturer_name')}
         WHERE teaching_hospital_ccn IS NOT NULL
           AND teaching_hospital_ccn <> ''
           AND manufacturer_name IS NOT NULL
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for ccn, parent in cur.fetchall():
            if parent:
                out.setdefault(ccn, []).append(parent)
    return out



def fetch_ca_tavr_by_ccn(conn) -> dict:
    """{ccn: {facility_name, year, volume, mortality, comparison}} — cardiac_valve only."""
    sql = """
        SELECT ccn, facility_name, report_year, tavr_volume,
               risk_adjusted_mortality_rate, statewide_comparison
          FROM ca_tavr_outcome
         WHERE ccn IS NOT NULL
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for ccn, name, yr, vol, mort, cmp in cur.fetchall():
            out[ccn] = {
                "facility_name": name, "report_year": yr,
                "tavr_volume": vol,
                "risk_adjusted_mortality_rate": float(mort) if mort is not None else None,
                "statewide_comparison": cmp,
            }
    return out


def fetch_pa_phc4_by_ccn(conn) -> dict:
    """{(ccn, condition): {...}} — PHC4 hospital performance per condition (CABG/etc)."""
    sql = """
        SELECT ccn, condition, mortality_rate, readmission_rate, volume, rating
          FROM pa_phc4_outcome WHERE ccn IS NOT NULL
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for ccn, cond, mort, readm, vol, rating in cur.fetchall():
            out.setdefault(ccn, []).append({
                "condition": cond, "mortality_rate": mort,
                "readmission_rate": readm, "volume": vol, "rating": rating,
            })
    return out


def fetch_news_by_ccn(conn) -> dict:
    """{ccn: [{title, source, published_date, url, snippet}, ...]}."""
    sql = """
        SELECT ccn, title, source, published_date, url, snippet
          FROM hospital_news WHERE ccn IS NOT NULL
         ORDER BY fetched_at DESC
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for ccn, title, src, dt, url, sn in cur.fetchall():
            out.setdefault(ccn, []).append({
                "title": title, "source": src, "published_date": dt,
                "url": url, "snippet": sn,
            })
    return out


# Condition → device_category mapping for PA PHC4 outcomes.
_PHC4_COND_TO_CAT = {
    "CABG":                  "cabg_conduit",
    "Coronary Artery Bypass": "cabg_conduit",
    "AMI":                   "drug_eluting_stent",
    "Heart Attack":          "drug_eluting_stent",
    "Pneumonia":             None,
}


def fetch_leapfrog_by_ccn(conn) -> dict:
    """{ ccn: {safety_grade, profile_url} } — Leapfrog A-F hospital quality grade."""
    sql = """
        SELECT ccn, safety_grade, profile_url
          FROM leapfrog_grade
         WHERE ccn IS NOT NULL AND safety_grade IS NOT NULL
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for ccn, grade, url in cur.fetchall():
            out[ccn] = {"safety_grade": grade, "profile_url": url}
    return out


def fetch_manufacturer_quality_by_category(conn) -> dict:
    """{device_category: {warning_letters:N, oai_inspections:N, firms:[...]}}.

    Joins scraped FDA 483 + Warning Letter tables through the 510k master
    (by firm_name ↔ applicant ↔ product_code) down to device_category. This
    is how manufacturer-level quality issues flow into per-device risk."""
    sql = """
        WITH pc_to_cat AS (
          SELECT DISTINCT product_code, device_category
            FROM bridge_hcpcs_to_product_code
           WHERE device_category IS NOT NULL
             AND product_code   IS NOT NULL
        ),
        applicants AS (
          SELECT DISTINCT
                 pc.device_category,
                 regexp_replace(lower(coalesce(k.applicant,'')),
                                '[^a-z0-9]+', ' ', 'g') AS firm_norm,
                 k.applicant
            FROM fda_510k k
            JOIN pc_to_cat pc ON pc.product_code = k.product_code
           WHERE k.applicant IS NOT NULL
        )
        SELECT a.device_category,
               COUNT(DISTINCT i.id) FILTER (WHERE i.classification ILIKE 'OAI%'
                                               OR i.classification ILIKE '%Official%') AS oai_inspections,
               ARRAY_AGG(DISTINCT a.applicant)
                 FILTER (WHERE i.id IS NOT NULL) AS flagged_firms
          FROM applicants a
          JOIN fda_483_inspection i
            ON i.firm_name_norm = a.firm_norm
           AND length(i.firm_name_norm) > 3
         GROUP BY a.device_category
        HAVING COUNT(DISTINCT i.id) FILTER (WHERE i.classification ILIKE 'OAI%'
                                               OR i.classification ILIKE '%Official%') > 0
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for cat, oai, firms in cur.fetchall():
            out[cat] = {
                "oai_inspections":   int(oai or 0),
                "flagged_firms":     list(firms or [])[:10],
            }
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
    """Additive volume bump — tighter than v1 so 183 discharges doesn't near-
    max the bump. We want the exposure bonus to differentiate specialists
    (thousands of discharges) from generalist hospitals, not saturate at
    mid-volume.

    New values (log10(1+N) * 0.25, capped at 1.0):
        20 discharges  → +0.33
        100            → +0.50
        500            → +0.68
        5,000          → +0.92
        50,000         → +1.00 (capped)
    """
    if discharges <= 0:
        return 0.0
    import math
    return round(min(math.log10(1 + discharges) * 0.25, 1.0), 2)


def _manufacturer_bonus(op_agg: dict, mfr_cat_match: bool) -> float:
    """Small additive bonus when manufacturer Open Payments overlap is
    present AND the manufacturers making payments produce devices in this
    category (via fda_510k.applicant → product_code → device_category).

    If the manufacturer-to-category link is structurally confirmed, this
    says "this hospital has financial ties to firms that make THIS device
    family" — which genuinely changes the risk posture.

    Values (require mfr_cat_match=True, otherwise return 0):
        total_payments >= $1M  → +0.30
        >= $100k               → +0.20
        >= $10k                → +0.10
        >0 but below           → +0.05
    """
    if not mfr_cat_match or not op_agg:
        return 0.0
    total = float(op_agg.get("total_payments") or 0)
    if total >= 1_000_000:
        return 0.30
    if total >= 100_000:
        return 0.20
    if total >= 10_000:
        return 0.10
    if total > 0:
        return 0.05
    return 0.0


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
              complications: dict = None,
              leapfrog: dict = None,
              mfr_quality: dict = None,
              sparcs: dict = None,
              ca_tavr: dict = None,
              pa_phc4: dict = None,
              news: dict = None,
              mfr_to_cats: dict = None,
              op_mfrs_by_ccn: dict = None) -> dict:
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
    leapfrog_ccn = (leapfrog or {}).get(ccn) or {}
    mfr_q    = (mfr_quality or {}).get(cat_slug) or {}
    sparcs_rows = (sparcs or {}).get((ccn, cat_slug)) or []
    # CA TAVR only applies when this row's device_category is cardiac_valve
    ca_tavr_row = None
    if cat_slug == "cardiac_valve":
        ca_tavr_row = (ca_tavr or {}).get(ccn)
    # PA PHC4 — only the rows whose condition maps into this device_category
    pa_phc4_rows = []
    for entry in (pa_phc4 or {}).get(ccn, []):
        mapped = _PHC4_COND_TO_CAT.get(entry.get("condition"))
        if mapped == cat_slug:
            pa_phc4_rows.append(entry)
    news_rows = (news or {}).get(ccn, [])
    # Structural manufacturer→device_category overlap:
    # Does this CCN's Open-Payments manufacturer list include any firm that
    # makes devices in this row's device_category (via fda_510k.applicant)?
    mfr_cat_match = False
    mfr_matched_firms: list[str] = []
    if mfr_to_cats and op_mfrs_by_ccn:
        for mk in op_mfrs_by_ccn.get(ccn, []):
            cats_for_mk = mfr_to_cats.get(mk)
            if cats_for_mk and cat_slug in cats_for_mk:
                mfr_cat_match = True
                mfr_matched_firms.append(mk)

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
    hosp_link_conf, linkage_multiplier = _linkage_confidence(
        exposure_discharges, comps, op_agg,
    )

    # Manufacturer bonus — only meaningful when the structural mfr→category
    # match is present (user complaint #3: Open Payments was floating signal).
    manufacturer_bonus = _manufacturer_bonus(op_agg, mfr_cat_match)

    hospital_final_score = round(
        min(global_score * linkage_multiplier + exposure_bonus + manufacturer_bonus,
            10.0), 2,
    )

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
                # Our ClinicalTrials.gov buckets are coarser than our
                # device_category taxonomy (e.g. "pacemaker" bucket covers
                # pacemaker_dual_chamber, pacemaker_lead, etc.). This flag
                # tells consumers the match is broad rather than device-exact.
                "clinical_match_precision": "broad",
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
            "manufacturer_bonus":  manufacturer_bonus,
            "manufacturer_category_match": mfr_cat_match,
            "hospital_final_score":    hospital_final_score,

            # What the score would be IF we assumed full linkage — so the UI
            # can still rank hospitals within a category by pure volume.
            "exposure_adjusted_score": exposure_adjusted,
        },

        # Trim catalog: drop the noisy zero-event entries that bloat the
        # payload (most product codes have many 510k clearances, only a few
        # have MAUDE events). Sort by event_count desc, decision_date desc,
        # cap at 8. Kept _shown count separately so a UI can still say
        # "showing 8 of 47 device clearances".
        "device_510k_catalog": (lambda c: {
            "count_total":  len(c),
            "count_shown":  min(8, len([d for d in c if (d.get('maude') or {}).get('event_count', 0) > 0]) or len(c)),
            "devices": (
                # First: any with MAUDE events (most clinically relevant)
                [{**d, "maude": d.get("maude")} for d in c
                 if (d.get('maude') or {}).get('event_count', 0) > 0][:8]
                # Then: fill up to 8 from the most-recent zero-event ones
                or c[:8]
            ),
        })(catalog),
        # Scraped / non-API signals (populated from TinyFish + CMS catalog).
        "external_signals": {
            # Leapfrog Hospital Safety Grade, A–F.  Joins on (name,state)
            # via exact + pg_trgm fuzzy match, linked back to CCN.
            "leapfrog": leapfrog_ccn or None,
            # Manufacturer-level quality issues for this device category.
            # FDA Warning Letters + 483 inspections joined to the 510k
            # applicants whose product_codes sit in this device_category.
            "manufacturer_quality": {
                "oai_inspections":  mfr_q.get("oai_inspections") or 0,
                "flagged_firms":    mfr_q.get("flagged_firms") or [],
            } if mfr_q else None,
            # NY SPARCS Potentially Preventable Complications for this
            # (CCN × device_category) — structurally joined via the
            # sparcs_ppc_to_device_category curated map and the strict
            # PFI↔CCN crosswalk. Only populated for NY hospitals.
            "state_complications": {
                "source":   "NY SPARCS" if sparcs_rows else None,
                "ppc_rows": sparcs_rows,
            } if sparcs_rows else None,
            # Device-specific state registry data. CA HCAI publishes TAVR
            # outcomes per hospital — this is hospital × device × mortality
            # at the cleanest public level we've found.
            "ca_tavr_outcome": ca_tavr_row,
            # PA PHC4 hospital performance — CABG/AMI condition-level outcomes
            # mapped into device_category via _PHC4_COND_TO_CAT.
            "pa_phc4_outcomes": pa_phc4_rows or None,
            # Hospital-level news headlines (lawsuit, adverse event press,
            # closure/layoff signals). Scraped via TinyFish per CCN.
            "news": news_rows or None,
        },
        "meta": {
            # Honest framing: we never have point-of-care UDI capture, so
            # even "strong" DRG+payments+measures evidence is still inference,
            # not device-level ground truth. Label it as such.
            "linkage_type":        "multi_signal_inference",
            "has_procedure_evidence": exposure_discharges > 0,
            # Explicit step-by-step chain showing HOW the hospital is joined
            # to the device. Each step lists the source table, the join key
            # used, and the matched value — so a reviewer can spot-check the
            # link without reading the UI.
            "connection_chain": [
                {
                    "step": 1,
                    "what": "hospital",
                    "source": "cms_hospitals (CMS Hospital General Information)",
                    "key": "facility_id (CCN)",
                    "value": hospital["facility_id"],
                    "label": hospital.get("facility_name"),
                },
                {
                    "step": 2,
                    "what": "procedure_volume",
                    "source": "cms_hospital_drg_volume (CMS Medicare Inpatient PUF)",
                    "key": "ccn",
                    "value": hospital["facility_id"],
                    "matched": {
                        "discharges_2024": exposure_discharges,
                        "drg_codes_billed": list(volume.get("drg_codes") or []),
                    } if volume else {"matched": False},
                },
                {
                    "step": 3,
                    "what": "device_category",
                    "source": "drg_to_device_category (curated map) + bridge_hcpcs_to_product_code",
                    "key": "drg_code → device_category",
                    "value": cat_slug,
                    "matched": {
                        "product_codes": list(category.get("product_codes") or []),
                        "hcpcs_codes":   list(category.get("hcpcs_codes") or []),
                        "drg_codes":     list(category.get("drg_codes") or []),
                    },
                },
                {
                    "step": 4,
                    "what": "device_safety",
                    "source": "fda_maude_events + fda_recalls",
                    "key": "product_code",
                    "value": category.get("product_code"),
                    "matched": {
                        "adverse_events": int(events.get("total_events") or 0),
                        "deaths":         int(events.get("death_count") or 0),
                        "active_recalls": int(rec.get("recall_count") or 0),
                    },
                },
                {
                    "step": 5,
                    "what": "manufacturer_overlap",
                    "source": "cms_open_payments → manufacturer_alias → fda_510k.applicant",
                    "key": "manufacturer_norm → parent_name → product_code → device_category",
                    "value": cat_slug,
                    "matched": {
                        "op_manufacturer_count": int(op_agg.get("manufacturer_count") or 0),
                        "op_total_payments":     float(op_agg.get("total_payments") or 0.0),
                        "device_category_match": bool(mfr_cat_match),
                        "matched_firm_keys":     mfr_matched_firms[:5],
                    },
                },
                {
                    "step": 6,
                    "what": "score",
                    "source": "computed",
                    "formula": "min(global × linkage_multiplier + exposure_bonus + manufacturer_bonus, 10)",
                    "value": hospital_final_score,
                    "components": {
                        "global_score":        global_score,
                        "linkage_multiplier":  linkage_multiplier,
                        "exposure_bonus":      exposure_bonus,
                        "manufacturer_bonus":  manufacturer_bonus,
                    },
                },
            ],
            # Time alignment — readers need to know which year each layer
            # represents so they can weight a "2018 MAUDE death" against a
            # "2024 claim" appropriately.
            "time_alignment": {
                "claims_year":         volume.get("data_year"),
                "maude_window":        "2019-2024",
                "recalls_window":      "2019-2024",
                "open_payments_year":  2024,
                "leapfrog_window":     "2024-2025",
                "sparcs_window":       "2013-2023",
                "ca_tavr_year":        2024,
                "trials_window":       "rolling (no end-date filter)",
            },
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
                "Leapfrog Hospital Safety Grade (TinyFish)",
                "FDA 483 Inspection Classifications (TinyFish)",
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
        "linkage_type":       "multi_signal_inference",
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
        leapfrog       = fetch_leapfrog_by_ccn(conn)
        mfr_quality    = fetch_manufacturer_quality_by_category(conn)
        sparcs         = fetch_sparcs_ppc_by_ccn(conn)
        ca_tavr        = fetch_ca_tavr_by_ccn(conn)
        pa_phc4        = fetch_pa_phc4_by_ccn(conn)
        news           = fetch_news_by_ccn(conn)
        mfr_to_cats    = fetch_manufacturer_to_categories(conn)
        op_mfrs_by_ccn = fetch_op_manufacturers_by_ccn(conn)
        log.info("mfr→cat map: %d firms | OP mfrs by ccn: %d ccns",
                 len(mfr_to_cats), len(op_mfrs_by_ccn))
        log.info("aggregates: %d categories | maude=%d recalls=%d trials=%d "
                 "reg_class=%d 510k_catalog=%d K#_with_events=%d op_ccns=%d "
                 "direct_volume_links=%d complication_links=%d "
                 "leapfrog_ccns=%d mfr_quality_cats=%d",
                 len(categories), len(maude), len(recalls), len(trials),
                 len(reg_class), len(catalog_510k), len(maude_by_k), len(op),
                 len(device_volume), len(complications),
                 len(leapfrog), len(mfr_quality))

        hospitals = fetch_hospitals(conn, limit=limit_hospitals, state=state, ccn=ccn)
        log.info("assembling for %d hospitals × %d categories = %d rows",
                 len(hospitals), len(categories), len(hospitals) * len(categories))

        batch: list[dict] = []
        for h in hospitals:
            for cat in categories:
                batch.append(build_row(h, cat, maude, recalls, trials, op,
                                         reg_class, catalog_510k,
                                         device_volume=device_volume,
                                         complications=complications,
                                         leapfrog=leapfrog,
                                         mfr_quality=mfr_quality,
                                         sparcs=sparcs,
                                         ca_tavr=ca_tavr,
                                         pa_phc4=pa_phc4,
                                         news=news,
                                         mfr_to_cats=mfr_to_cats,
                                         op_mfrs_by_ccn=op_mfrs_by_ccn))
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
