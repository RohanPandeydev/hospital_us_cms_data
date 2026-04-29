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

_CH_CLIENT = None


def _ch():
    """Module-level ClickHouse client. db.connect() opens a fresh HTTP client
    per call — each one runs `SELECT version(), timezone()` on init, adding
    ~3s round-trip overhead. With 10 queries per page that becomes 30s+. The
    HttpClient is safe to reuse across threads, so we hold one per process."""
    global _CH_CLIENT
    if _CH_CLIENT is None:
        from src import config
        import clickhouse_connect
        _CH_CLIENT = clickhouse_connect.get_client(
            host=config.CLICKHOUSE_HOST, port=config.CLICKHOUSE_PORT,
            username=config.CLICKHOUSE_USER, password=config.CLICKHOUSE_PASSWORD,
            database=config.CLICKHOUSE_DATABASE, secure=config.CLICKHOUSE_SECURE,
            connect_timeout=15, send_receive_timeout=120,
        )
    return _CH_CLIENT


def q(sql, parameters=None):
    """Run a ClickHouse query, return (columns, rows) as lists."""
    res = _ch().query(sql, parameters=parameters or {})
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


# --------------------------- analytics helpers ---------------------------

def _compute_cusum_series(monthly_rows, k_mul: float = 0.5, h_mul: float = 5.0,
                          min_baseline: int = 6):
    """One-sided upper CUSUM on a monthly count series.

    monthly_rows: list of (date, count). Output:
      ts: list of (iso_date, count, cusum_stat, alarm_flag)
      alarms: list of iso_dates where the alarm threshold was crossed.

    The doc's `cms_complication_velocity` feature flags the moment a device's
    adverse-event volume rises above its own baseline. We use the classic
    Page CUSUM:
        S_t = max(0, S_{t-1} + (x_t - mu_0 - k))
        alarm when S_t > h
    with mu_0 from the *median* of the bottom 60% of months (robust to
    outliers and to gaps in the data — important because MAUDE has known
    coverage holes that would inflate the mean otherwise). sigma is the
    MAD-based estimate (median-abs-deviation × 1.4826 ≈ σ).

    If we have <min_baseline non-zero months the data is too sparse to
    detect anything reliably — we return the series with no alarms.
    """
    if not monthly_rows:
        return [], []
    counts = [(r[0], int(r[1] or 0)) for r in monthly_rows]
    nonzero = [c for _, c in counts if c > 0]
    if len(nonzero) < min_baseline:
        return [
            ((d.isoformat() if hasattr(d, "isoformat") else str(d)), c, 0.0, False)
            for d, c in counts
        ], []

    # Robust baseline: median of bottom 60% of nonzero months.
    sorted_nz = sorted(nonzero)
    cutoff = max(1, int(len(sorted_nz) * 0.6))
    baseline_pool = sorted_nz[:cutoff]
    mu = sum(baseline_pool) / len(baseline_pool)
    # MAD-based sigma — robust to spikes
    med = sorted_nz[len(sorted_nz) // 2]
    mad = sorted([abs(v - med) for v in sorted_nz])[len(sorted_nz) // 2]
    sigma = max(1.0, mad * 1.4826)
    k = k_mul * sigma
    h = h_mul * sigma

    out, alarms = [], []
    s = 0.0
    for d, c in counts:
        s = max(0.0, s + (c - mu - k))
        alarm = s > h
        if alarm:
            alarms.append(d.isoformat() if hasattr(d, "isoformat") else str(d))
        out.append((d.isoformat() if hasattr(d, "isoformat") else str(d),
                    c, round(s, 1), alarm))
    return out, alarms


def _build_verdict(m_12: int, m_prev: int, yoy_pct, r_24: int, r_lifetime: int,
                   asof_date=None):
    """One-sentence plain-English read on a product code's risk posture.

    The numbers on the page are dense (rates, CUSUM, classes, recall counts).
    A single readable sentence at the top — what the average person would say
    after looking at this for 60 seconds — is what the user kept asking for
    when they said "data is not understandable". Returns dict with `level`
    (high/med/low/none) and `text`.
    """
    if m_12 == 0 and r_lifetime == 0:
        return {"level": "none",
                "text": "No recent MAUDE activity and no recall history. "
                        "Either a quiet device or one we haven't ingested data for yet."}

    parts = []
    if m_prev > 0 and yoy_pct is not None:
        if yoy_pct >= 50:
            parts.append(f"MAUDE volume rose <strong>{yoy_pct:+.0f}%</strong> YoY ({m_12:,} vs {m_prev:,}) — a rising-failure signal.")
        elif yoy_pct >= 10:
            parts.append(f"MAUDE volume up {yoy_pct:+.0f}% YoY ({m_12:,} vs {m_prev:,}) — modest upward trend.")
        elif yoy_pct <= -10:
            parts.append(f"MAUDE volume down {yoy_pct:+.0f}% YoY ({m_12:,} vs {m_prev:,}) — declining failure reports.")
        else:
            parts.append(f"MAUDE volume stable YoY ({m_12:,} vs {m_prev:,}, {yoy_pct:+.0f}%).")
    elif m_12 > 0:
        parts.append(f"<strong>{m_12:,}</strong> MAUDE events in the last 12 months (no prior-year baseline yet).")

    if r_24 > 0:
        parts.append(f"<strong>{r_24}</strong> FDA recall(s) in the last 24 months.")
    elif r_lifetime > 0:
        parts.append(f"{r_lifetime} historical recall(s) but none in the last 24 months.")

    # Decide level
    if (yoy_pct is not None and yoy_pct >= 50) and r_24 > 0:
        level = "high"  # both signals firing — doc's combined uplift case
    elif (yoy_pct is not None and yoy_pct >= 50) or r_24 > 0:
        level = "med"
    elif (yoy_pct is not None and yoy_pct >= 10):
        level = "med"
    else:
        level = "low"

    if asof_date is not None:
        asof_str = asof_date.isoformat() if hasattr(asof_date, "isoformat") else str(asof_date)
        parts.append(f'<span style="color:#888;">(MAUDE through {asof_str})</span>')

    return {"level": level, "text": " ".join(parts)}


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


# =====================================================================
# Risk Intelligence (Postgres-backed — cms_hospitals DB via src.risk_db)
# =====================================================================
# This block queries the unified `hospital_device_risk_intelligence` table
# (now in ClickHouse — was Postgres-only). _pg_rows is kept as the call-site
# name so existing routes work; SQL is auto-translated PG → CH.

import re as _re

_PG_TO_CH_RE = [
    # COUNT(*) FILTER (WHERE expr) → countIf(expr)
    (_re.compile(r"COUNT\s*\(\s*\*\s*\)\s*FILTER\s*\(\s*WHERE\s+(.+?)\s*\)", _re.IGNORECASE | _re.DOTALL),
     r"countIf(\1)"),
    # ::numeric / ::float casts → drop (CH infers)
    (_re.compile(r"::\s*numeric", _re.IGNORECASE), ""),
    (_re.compile(r"::\s*float", _re.IGNORECASE), ""),
    # NULLS LAST / NULLS FIRST clauses → drop (CH default)
    (_re.compile(r"\s+NULLS\s+(?:FIRST|LAST)", _re.IGNORECASE), ""),
    # IS TRUE / IS FALSE → = 1 / = 0 (CH bools are UInt8)
    (_re.compile(r"\bIS\s+TRUE\b", _re.IGNORECASE), "= 1"),
    (_re.compile(r"\bIS\s+FALSE\b", _re.IGNORECASE), "= 0"),
    (_re.compile(r"\bIS\s+NOT\s+TRUE\b", _re.IGNORECASE), "!= 1"),
    (_re.compile(r"\bIS\s+NOT\s+FALSE\b", _re.IGNORECASE), "!= 0"),
]


def _quote_ch(v):
    """Render a Python value as a ClickHouse SQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'"


def _pg_rows(sql, params=None):
    """Run a query against ClickHouse, accepting psycopg-style %s placeholders.

    Auto-translates Postgres-only syntax (FILTER, NULLS LAST, IS TRUE, ::cast)
    to the ClickHouse equivalent so call sites don't need to change.
    """
    for pat, repl in _PG_TO_CH_RE:
        sql = pat.sub(repl, sql)
    if params:
        params_iter = iter(params)
        sql = _re.sub(r"%s", lambda _: _quote_ch(next(params_iter)), sql)
    with db.connect() as conn:
        res = conn.query(sql)
    cols = list(res.column_names)
    rows = [[_json_safe(v) for v in r] for r in res.result_rows]
    return cols, rows


@app.get("/crosswalk", response_class=HTMLResponse)
def crosswalk(request: Request, device_category: str = "", code_type: str = "",
              search: str = ""):
    """Unified code crosswalk browser — filter across every code type."""
    cats_cols, cats_rows = _pg_rows(
        "SELECT device_category, COUNT(*) FROM device_code_crosswalk "
        "GROUP BY 1 ORDER BY 1"
    )
    types_cols, types_rows = _pg_rows(
        "SELECT code_type, COUNT(*) FROM device_code_crosswalk "
        "GROUP BY 1 ORDER BY 2 DESC"
    )

    where = []
    params: list = []
    if device_category:
        where.append("device_category = %s")
        params.append(device_category)
    if code_type:
        where.append("code_type = %s")
        params.append(code_type)
    if search:
        where.append("(code_value ILIKE %s OR display_name ILIKE %s "
                     "OR linked_brand ILIKE %s OR linked_manufacturer ILIKE %s)")
        for _ in range(4):
            params.append(f"%{search}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    cols, rows = _pg_rows(
        f"""SELECT device_category, code_type, code_value, display_name,
                   source, confidence, linked_brand, linked_manufacturer,
                   linked_product_code
              FROM device_code_crosswalk
              {where_sql}
              ORDER BY CASE code_type
                        WHEN 'hcpcs' THEN 1 WHEN 'cpt' THEN 2 WHEN 'drg' THEN 3
                        WHEN 'product_code' THEN 4 WHEN 'k_number' THEN 5
                        WHEN 'udi_di' THEN 6 WHEN 'brand' THEN 7
                        WHEN 'manufacturer' THEN 8 WHEN 'gmdn' THEN 9 END,
                       code_value
              LIMIT 500""",
        params,
    )
    kpi_cols, kpi_rows = _pg_rows(
        """SELECT COUNT(*) AS total, COUNT(DISTINCT device_category) AS cats,
                   COUNT(DISTINCT code_type) AS types,
                   COUNT(DISTINCT code_value) AS codes
             FROM device_code_crosswalk"""
    )
    kpi = dict(zip(kpi_cols, kpi_rows[0])) if kpi_rows else {}

    return TEMPLATES.TemplateResponse(
        "crosswalk.html",
        {
            "request": request, "active": "crosswalk",
            "categories": [r[0] for r in cats_rows],
            "type_counts": types_rows,
            "cols": cols, "rows": rows, "kpi": kpi,
            "filters": {"device_category": device_category,
                         "code_type": code_type, "search": search},
        },
    )


@app.get("/devices", response_class=HTMLResponse)
def devices(request: Request, sort: str = "trend", device_class: str = ""):
    """Device-centric recall-prediction view.

    Joins Part B procedure volume × MAUDE adverse-event count × Recall history
    via bridge_hcpcs_to_product_code → produces the doc's
    `complaint_rate_per_1k_procedures` feature, ranked by risk.
    """
    sort_sql = {
        "rate":     "rate_per_1k DESC NULLS LAST",
        "vol":      "proc_volume DESC",
        "maude":    "maude_12mo DESC",
        "recall":   "recall_24mo DESC",
        "trend":    "maude_pct_change DESC NULLS LAST",
    }.get(sort, "maude_12mo DESC")

    # Numerator = MAUDE events in last 12 months (matches denominator window:
    # one year of Part B procedures). Lifetime MAUDE / one-year procedures
    # was producing nonsense rates like 7,000 / 1k — denominator scope
    # already partial (Part B Physician misses inpatient/facility billing),
    # so window-mismatch on top of that was making the column meaningless.
    #
    # Anchor windows on max(date_received), not today(): there's typically a
    # multi-month gap between "now" and the latest ingested MAUDE event, so
    # today()-anchored windows compare partial-recent vs full-prior and
    # spuriously show declines.
    cols, rows = q(
        f"""
        WITH (SELECT max(date_received) FROM fda_maude_events FINAL) AS asof,
        pb_vol AS (
          SELECT b.product_code,
                 anyLast(b.device_category) AS device_category,
                 sum(pb.total_services)     AS proc_volume,
                 sum(pb.total_beneficiaries) AS bene_count,
                 count(distinct pb.npi)     AS providers
          FROM cms_provider_summary pb
          INNER JOIN bridge_hcpcs_to_product_code b ON pb.hcpcs_code = b.hcpcs_code
          WHERE pb.dataset_id = 'medicare_physician_by_provider_service'
          GROUP BY b.product_code
        ),
        m_recent AS (
          SELECT d.product_code,
                 countIf(e.date_received >  asof - INTERVAL 12 MONTH AND e.date_received <= asof) AS maude_12mo,
                 countIf(e.date_received >  asof - INTERVAL 24 MONTH
                          AND e.date_received <= asof - INTERVAL 12 MONTH)                       AS maude_prev_12mo,
                 count()                                                                         AS maude_lifetime,
                 max(e.date_received)                                                            AS maude_latest
          FROM fda_maude_devices d FINAL
          LEFT JOIN fda_maude_events e FINAL ON d.report_number = e.report_number
          WHERE d.product_code IS NOT NULL
          GROUP BY d.product_code
        ),
        r AS (
          SELECT product_code,
                 countIf(recall_initiation_date >= today() - INTERVAL 24 MONTH) AS recall_24mo,
                 count()                                                        AS recall_lifetime,
                 max(recall_initiation_date)                                    AS recall_latest
          FROM fda_recalls GROUP BY product_code
        ),
        c AS (SELECT product_code, anyLast(device_name) AS device_name,
                     anyLast(device_class) AS device_class
              FROM fda_device_classification GROUP BY product_code)
        SELECT v.product_code,
               v.device_category,
               c.device_name,
               c.device_class,
               v.proc_volume,
               v.bene_count,
               v.providers,
               ifNull(m.maude_12mo, 0)              AS maude_12mo,
               ifNull(m.maude_prev_12mo, 0)         AS maude_prev_12mo,
               ifNull(m.maude_lifetime, 0)          AS maude_lifetime,
               m.maude_latest                       AS maude_latest,
               ifNull(r.recall_24mo, 0)             AS recall_24mo,
               ifNull(r.recall_lifetime, 0)         AS recall_lifetime,
               r.recall_latest                      AS recall_latest,
               -- Apples-to-apples 12-month rate: MAUDE 12mo ÷ annual procedures × 1000
               round(ifNull(m.maude_12mo, 0) * 1000.0 / nullIf(v.proc_volume, 0), 3) AS rate_per_1k,
               -- YoY change in MAUDE volume — proxies the doc's complication-velocity feature
               round((ifNull(m.maude_12mo, 0) - ifNull(m.maude_prev_12mo, 0)) * 100.0
                     / nullIf(m.maude_prev_12mo, 0), 1) AS maude_pct_change
        FROM pb_vol v
        LEFT JOIN m_recent m ON v.product_code = m.product_code
        LEFT JOIN r         ON v.product_code = r.product_code
        LEFT JOIN c         ON v.product_code = c.product_code
        WHERE v.proc_volume >= 100  -- drop noise: PCs with <100 Part B services aren't comparable
          AND ({{cls:String}} = '' OR c.device_class = {{cls:String}})
        ORDER BY {sort_sql}
        """,
        {"cls": device_class},
    )

    _, kpi = q(
        """
        SELECT
          (SELECT count(distinct product_code) FROM bridge_hcpcs_to_product_code) AS bridge_pcs,
          (SELECT count(distinct hcpcs_code)   FROM bridge_hcpcs_to_product_code) AS bridge_codes,
          (SELECT count() FROM cms_provider_summary
             WHERE dataset_id='medicare_physician_by_provider_service')           AS partb_rows,
          (SELECT count() FROM fda_maude_devices FINAL)                           AS maude_devs,
          (SELECT count() FROM fda_recalls)                                       AS recalls
        """
    )
    kpi = kpi[0] if kpi else (0, 0, 0, 0, 0)

    # Bucket the rows by FDA risk class so the template can render three
    # separate sections (Class III shown first — highest risk, most relevant
    # for the predictor's recall-attribution use case).
    by_class = {"3": [], "2": [], "1": [], "other": []}
    for r in rows:
        cls = (r[3] or "").strip() if len(r) > 3 else ""
        if cls in ("3", "III"):
            by_class["3"].append(r)
        elif cls in ("2", "II"):
            by_class["2"].append(r)
        elif cls in ("1", "I"):
            by_class["1"].append(r)
        else:
            by_class["other"].append(r)

    return TEMPLATES.TemplateResponse(
        "devices.html",
        {
            "request": request, "active": "devices",
            "cols": cols, "rows": rows,
            "rows_by_class": by_class,
            "kpi": kpi, "sort": sort,
            "device_class": device_class,
        },
    )


@app.get("/devices/{product_code}", response_class=HTMLResponse)
def device_detail(request: Request, product_code: str):
    """Per-product-code detail page: time-series, manufacturers, recalls, hospitals."""
    pc = product_code.upper()
    _tick = lambda *_: None  # no-op; left in for easy re-instrumentation
    _t0 = None

    # Header — FDA device classification
    header = q_one(
        "SELECT product_code, device_name, device_class, medical_specialty_description, "
        "regulation_number, implant_flag, life_sustain_support_flag "
        "FROM fda_device_classification WHERE product_code = {pc:String} LIMIT 1",
        {"pc": pc},
    )
    _t0 = _tick("header", _t0)

    # Linked HCPCS via bridge
    _, hcpcs_rows = q(
        "SELECT hcpcs_code, device_category, confidence FROM bridge_hcpcs_to_product_code "
        "WHERE product_code = {pc:String} ORDER BY confidence DESC, hcpcs_code",
        {"pc": pc},
    )

    # KPIs. Use FINAL on fda_maude_devices because each (report_number, seq) is
    # keyed on a synthesized id and ReplacingMergeTree leaves duplicates until merged.
    _, kpi = q(
        """
        SELECT
          (SELECT count() FROM fda_maude_devices FINAL WHERE product_code = {pc:String})  AS maude,
          (SELECT count() FROM fda_recalls       WHERE product_code = {pc:String})        AS recalls,
          (SELECT uniqExact(manufacturer)
             FROM fda_maude_devices FINAL WHERE product_code = {pc:String})               AS distinct_mfrs,
          (SELECT count() FROM fda_510k          WHERE product_code = {pc:String})        AS k_numbers,
          (SELECT sum(pb.total_services)
             FROM cms_provider_summary pb
             INNER JOIN bridge_hcpcs_to_product_code b ON pb.hcpcs_code = b.hcpcs_code
            WHERE b.product_code = {pc:String}
              AND pb.dataset_id  = 'medicare_physician_by_provider_service')              AS proc_volume
        """,
        {"pc": pc},
    )
    kpi = kpi[0] if kpi else (0, 0, 0, 0, 0)
    _t0 = _tick("kpi", _t0)

    # Trend metrics for the plain-English verdict at the top of the page.
    # Anchor the 12-month window on the latest MAUDE date for this PC, NOT
    # today() — there's typically a multi-month lag between "now" and the
    # latest ingested MAUDE event, so today()-anchored windows compare
    # partial-year-recent to full-year-prior and falsely show declines.
    _, trend = q(
        """
        WITH (SELECT max(e.date_received)
                FROM fda_maude_devices d FINAL
                JOIN fda_maude_events  e FINAL ON d.report_number = e.report_number
               WHERE d.product_code = {pc:String}) AS asof
        SELECT
          countIf(e.date_received >  asof - INTERVAL 12 MONTH AND e.date_received <= asof) AS m_12,
          countIf(e.date_received >  asof - INTERVAL 24 MONTH
                   AND e.date_received <= asof - INTERVAL 12 MONTH)                       AS m_prev,
          (SELECT countIf(recall_initiation_date >= today() - INTERVAL 24 MONTH)
             FROM fda_recalls WHERE product_code = {pc:String})                           AS r_24,
          asof                                                                            AS asof_date
        FROM fda_maude_devices d FINAL
        LEFT JOIN fda_maude_events e FINAL ON d.report_number = e.report_number
        WHERE d.product_code = {pc:String}
        """,
        {"pc": pc},
    )
    trend = trend[0] if trend else (0, 0, 0, None)
    m_12, m_prev, r_24 = int(trend[0] or 0), int(trend[1] or 0), int(trend[2] or 0)
    asof_date = trend[3]
    yoy_pct = round((m_12 - m_prev) * 100.0 / m_prev, 1) if m_prev > 0 else None
    verdict = _build_verdict(m_12, m_prev, yoy_pct, r_24, kpi[1] or 0, asof_date)
    _t0 = _tick("trend+verdict", _t0)

    # Time-series: MAUDE events by month for the last 5 yrs. Monthly cadence
    # gives the CUSUM detector enough resolution to flag a 60-90 day pre-recall
    # rise; quarterly was too coarse.
    _, ts_raw = q(
        """
        SELECT toStartOfMonth(e.date_received) AS month, count() AS events
        FROM fda_maude_devices d FINAL
        LEFT JOIN fda_maude_events e FINAL ON d.report_number = e.report_number
        WHERE d.product_code = {pc:String}
          AND e.date_received IS NOT NULL
          AND e.date_received >= today() - INTERVAL 60 MONTH
        GROUP BY month ORDER BY month
        """,
        {"pc": pc},
    )
    ts_rows, cusum_alarms = _compute_cusum_series(ts_raw)
    _t0 = _tick("ts+cusum", _t0)

    # Recall dates as date markers on the time-series chart — visual proof
    # of the doc's claim that MAUDE rate-rises lead recalls by 60-90 days.
    _, recall_marks = q(
        """
        SELECT toStartOfMonth(recall_initiation_date) AS month,
               recall_class, count() AS n
        FROM fda_recalls
        WHERE product_code = {pc:String}
          AND recall_initiation_date IS NOT NULL
          AND recall_initiation_date >= today() - INTERVAL 60 MONTH
        GROUP BY month, recall_class ORDER BY month
        """,
        {"pc": pc},
    )

    _t0 = _tick("recall_marks", _t0)

    # Top manufacturers seen in MAUDE for this product_code
    _, mfr_rows = q(
        "SELECT manufacturer, count() AS events FROM fda_maude_devices FINAL "
        "WHERE product_code = {pc:String} AND manufacturer IS NOT NULL "
        "GROUP BY manufacturer ORDER BY events DESC LIMIT 15",
        {"pc": pc},
    )
    _t0 = _tick("mfr", _t0)

    # 510(k) applicants (also potential manufacturers)
    _, k_rows = q(
        "SELECT applicant, k_number, decision_date, decision_description "
        "FROM fda_510k WHERE product_code = {pc:String} "
        "ORDER BY decision_date DESC LIMIT 15",
        {"pc": pc},
    )

    # Recall list (no MAUDE annotation yet — we'll join in Python below).
    _, recall_base = q(
        """
        SELECT recall_number, recall_class, firm_name,
               product_description, reason_for_recall, recall_initiation_date
        FROM fda_recalls
        WHERE product_code = {pc:String}
        ORDER BY recall_initiation_date DESC NULLS LAST
        LIMIT 30
        """,
        {"pc": pc},
    )

    # Daily MAUDE counts for this PC over the last ~5 yrs — fetched once,
    # then used in Python to compute per-recall 90d-pre and 90-180d-pre
    # windows. Avoids 30 × 2 = 60 correlated FINAL subqueries on a 800K+
    # row table (which was making this page take 30+s).
    _, maude_daily = q(
        """
        SELECT toDate(e.date_received) AS d, count() AS n
        FROM fda_maude_devices d FINAL
        LEFT JOIN fda_maude_events e FINAL ON d.report_number = e.report_number
        WHERE d.product_code = {pc:String}
          AND e.date_received IS NOT NULL
          AND e.date_received >= today() - INTERVAL 8 YEAR
        GROUP BY d
        """,
        {"pc": pc},
    )
    daily_map = {row[0]: int(row[1]) for row in maude_daily}
    _t0 = _tick("recalls+daily_map", _t0)

    def _count_window(end_date, start_offset_days, end_offset_days):
        """Sum MAUDE events in [end_date - start_offset, end_date - end_offset)."""
        if not end_date:
            return 0
        from datetime import timedelta
        total = 0
        for delta in range(end_offset_days, start_offset_days):
            d = end_date - timedelta(days=delta)
            total += daily_map.get(d, 0)
        return total

    recall_rows = []
    for r in recall_base:
        d = r[5]
        m_pre_90 = _count_window(d, 90, 0)        # last 90 days before recall
        m_pre_180_90 = _count_window(d, 180, 90)  # 90-180 days before recall
        recall_rows.append((r[0], r[1], r[2], r[3], r[4], r[5], m_pre_90, m_pre_180_90))

    # Top hospitals billing this device-family (via Part B → bridge)
    _, hosp_rows = q(
        """
        SELECT pb.provider_name, pb.provider_state, pb.provider_city,
               sum(pb.total_services) AS svcs,
               sum(pb.total_beneficiaries) AS benes
        FROM cms_provider_summary pb
        INNER JOIN bridge_hcpcs_to_product_code b ON pb.hcpcs_code = b.hcpcs_code
        WHERE b.product_code = {pc:String}
          AND pb.dataset_id  = 'medicare_physician_by_provider_service'
        GROUP BY pb.provider_name, pb.provider_state, pb.provider_city
        ORDER BY svcs DESC LIMIT 20
        """,
        {"pc": pc},
    )
    _t0 = _tick("hosp_rows", _t0)

    # Inferential "likely affected hospitals" — the closest free approximation to
    # "which hospital had this adverse event". MAUDE strips the reporting hospital
    # by FDA design, so we cannot answer that question directly. Instead:
    # (a) bridge_product_code_to_measure tells us which CMS Hospital Compare
    #     measure is most relevant to this product code (e.g. DXY pacemakers →
    #     MORT_30_HF, the 30-day heart-failure mortality rate), and
    # (b) cms_hospital_measures has every facility's score on that measure.
    # We surface the worst-performing hospitals on the relevant outcome —
    # statistically the hospitals most likely to have had problems with this
    # device family, even though no single record proves causation.
    _, measure_link = q(
        "SELECT cms_measure_id, device_category, confidence "
        "FROM bridge_product_code_to_measure WHERE product_code = {pc:String} LIMIT 1",
        {"pc": pc},
    )
    measure_link = measure_link[0] if measure_link else None
    bad_hospitals = []
    if measure_link:
        measure_id = measure_link[0]
        _, bad_hospitals = q(
            """
            SELECT
              h.facility_name,
              h.state,
              h.city,
              m.score_num,
              m.compared_to_national,
              h.overall_rating
            FROM cms_hospital_measures m FINAL
            JOIN cms_hospitals h FINAL ON h.facility_id = m.facility_id
            WHERE m.measure_id = {mid:String}
              AND m.score_num IS NOT NULL
              AND h.facility_name IS NOT NULL
            ORDER BY m.score_num DESC
            LIMIT 15
            """,
            {"mid": measure_id},
        )
    _t0 = _tick("bad_hospitals", _t0)

    # FDA 483 inspections — match firms to known manufacturers of this product
    # code via manufacturer_to_product_code. We previously also tried matching
    # to top Part B hospitals but the per-row regex normalization on a 91K-row
    # CMS Part B table made this query slow enough to timeout for some PCs.
    # The 483 data is small (30 rows) — the manufacturer match alone is the
    # signal-bearing part.
    _, fda_483 = q(
        """
        SELECT i.firm_name, i.firm_city, i.firm_state, i.classification, i.inspection_end_date
        FROM fda_483_inspection i
        WHERE i.product_type = 'Devices'
          AND i.firm_name_norm IN (
            SELECT lower(replaceRegexpAll(applicant_name, '[^A-Za-z0-9]', ''))
            FROM manufacturer_to_product_code
            WHERE product_code = {pc:String}
          )
        ORDER BY i.inspection_end_date DESC
        LIMIT 10
        """,
        {"pc": pc},
    )
    _t0 = _tick("fda_483", _t0)

    # Massachusetts Serious Reportable Events — the only public US source we
    # have that ties a NAMED hospital to a device-related adverse event.
    # Public MAUDE strips the reporting hospital; MA DPH publishes annual
    # XLSX workbooks with every SRE per hospital. Coverage 2015-2022, all MA
    # acute-care + non-acute hospitals + ASCs. Data is NOT keyed to FDA
    # product codes — MA reports general categories ("Device misuse or
    # malfunction", "Retained foreign object") not specific devices. So this
    # panel shows ALL MA device events (not filtered to {pc}) — the closest
    # thing to "this hospital had a device adverse event" in the free public
    # data, even though it can't be tied to one MAUDE record.
    _, ma_sre = q(
        """
        SELECT hospital_name, ccn_match,
               sum(if(is_device_related=1, event_count, 0)) AS device_events,
               sum(event_count)                              AS all_events,
               max(report_year)                              AS latest_year
        FROM state_adverse_events
        WHERE state = 'MA'
        GROUP BY hospital_name, ccn_match
        HAVING device_events > 0 AND lower(hospital_name) != 'total'
        ORDER BY device_events DESC LIMIT 25
        """,
    )

    # Detail rows — every device-related SRE with hospital, year, what
    # event category and event type, and the count. This is what the user
    # gets when they ask "what actually happened" — each row is a real
    # filed adverse event from a named MA hospital.
    _, ma_sre_detail = q(
        """
        SELECT hospital_name, ccn_match, report_year,
               event_category, event_type, event_count
        FROM state_adverse_events
        WHERE state = 'MA'
          AND is_device_related = 1
          AND lower(hospital_name) != 'total'
          AND event_count > 0
        ORDER BY report_year DESC, event_count DESC, hospital_name
        LIMIT 200
        """,
    )

    rate_per_1k = (kpi[0] * 1000.0 / kpi[4]) if (kpi[4] and kpi[4] > 0) else None

    # Lightweight summary surfaced in the page header so the data is readable
    # without scrolling: are there active alarms? when was the last recall?
    last_alarm = cusum_alarms[-1] if cusum_alarms else None
    last_recall = None
    if recall_rows:
        # recall_rows[0] is most-recent (ORDER BY recall_initiation_date DESC)
        last_recall = recall_rows[0][5]
        if hasattr(last_recall, "isoformat"):
            last_recall = last_recall.isoformat()

    # Recall markers as ISO strings for the chart
    recall_marks_js = [
        [(r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])),
         r[1], int(r[2])]
        for r in recall_marks
    ]

    return TEMPLATES.TemplateResponse(
        "device_detail.html",
        {
            "request": request, "active": "devices",
            "pc": pc, "header": header,
            "hcpcs_rows": hcpcs_rows,
            "kpi": kpi, "rate_per_1k": rate_per_1k,
            "verdict": verdict,
            "m_12": m_12, "m_prev": m_prev, "yoy_pct": yoy_pct, "r_24": r_24,
            "ts_rows": ts_rows,
            "cusum_alarms": cusum_alarms,
            "last_alarm": last_alarm,
            "last_recall": last_recall,
            "recall_marks": recall_marks_js,
            "mfr_rows": mfr_rows, "k_rows": k_rows,
            "recall_rows": recall_rows, "hosp_rows": hosp_rows,
            "measure_link": measure_link,
            "bad_hospitals": bad_hospitals,
            "fda_483": fda_483,
            "ma_sre": ma_sre,
            "ma_sre_detail": ma_sre_detail,
        },
    )


@app.get("/manufacturers", response_class=HTMLResponse)
def manufacturers(request: Request, sort: str = "drop", min_2024: int = 100000):
    """Manufacturer payment-slope view.

    Per the doc: in 12-18 months before a major device recall, royalty/consulting
    payments to physicians drop 30-60%. We aggregate Open Payments (2022 / 2023
    / 2024) by manufacturer and surface YoY slope. Mfr name is normalized via
    manufacturer_alias to roll up subsidiaries; linked to product_code via
    manufacturer_to_product_code so a slope alert points to a device family.
    """
    sort_sql = {
        "drop":   "pct_23_24 ASC NULLS LAST",  # biggest declines first
        "rise":   "pct_23_24 DESC NULLS LAST",
        "amount": "y2024 DESC",
        "name":   "manufacturer_name ASC",
    }.get(sort, "pct_23_24 ASC NULLS LAST")

    cols, rows = q(
        f"""
        WITH norm AS (
          -- normalize mfr name: lowercase + strip non-alphanumeric
          SELECT manufacturer_name,
                 lower(replaceRegexpAll(manufacturer_name, '[^A-Za-z0-9]', '')) AS mfr_norm,
                 year, payment_total, physician_npi
          FROM cms_open_payments
          WHERE manufacturer_name IS NOT NULL AND year IN (2022, 2023, 2024)
        ),
        rolled AS (
          -- roll subsidiaries up to parent via manufacturer_alias
          SELECT n.manufacturer_name,
                 ifNull(a.parent_name, n.manufacturer_name) AS parent_name,
                 n.year, n.payment_total, n.physician_npi
          FROM norm n
          LEFT JOIN manufacturer_alias a ON n.mfr_norm = a.alias_norm
        ),
        yearly AS (
          SELECT parent_name, year,
                 sum(payment_total) AS total_$,
                 count() AS n_payments,
                 uniqExact(physician_npi) AS n_physicians
          FROM rolled
          GROUP BY parent_name, year
        ),
        pivot AS (
          SELECT parent_name AS manufacturer_name,
                 sumIf(total_$, year=2022) AS y2022,
                 sumIf(total_$, year=2023) AS y2023,
                 sumIf(total_$, year=2024) AS y2024,
                 sumIf(n_payments, year=2024) AS n_payments_2024,
                 sumIf(n_physicians, year=2024) AS n_physicians_2024
          FROM yearly GROUP BY parent_name
        ),
        with_pcs AS (
          SELECT m.*,
                 (SELECT groupArrayDistinct(product_code)
                    FROM manufacturer_to_product_code
                   WHERE lower(replaceRegexpAll(applicant_name, '[^A-Za-z0-9]', '')) =
                         lower(replaceRegexpAll(m.manufacturer_name, '[^A-Za-z0-9]', ''))
                  ) AS product_codes
          FROM pivot m
        )
        SELECT manufacturer_name,
               y2022, y2023, y2024,
               round((y2023 - y2022) * 100.0 / nullIf(y2022, 0), 1) AS pct_22_23,
               round((y2024 - y2023) * 100.0 / nullIf(y2023, 0), 1) AS pct_23_24,
               round((y2024 - y2022) * 100.0 / nullIf(y2022, 0), 1) AS pct_2y,
               n_payments_2024, n_physicians_2024,
               product_codes
        FROM with_pcs
        WHERE y2024 >= {{min_2024:UInt64}}
        ORDER BY {sort_sql}
        LIMIT 200
        """,
        {"min_2024": min_2024},
    )

    _, kpi = q(
        """
        SELECT
          (SELECT uniqExact(manufacturer_name) FROM cms_open_payments)         AS distinct_mfrs,
          (SELECT count() FROM manufacturer_alias)                              AS aliases,
          (SELECT count() FROM manufacturer_to_product_code)                    AS pc_links,
          (SELECT countIf(year=2022) FROM cms_open_payments)                    AS rows_2022,
          (SELECT countIf(year=2023) FROM cms_open_payments)                    AS rows_2023,
          (SELECT countIf(year=2024) FROM cms_open_payments)                    AS rows_2024
        """,
    )
    kpi = kpi[0] if kpi else (0, 0, 0, 0, 0, 0)

    return TEMPLATES.TemplateResponse(
        "manufacturers.html",
        {
            "request": request, "active": "manufacturers",
            "cols": cols, "rows": rows,
            "kpi": kpi, "sort": sort, "min_2024": min_2024,
        },
    )


@app.get("/risk", response_class=HTMLResponse)
def risk_intelligence(
    request: Request,
    state: str = "",
    device_category: str = "",
    min_score: str = "",
    has_recall: str = "",
    q_text: str = "",
    limit: int = 200,
):
    """Risk Intelligence leaderboard. Backed by hospital_device_risk_intelligence."""
    where = []
    params: list = []
    if state:
        where.append("state = %s")
        params.append(state)
    if device_category:
        where.append("device_category = %s")
        params.append(device_category)
    if has_recall == "1":
        where.append("has_recall IS TRUE")
    if min_score:
        try:
            params.append(float(min_score))
            where.append("final_score >= %s")
        except ValueError:
            pass
    if q_text:
        where.append("(hospital_name ILIKE %s OR ccn = %s)")
        params.append(f"%{q_text}%")
        params.append(q_text)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit_i = max(1, min(int(limit or 200), 2000))

    sql = f"""
        SELECT ccn, hospital_name, state, device_category, product_code,
               quality_score, total_events, death_count, injury_count,
               malfunction_count, has_recall, worst_recall_class,
               trial_count, manufacturer_exposure,
               maude_score, final_score,
               device_risk_confidence, hospital_device_link_confidence
        FROM hospital_device_risk_intelligence
        {where_sql}
        ORDER BY final_score DESC NULLS LAST, total_events DESC
        LIMIT {limit_i}
    """
    cols, rows = _pg_rows(sql, params)

    # Summary KPIs
    kpi_sql = """
        SELECT
          COUNT(*)                                         AS total_rows,
          COUNT(*) FILTER (WHERE has_recall IS TRUE)       AS with_recall,
          COUNT(*) FILTER (WHERE total_events > 0)         AS with_events,
          COUNT(*) FILTER (WHERE final_score >= 7)         AS high_risk,
          COUNT(*) FILTER (WHERE linkage_type = 'direct')  AS direct_linked,
          COUNT(*) FILTER (WHERE hospital_device_link_confidence IN ('strong','moderate')) AS evidenced,
          COUNT(DISTINCT ccn)                              AS hospitals,
          COUNT(DISTINCT device_category)                  AS categories,
          ROUND(AVG(final_score)::numeric, 2)              AS avg_score
        FROM hospital_device_risk_intelligence
    """
    kpi_cols, kpi_rows = _pg_rows(kpi_sql)
    kpi = dict(zip(kpi_cols, kpi_rows[0])) if kpi_rows else {}

    # Filter options
    cats_cols, cats_rows = _pg_rows(
        "SELECT device_category, COUNT(*) FROM hospital_device_risk_intelligence "
        "GROUP BY 1 ORDER BY 1"
    )
    states_cols, states_rows = _pg_rows(
        "SELECT state, COUNT(*) FROM hospital_device_risk_intelligence "
        "WHERE state IS NOT NULL GROUP BY 1 ORDER BY 1"
    )

    return TEMPLATES.TemplateResponse(
        "risk.html",
        {
            "request": request, "active": "risk",
            "cols": cols, "rows": rows,
            "kpi": kpi,
            "categories": [r[0] for r in cats_rows],
            "states":     [r[0] for r in states_rows],
            "filters": {
                "state": state, "device_category": device_category,
                "min_score": min_score, "has_recall": has_recall,
                "q_text": q_text, "limit": limit_i,
            },
        },
    )


@app.get("/risk/detail", response_class=HTMLResponse)
def risk_detail(request: Request, ccn: str, device_category: str):
    """One full unified-JSON record, pretty-rendered with section explainers."""
    import json as _json
    cols, rows = _pg_rows(
        "SELECT payload, final_score, hospital_name, state "
        "FROM hospital_device_risk_intelligence "
        "WHERE ccn = %s AND device_category = %s",
        [ccn, device_category],
    )
    if not rows:
        payload_obj = {}
        payload_str = "{}"
        final_score = None
        hospital_name = None
        state = None
    else:
        payload = rows[0][0]
        payload_obj = _json.loads(payload) if isinstance(payload, str) else payload
        payload_str = _json.dumps(payload_obj, indent=2, default=str)
        final_score = rows[0][1]
        hospital_name = rows[0][2]
        state = rows[0][3]

    # Pull match provenance for every recall tied to this device category so
    # the UI can show HOW each recall got linked (K# / PC / manufacturer / LLM).
    prov_cols, prov_rows = _pg_rows(
        """SELECT recall_number, recall_class, firm_name,
                   matched_k_number, matched_product_code,
                   match_method, match_confidence
           FROM fda_recalls
           WHERE device_category = %s
           ORDER BY CASE recall_class
                     WHEN 'Class I' THEN 1 WHEN 'Class II' THEN 2
                     WHEN 'Class III' THEN 3 ELSE 4 END,
                    match_method
           LIMIT 50""",
        [device_category],
    )
    provenance = [dict(zip(prov_cols, r)) for r in prov_rows]

    # Unified code crosswalk for this device category — every HCPCS, DRG,
    # product_code, K#, UDI, brand, manufacturer, GMDN we know about.
    cw_cols, cw_rows = _pg_rows(
        """SELECT code_type, code_value, display_name,
                   source, confidence, linked_brand, linked_manufacturer
           FROM device_code_crosswalk
           WHERE device_category = %s
           ORDER BY CASE code_type
                     WHEN 'hcpcs' THEN 1 WHEN 'cpt' THEN 2
                     WHEN 'drg' THEN 3  WHEN 'product_code' THEN 4
                     WHEN 'k_number' THEN 5 WHEN 'udi_di' THEN 6
                     WHEN 'brand' THEN 7 WHEN 'manufacturer' THEN 8
                     WHEN 'gmdn' THEN 9 ELSE 10 END,
                    code_value
           LIMIT 500""",
        [device_category],
    )
    crosswalk = [dict(zip(cw_cols, r)) for r in cw_rows]

    # Group the crosswalk by code_type for the UI card layout
    from collections import defaultdict
    crosswalk_by_type = defaultdict(list)
    for c in crosswalk:
        crosswalk_by_type[c["code_type"]].append(c)
    crosswalk_by_type = dict(crosswalk_by_type)

    # State-mandated adverse-event registries (currently MA SREs) — the only
    # public source that links a *named hospital* to a device-relevant event.
    # We surface every device-related SRE this hospital reported, regardless
    # of which device_category page is being viewed, because MA reports are
    # not coded to FDA product_code — the link is hospital-level, not
    # device-family-specific.
    sae_cols, sae_rows = _pg_rows(
        """SELECT report_year, facility_type, event_category, event_type,
                  is_device_related, sum(event_count) AS total_events,
                  any(source_url) AS source_url
             FROM state_adverse_events FINAL
            WHERE ccn_match = %s
            GROUP BY report_year, facility_type, event_category, event_type,
                     is_device_related
            ORDER BY report_year DESC, total_events DESC""",
        [ccn],
    )
    state_events = [dict(zip(sae_cols, r)) for r in sae_rows]
    state_events_device = [r for r in state_events if r.get("is_device_related")]

    # FDA device classification for this category's product_code(s)
    cls_cols, cls_rows = _pg_rows(
        """SELECT DISTINCT c.product_code, c.device_name, c.device_class,
                  c.regulation_number, c.medical_specialty_description,
                  c.implant_flag, c.life_sustain_support_flag
             FROM fda_device_classification c
             JOIN bridge_hcpcs_to_product_code b ON b.product_code = c.product_code
            WHERE b.device_category = %s
            ORDER BY c.product_code""",
        [device_category],
    )
    classification = [dict(zip(cls_cols, r)) for r in cls_rows]

    return TEMPLATES.TemplateResponse(
        "risk_detail.html",
        {
            "request": request, "active": "risk",
            "ccn": ccn, "device_category": device_category,
            "hospital_name": hospital_name, "state": state,
            "final_score": final_score,
            "payload": payload_obj,
            "payload_json": payload_str,
            "provenance": provenance,
            "crosswalk_by_type": crosswalk_by_type,
            "classification": classification,
            "state_events": state_events,
            "state_events_device": state_events_device,
        },
    )
