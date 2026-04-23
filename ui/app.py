"""CMS Hospital Data Explorer — FastAPI + plain HTML (ClickHouse backend).

Run:
    uvicorn ui.app:app --reload --port 8000
Then open http://localhost:8000

ClickHouse dialect notes vs. the Postgres version this replaced:
  - ReplacingMergeTree tables queried with FINAL to collapse duplicates
  - `raw->>'key'`          -> JSONExtractString(raw, 'key')
  - `FILTER (WHERE cond)`  -> countIf / sumIf
  - `ILIKE 'foo%'`         -> ILIKE (ClickHouse 20+ supports it natively)
  - `regexp_replace(x,r,'','g')` -> replaceRegexpAll(x, r, '')
  - `::numeric`            -> toFloat64OrNull(...)
  - `information_schema.tables` -> system.tables
  - `%(name)s` params      -> `{name:Type}` with explicit type annotation
"""

import os
import sys
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import db  # noqa: E402
from src.measure_dict import (  # noqa: E402
    decode_measure, decode_dataset, risk_level, MEASURES, DATASETS,
)


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

TEMPLATES.env.globals["decode_measure"] = decode_measure
TEMPLATES.env.globals["decode_dataset"] = decode_dataset
TEMPLATES.env.globals["risk_level"] = risk_level
TEMPLATES.env.filters["json_safe"] = lambda rows: [[
    float(v) if hasattr(v, "as_tuple") else
    (v.isoformat() if hasattr(v, "isoformat") else v)
    for v in r] for r in rows]

app = FastAPI(title="CMS Hospital Data Explorer")


# --------------------------- db helpers ---------------------------

def q(sql, parameters=None):
    """Run a ClickHouse query, return (columns, rows) as lists."""
    with db.connect() as conn:
        res = conn.query(sql, parameters=parameters or {})
    return list(res.column_names), list(res.result_rows)


def q_one(sql, parameters=None):
    cols, rows = q(sql, parameters)
    return dict(zip(cols, rows[0])) if rows else {}


def _json_safe(v):
    from decimal import Decimal
    from datetime import date, datetime, time
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime, time)):
        return v.isoformat()
    if isinstance(v, tuple):
        return [_json_safe(x) for x in v]
    if isinstance(v, list):
        return [_json_safe(x) for x in v]
    return v


def json_safe_rows(rows):
    return [[_json_safe(v) for v in row] for row in rows]


# --------------------------- routes ---------------------------

@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    _, hero = q(
        """
        SELECT
          (SELECT count() FROM cms_hospitals FINAL)                                     AS hospitals,
          (SELECT count() FROM cms_hospital_measures FINAL)                             AS measures,
          (SELECT count() FROM fda_maude_events FINAL)                                  AS fda_events,
          (SELECT uniqExact(manufacturer) FROM fda_maude_devices FINAL)                 AS manufacturers,
          (SELECT count() FROM bridge_product_code_to_measure FINAL)                    AS bridges,
          (SELECT uniqExact(d.report_number)
             FROM fda_maude_devices AS d FINAL
             INNER JOIN bridge_product_code_to_measure AS b FINAL
               ON d.product_code = b.product_code)                                      AS linked_events
        """
    )
    hero = hero[0] if hero else (0, 0, 0, 0, 0, 0)

    _, ratings = q(
        "SELECT coalesce(overall_rating, 'NA') AS rating, count() AS n "
        "FROM cms_hospitals FINAL GROUP BY rating ORDER BY rating"
    )
    _, types = q(
        "SELECT hospital_type, count() AS n FROM cms_hospitals FINAL "
        "WHERE hospital_type IS NOT NULL GROUP BY hospital_type ORDER BY n DESC"
    )
    _, by_state = q(
        "SELECT state, count() AS hospitals FROM cms_hospitals FINAL "
        "WHERE state IS NOT NULL GROUP BY state ORDER BY hospitals DESC LIMIT 15"
    )
    _, worst_err = q(
        """
        SELECT h.facility_name, h.state, m.score_num AS err, m.facility_id
        FROM cms_hospital_measures AS m FINAL
        INNER JOIN cms_hospitals AS h FINAL ON m.facility_id = h.facility_id
        WHERE m.dataset_id='9n3s-kdb3' AND m.measure_id='READM-30-HIP-KNEE-HRRP'
          AND m.score_num IS NOT NULL
        ORDER BY m.score_num DESC LIMIT 10
        """
    )
    _, fda_types = q(
        "SELECT coalesce(event_type,'Unknown') AS et, count() "
        "FROM fda_maude_events FINAL GROUP BY et ORDER BY count() DESC"
    )
    _, top_linked = q(
        """
        SELECT b.device_category, b.cms_measure_id, count() AS events
        FROM fda_maude_devices AS d FINAL
        INNER JOIN bridge_product_code_to_measure AS b FINAL
          ON d.product_code = b.product_code
        GROUP BY b.device_category, b.cms_measure_id
        ORDER BY events DESC LIMIT 10
        """
    )
    _, runs = q(
        """
        SELECT dataset_id, status, rows_upserted,
               toDateTime(started_at) AS started
        FROM cms_ingestion_log ORDER BY started_at DESC LIMIT 8
        """
    )
    return TEMPLATES.TemplateResponse(
        "overview.html",
        {
            "request": request, "active": "overview",
            "hero": hero, "ratings": ratings, "types": types,
            "by_state": by_state, "worst_err": worst_err,
            "fda_types": fda_types, "top_linked": top_linked, "runs": runs,
        },
    )


@app.get("/hospitals", response_class=HTMLResponse)
def hospitals(
    request: Request,
    state: str = "",
    hospital_type: str = "",
    rating: str = "",
    search: str = "",
    facility_id: str = "",
):
    _, states = q(
        "SELECT DISTINCT state FROM cms_hospitals FINAL "
        "WHERE state IS NOT NULL ORDER BY state"
    )
    _, type_rows = q(
        "SELECT DISTINCT hospital_type FROM cms_hospitals FINAL "
        "WHERE hospital_type IS NOT NULL ORDER BY hospital_type"
    )

    where, params, types_map = [], {}, {}
    if state:
        where.append("state = {state:String}")
        params["state"] = state
    if hospital_type:
        where.append("hospital_type = {ht:String}")
        params["ht"] = hospital_type
    if rating:
        where.append("toString(overall_rating) >= {r:String}")
        params["r"] = rating
    if search:
        where.append("facility_name ILIKE {s:String}")
        params["s"] = f"%{search}%"
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    cols, rows = q(
        f"""
        SELECT facility_id, facility_name, city, state, hospital_type,
               hospital_ownership, overall_rating, emergency_services
        FROM cms_hospitals FINAL {where_sql}
        ORDER BY state, facility_name LIMIT 300
        """,
        params,
    )

    measures_cols, measures = [], []
    hospital_info = {}
    risk_summary = {}
    top_worse = []
    cjr_info = {}
    domain_stats = []
    if facility_id:
        hospital_info = q_one(
            "SELECT facility_id, facility_name, city, state, zip_code, "
            "hospital_type, hospital_ownership, overall_rating "
            "FROM cms_hospitals FINAL WHERE facility_id = {f:String}",
            {"f": facility_id},
        )

        risk_summary = q_one(
            """
            SELECT
              countIf(compared_to_national ILIKE 'worse%')              AS worse_count,
              countIf(compared_to_national ILIKE 'better%')             AS better_count,
              countIf(compared_to_national ILIKE 'no different%'
                   OR compared_to_national ILIKE 'same%')               AS same_count,
              countIf(score IS NULL OR score ILIKE 'not available%')    AS not_available,
              count()                                                   AS total
            FROM cms_hospital_measures FINAL
            WHERE facility_id = {f:String}
            """,
            {"f": facility_id},
        )

        _, domain_stats = q(
            """
            SELECT dataset_id,
                   countIf(compared_to_national ILIKE 'better%')        AS better_n,
                   countIf(compared_to_national ILIKE 'no different%'
                        OR compared_to_national ILIKE 'same%')          AS same_n,
                   countIf(compared_to_national ILIKE 'worse%')         AS worse_n,
                   count()                                               AS total_n
            FROM cms_hospital_measures FINAL
            WHERE facility_id = {f:String} AND compared_to_national IS NOT NULL
              AND compared_to_national != ''
            GROUP BY dataset_id
            """,
            {"f": facility_id},
        )

        _, top_worse = q(
            """
            SELECT dataset_id, measure_id, score_num, compared_to_national
            FROM cms_hospital_measures FINAL
            WHERE facility_id = {f:String} AND score_num IS NOT NULL
              AND (
                compared_to_national ILIKE 'worse%'
                OR (score_num > 1.0 AND dataset_id IN ('9n3s-kdb3','77hc-ibv8'))
              )
            ORDER BY score_num DESC LIMIT 10
            """,
            {"f": facility_id},
        )

        cjr_info = q_one(
            """
            SELECT JSONExtractString(raw, 'comphipknee') AS comp_hip_knee,
                   JSONExtractString(raw, 'hcahps_hlmr') AS hcahps_hlmr,
                   JSONExtractString(raw, 'msa_title')   AS msa
            FROM cms_wide_facility_snapshots FINAL
            WHERE dataset_id='tqkv-mgxq' AND facility_id={f:String}
            LIMIT 1
            """,
            {"f": facility_id},
        )

        measures_cols, measures = q(
            """
            SELECT dataset_id, measure_id, score, score_num,
                   compared_to_national, start_date, end_date, footnote
            FROM cms_hospital_measures FINAL
            WHERE facility_id = {f:String}
            ORDER BY dataset_id, measure_id
            """,
            {"f": facility_id},
        )

    return TEMPLATES.TemplateResponse(
        "hospitals.html",
        {
            "request": request,
            "active": "hospitals",
            "states": [r[0] for r in states],
            "types": [r[0] for r in type_rows],
            "state": state, "hospital_type": hospital_type, "rating": rating,
            "search": search, "facility_id": facility_id,
            "cols": cols, "rows": rows,
            "hospital_info": hospital_info,
            "risk_summary": risk_summary,
            "top_worse": top_worse,
            "cjr_info": cjr_info,
            "domain_stats": domain_stats,
            "measures_cols": measures_cols, "measures": measures,
        },
    )


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(request: Request, tab: str = "err", measure: str = ""):
    cols, rows = [], []
    measures_list = []
    subtitle = ""

    if tab == "err":
        subtitle = "Hospitals by Excess Readmission Ratio (ERR > 1.0 = worse than national)"
        _, m = q(
            "SELECT DISTINCT measure_id FROM cms_hospital_measures FINAL "
            "WHERE dataset_id='9n3s-kdb3' AND score_num IS NOT NULL ORDER BY measure_id"
        )
        measures_list = [r[0] for r in m]
        if not measure and measures_list:
            measure = measures_list[0]
        if measure:
            cols, rows = q(
                """
                SELECT m.facility_id, h.facility_name, h.state, h.overall_rating,
                       m.score_num AS err,
                       JSONExtractString(m.raw, 'number_of_discharges')   AS discharges,
                       JSONExtractString(m.raw, 'number_of_readmissions') AS readmits
                FROM cms_hospital_measures AS m FINAL
                INNER JOIN cms_hospitals AS h FINAL ON m.facility_id = h.facility_id
                WHERE m.dataset_id='9n3s-kdb3' AND m.measure_id={m:String}
                  AND m.score_num IS NOT NULL
                ORDER BY m.score_num DESC LIMIT 50
                """,
                {"m": measure},
            )

    elif tab == "hai":
        subtitle = ("Hospital-Acquired Infection SIR (SIR > 1.0 = more infections than expected). "
                    "HAI_1 = CLABSI, HAI_2 = CAUTI, HAI_5 = MRSA.")
        _, m = q(
            "SELECT DISTINCT measure_id FROM cms_hospital_measures FINAL "
            "WHERE dataset_id='77hc-ibv8' AND measure_id LIKE {pat:String} "
            "AND score_num IS NOT NULL ORDER BY measure_id",
            {"pat": "HAI_%_SIR"},
        )
        measures_list = [r[0] for r in m]
        if not measure and measures_list:
            measure = measures_list[0]
        if measure:
            cols, rows = q(
                """
                SELECT m.facility_id, h.facility_name, h.state, m.score_num AS sir,
                       m.compared_to_national
                FROM cms_hospital_measures AS m FINAL
                INNER JOIN cms_hospitals AS h FINAL ON m.facility_id = h.facility_id
                WHERE m.dataset_id='77hc-ibv8' AND m.measure_id={m:String}
                  AND m.score_num IS NOT NULL
                ORDER BY m.score_num DESC LIMIT 50
                """,
                {"m": measure},
            )

    elif tab == "psi":
        subtitle = "Patient Safety Indicator complication rates"
        _, m = q(
            "SELECT DISTINCT measure_id FROM cms_hospital_measures FINAL "
            "WHERE dataset_id='ynj2-r877' AND measure_id LIKE {pat:String} "
            "AND score_num IS NOT NULL ORDER BY measure_id",
            {"pat": "PSI_%"},
        )
        measures_list = [r[0] for r in m]
        if not measure and measures_list:
            measure = measures_list[0]
        if measure:
            cols, rows = q(
                """
                SELECT m.facility_id, h.facility_name, h.state, m.score_num AS rate,
                       m.lower_estimate_num AS ci_lo, m.higher_estimate_num AS ci_hi,
                       m.compared_to_national
                FROM cms_hospital_measures AS m FINAL
                INNER JOIN cms_hospitals AS h FINAL ON m.facility_id = h.facility_id
                WHERE m.dataset_id='ynj2-r877' AND m.measure_id={m:String}
                  AND m.score_num IS NOT NULL
                ORDER BY m.score_num DESC LIMIT 50
                """,
                {"m": measure},
            )

    elif tab == "cjr":
        subtitle = "Comprehensive Care for Joint Replacement — direct hip/knee device outcomes"
        cols, rows = q(
            r"""
            SELECT facility_id, facility_name, state,
                   toFloat64OrNull(replaceRegexpAll(JSONExtractString(raw, 'comphipknee'), '[^0-9.\-]', '')) AS comp_hip_knee,
                   toFloat64OrNull(replaceRegexpAll(JSONExtractString(raw, 'hcahps_hlmr'), '[^0-9.\-]', '')) AS hcahps_hlmr,
                   JSONExtractString(raw, 'msa_title') AS msa
            FROM cms_wide_facility_snapshots FINAL
            WHERE dataset_id='tqkv-mgxq'
              AND toFloat64OrNull(replaceRegexpAll(JSONExtractString(raw, 'comphipknee'), '[^0-9.\-]', '')) IS NOT NULL
            ORDER BY comp_hip_knee DESC LIMIT 100
            """
        )

    return TEMPLATES.TemplateResponse(
        "leaderboard.html",
        {
            "request": request, "active": "leaderboard",
            "tab": tab, "measure": measure, "measures_list": measures_list,
            "subtitle": subtitle, "cols": cols, "rows": rows,
        },
    )


@app.get("/browser", response_class=HTMLResponse)
def browser(request: Request, dataset_id: str = ""):
    _, runs = q(
        """
        SELECT dataset_id, any(dataset_name) AS dataset_name,
               toDateTime(max(finished_at)) AS last_run
        FROM cms_ingestion_log WHERE status='success'
        GROUP BY dataset_id ORDER BY dataset_id
        """
    )

    sections = []
    if dataset_id:
        for table, label in [
            ("cms_hospital_measures", "facility measures"),
            ("cms_state_measures", "state measures"),
            ("cms_national_measures", "national measures"),
            ("cms_wide_facility_snapshots", "wide-format snapshots"),
        ]:
            try:
                n = q(
                    f"SELECT count() FROM {table} FINAL WHERE dataset_id={{d:String}}",
                    {"d": dataset_id},
                )[1][0][0]
            except Exception:
                n = 0
            if n:
                cols, rows = q(
                    f"SELECT * FROM {table} FINAL WHERE dataset_id={{d:String}} LIMIT 100",
                    {"d": dataset_id},
                )
                sections.append({"label": label, "count": n, "cols": cols, "rows": rows})

        if dataset_id == "y9us-9xdf":
            cols, rows = q(
                "SELECT * FROM cms_footnote_crosswalk FINAL "
                "ORDER BY length(footnote), footnote"
            )
            sections.append({"label": "footnote crosswalk", "count": len(rows), "cols": cols, "rows": rows})

    return TEMPLATES.TemplateResponse(
        "browser.html",
        {
            "request": request, "active": "browser",
            "datasets": runs, "dataset_id": dataset_id, "sections": sections,
        },
    )


@app.get("/codes", response_class=HTMLResponse)
def codes(request: Request, search: str = "", family: str = "", device_only: str = ""):
    _, exists = q(
        "SELECT count() FROM system.tables "
        "WHERE database = currentDatabase() AND name = 'hcpcs_master'"
    )
    if not exists or not exists[0][0]:
        return TEMPLATES.TemplateResponse(
            "codes.html",
            {
                "request": request, "active": "codes",
                "kpi": (0, 0, 0, 0), "family_breakdown": [],
                "search": "", "family": "", "device_only": "",
                "cols": [], "rows": [], "bridged": [],
            },
        )

    _, kpi = q(
        """
        SELECT
          (SELECT count() FROM hcpcs_master FINAL)                                  AS total,
          (SELECT count() FROM hcpcs_master FINAL WHERE is_device = 1)              AS devices,
          (SELECT count() FROM hcpcs_master FINAL WHERE betos_code IS NOT NULL)     AS betos_coded,
          (SELECT uniqExact(code_family) FROM hcpcs_master FINAL
            WHERE code_family IS NOT NULL)                                           AS families
        """
    )
    kpi = kpi[0] if kpi else (0, 0, 0, 0)

    _, family_breakdown = q(
        """
        SELECT code_family,
               count()                              AS n,
               countIf(is_device = 1)               AS devices,
               anyIf(short_desc, code_family = 'A') AS sample
        FROM hcpcs_master FINAL
        WHERE code_family IS NOT NULL
        GROUP BY code_family ORDER BY code_family
        """
    )

    where, params = [], {}
    if search:
        where.append(
            "(hcpcs_code ILIKE {s:String} OR short_desc ILIKE {s:String} "
            "OR long_desc ILIKE {s:String})"
        )
        params["s"] = f"%{search}%"
    if family:
        where.append("code_family = {f:String}")
        params["f"] = family
    if device_only:
        where.append("is_device = 1")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    cols, rows = q(
        f"""
        SELECT hcpcs_code, short_desc, long_desc, betos_code, code_family, is_device,
               coverage_code, asc_payment_grp
        FROM hcpcs_master FINAL
        {where_sql}
        ORDER BY hcpcs_code LIMIT 200
        """,
        params,
    )

    _, bridged = q(
        """
        SELECT h.hcpcs_code, h.short_desc
        FROM hcpcs_master AS h FINAL
        INNER JOIN bridge_product_code_to_measure AS b FINAL
          ON b.product_code = h.hcpcs_code
        LIMIT 20
        """
    )

    return TEMPLATES.TemplateResponse(
        "codes.html",
        {
            "request": request, "active": "codes",
            "kpi": kpi, "family_breakdown": family_breakdown,
            "search": search, "family": family, "device_only": device_only,
            "cols": cols, "rows": rows, "bridged": bridged,
        },
    )


@app.get("/utilization", response_class=HTMLResponse)
def utilization(request: Request, dataset_id: str = "", ccn: str = "", npi: str = ""):
    _, dataset_summary = q(
        """
        SELECT dataset_id,
               count()                     AS rows,
               uniqExact(ccn)              AS hospitals,
               uniqExact(npi)              AS providers,
               uniqExact(drg_code)         AS drg_codes,
               uniqExact(hcpcs_code)       AS hcpcs_codes,
               min(year) AS first_year,
               max(year) AS last_year
        FROM cms_provider_summary
        GROUP BY dataset_id ORDER BY dataset_id
        """
    )

    if not dataset_id and dataset_summary:
        dataset_id = dataset_summary[0][0]

    where, params = ["dataset_id = {d:String}"], {"d": dataset_id}
    if ccn:
        where.append("ccn = {c:String}"); params["c"] = ccn
    if npi:
        where.append("npi = {n:String}"); params["n"] = npi
    where_sql = "WHERE " + " AND ".join(where)

    _, top_drg = q(
        f"""
        SELECT drg_code, any(drg_description) AS drg_description,
               count() AS rows,
               sum(total_services) AS services, sum(total_beneficiaries) AS benes
        FROM cms_provider_summary
        {where_sql} AND drg_code IS NOT NULL
        GROUP BY drg_code ORDER BY services DESC NULLS LAST LIMIT 15
        """,
        params,
    )
    _, top_hcpcs = q(
        f"""
        SELECT hcpcs_code, count() AS rows,
               sum(total_services) AS services, sum(total_beneficiaries) AS benes
        FROM cms_provider_summary
        {where_sql} AND hcpcs_code IS NOT NULL
        GROUP BY hcpcs_code ORDER BY services DESC NULLS LAST LIMIT 15
        """,
        params,
    )
    _, top_ccn = q(
        f"""
        SELECT ccn, any(provider_name) AS provider_name, any(provider_state) AS provider_state,
               count() AS rows, sum(total_services) AS services
        FROM cms_provider_summary
        {where_sql} AND ccn IS NOT NULL
        GROUP BY ccn ORDER BY services DESC NULLS LAST LIMIT 15
        """,
        params,
    )

    sample_cols, sample_rows = q(
        f"""
        SELECT dataset_id, year, ccn, npi, drg_code, drg_description,
               hcpcs_code, provider_name, provider_state,
               total_services, total_beneficiaries, total_payment_amt
        FROM cms_provider_summary
        {where_sql} LIMIT 50
        """,
        params,
    )

    return TEMPLATES.TemplateResponse(
        "utilization.html",
        {
            "request": request, "active": "utilization",
            "dataset_summary": dataset_summary,
            "dataset_id": dataset_id, "ccn": ccn, "npi": npi,
            "top_drg": top_drg, "top_hcpcs": top_hcpcs, "top_ccn": top_ccn,
            "sample_cols": sample_cols, "sample_rows": sample_rows,
        },
    )


@app.get("/fda", response_class=HTMLResponse)
def fda(request: Request, product_code: str = "", brand: str = "", event_type: str = ""):
    _, kpi = q(
        """
        SELECT
          (SELECT count() FROM fda_maude_events FINAL)                                                   AS events,
          (SELECT count() FROM fda_maude_devices FINAL)                                                  AS devices,
          (SELECT count() FROM fda_maude_devices FINAL WHERE udi_di IS NOT NULL)                         AS with_udi,
          (SELECT uniqExact(product_code) FROM fda_maude_devices FINAL WHERE product_code IS NOT NULL)   AS distinct_product_codes,
          (SELECT uniqExact(manufacturer) FROM fda_maude_devices FINAL WHERE manufacturer IS NOT NULL)   AS distinct_manufacturers
        """
    )
    kpi = kpi[0] if kpi else (0, 0, 0, 0, 0)

    _, event_breakdown = q(
        "SELECT event_type, count() FROM fda_maude_events FINAL "
        "GROUP BY event_type ORDER BY count() DESC"
    )
    _, top_pc = q(
        """
        SELECT d.product_code, b.device_category, count() AS events
        FROM fda_maude_devices AS d FINAL
        LEFT JOIN bridge_product_code_to_measure AS b FINAL
          ON d.product_code = b.product_code
        WHERE d.product_code IS NOT NULL
        GROUP BY d.product_code, b.device_category ORDER BY events DESC LIMIT 20
        """
    )
    _, top_mfr = q(
        "SELECT manufacturer, count() FROM fda_maude_devices FINAL "
        "WHERE manufacturer IS NOT NULL GROUP BY manufacturer "
        "ORDER BY count() DESC LIMIT 15"
    )

    where, params = [], {}
    if product_code:
        where.append("d.product_code = {pc:String}"); params["pc"] = product_code
    if brand:
        where.append("d.brand_name ILIKE {b:String}"); params["b"] = f"%{brand}%"
    if event_type:
        where.append("e.event_type = {et:String}"); params["et"] = event_type
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    events_cols, events_rows = q(
        f"""
        SELECT e.report_number, e.event_type, e.date_received,
               d.product_code, d.brand_name, d.manufacturer,
               d.model_number, d.udi_di, e.patient_outcomes
        FROM fda_maude_events AS e FINAL
        LEFT JOIN fda_maude_devices AS d FINAL ON d.report_number = e.report_number
        {where_sql}
        ORDER BY e.date_received DESC NULLS LAST
        LIMIT 100
        """,
        params,
    )

    return TEMPLATES.TemplateResponse(
        "fda.html",
        {
            "request": request, "active": "fda",
            "kpi": kpi,
            "event_breakdown": event_breakdown,
            "top_pc": top_pc, "top_mfr": top_mfr,
            "events_cols": events_cols, "events_rows": events_rows,
            "product_code": product_code, "brand": brand, "event_type": event_type,
        },
    )


@app.get("/linkage-live", response_class=HTMLResponse)
def linkage_live(request: Request, cms_measure: str = ""):
    _, summary = q(
        """
        SELECT uniqExact(d.report_number)   AS linked_events,
               uniqExact(d.product_code)    AS linked_product_codes,
               uniqExact(b.cms_measure_id)  AS cms_measures_hit,
               (SELECT count() FROM fda_maude_events FINAL)                AS total_maude_events,
               (SELECT count() FROM bridge_product_code_to_measure FINAL)  AS bridge_rows
        FROM fda_maude_devices AS d FINAL
        INNER JOIN bridge_product_code_to_measure AS b FINAL
          ON d.product_code = b.product_code
        """
    )
    summary = summary[0] if summary else (0, 0, 0, 0, 0)

    _, by_category = q(
        """
        SELECT b.device_category, b.cms_measure_id, count() AS maude_events
        FROM fda_maude_devices AS d FINAL
        INNER JOIN bridge_product_code_to_measure AS b FINAL
          ON d.product_code = b.product_code
        GROUP BY b.device_category, b.cms_measure_id ORDER BY maude_events DESC
        """
    )

    _, mlist = q(
        "SELECT DISTINCT cms_measure_id FROM bridge_product_code_to_measure FINAL "
        "ORDER BY cms_measure_id"
    )
    measures_list = [r[0] for r in mlist]

    detail_rows = []
    if cms_measure:
        _, detail_rows = q(
            """
            SELECT b.device_category, d.product_code, d.brand_name, d.manufacturer,
                   count() AS events,
                   countIf(e.event_type = 'Death')  AS deaths,
                   countIf(e.event_type = 'Injury') AS injuries
            FROM fda_maude_devices AS d FINAL
            INNER JOIN bridge_product_code_to_measure AS b FINAL
              ON d.product_code = b.product_code
            INNER JOIN fda_maude_events AS e FINAL
              ON d.report_number = e.report_number
            WHERE b.cms_measure_id = {m:String}
            GROUP BY b.device_category, d.product_code, d.brand_name, d.manufacturer
            ORDER BY events DESC LIMIT 50
            """,
            {"m": cms_measure},
        )

    return TEMPLATES.TemplateResponse(
        "linkage_live.html",
        {
            "request": request, "active": "linkage-live",
            "summary": summary, "by_category": by_category,
            "measures_list": measures_list, "cms_measure": cms_measure,
            "detail_rows": detail_rows,
        },
    )


@app.get("/glossary", response_class=HTMLResponse)
def glossary(request: Request):
    from collections import defaultdict
    by_domain = defaultdict(list)
    for mid, entry in MEASURES.items():
        name, domain, desc, direction, threshold = entry
        by_domain[domain].append({
            "id": mid, "name": name, "description": desc,
            "direction": direction, "threshold": threshold,
        })
    return TEMPLATES.TemplateResponse(
        "glossary.html",
        {
            "request": request, "active": "glossary",
            "by_domain": dict(by_domain), "datasets": DATASETS,
        },
    )


@app.get("/linkage", response_class=HTMLResponse)
def linkage(request: Request):
    _, asc_count_row = q(
        "SELECT count() FROM cms_wide_facility_snapshots FINAL "
        "WHERE dataset_id='4jcv-atw7' AND npi IS NOT NULL"
    )
    asc_cols, asc = q(
        """
        SELECT facility_id AS ccn, npi, facility_name, state, zip_code
        FROM cms_wide_facility_snapshots FINAL
        WHERE dataset_id='4jcv-atw7' AND npi IS NOT NULL
        ORDER BY state, facility_name LIMIT 200
        """
    )
    measures_cols, measures = q(
        """
        SELECT dataset_id, measure_id, count() AS hospitals_reporting
        FROM cms_hospital_measures FINAL
        WHERE measure_id IS NOT NULL
        GROUP BY dataset_id, measure_id ORDER BY dataset_id, measure_id LIMIT 300
        """
    )
    zip_cols, zips = q(
        """
        SELECT state, zip_code, count() AS hospitals
        FROM cms_hospitals FINAL
        WHERE zip_code IS NOT NULL GROUP BY state, zip_code
        ORDER BY hospitals DESC LIMIT 50
        """
    )
    return TEMPLATES.TemplateResponse(
        "linkage.html",
        {
            "request": request, "active": "linkage",
            "asc_total": asc_count_row[0][0],
            "asc_cols": asc_cols, "asc": asc,
            "measures_cols": measures_cols, "measures": measures,
            "zip_cols": zip_cols, "zips": zips,
        },
    )
