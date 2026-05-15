"""Hospital Value-Based Purchasing (HVBP) — Safety domain ingester.

Source: data.cms.gov Provider Data Catalog dataset 'Hospital Value-Based
Purchasing (HVBP) - Safety' (dgmq-aat3). FY2026 per-hospital performance
on 8 measures with achievement_threshold / benchmark / baseline_rate /
performance_rate / achievement_points / improvement_points / measure_score
for each of:

  HAI-1 CLABSI         (Central Line-Associated Blood Stream Infections)
  HAI-2 CAUTI          (Catheter-Associated Urinary Tract Infections)
  HAI-3 SSI Colon      (Surgical Site Infection)
  HAI-4 SSI Hyst       (Abdominal Hysterectomy SSI)
  HAI-5 MRSA           (MRSA bacteremia)
  HAI-6 C.diff         (Clostridium difficile)
  combined_ssi_measure_score
  SEP-1                (Sepsis bundle compliance)

These scores translate to actual Medicare payment penalties / incentives —
a hospital with low scores is being financially punished for unsafe care.
~2.5K rows.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import requests

from . import db


log = logging.getLogger(__name__)

DATASET_ID = "hvbp_safety"
DATASET_NAME = "Hospital Value-Based Purchasing (HVBP) - Safety"
CSV_URL = (
    "https://data.cms.gov/provider-data/sites/default/files/resources/"
    "33e26123c259ca779642b0d61b2e82f5_1777392336/hvbp_safety.csv"
)

MEASURES = ["HAI-1", "HAI-2", "HAI-3", "HAI-4", "HAI-5", "HAI-6", "SEP-1"]
NUMERIC_SUFFIXES = ["Achievement Threshold", "Benchmark", "Baseline Rate", "Performance Rate"]
TEXT_SUFFIXES = ["Achievement Points", "Improvement Points", "Measure Score"]


COLS = [
    "ccn", "fiscal_year", "facility_name", "address", "city", "state", "zip_code", "county",
    "hai1_achievement_threshold", "hai1_benchmark", "hai1_baseline_rate", "hai1_performance_rate",
    "hai1_achievement_points", "hai1_improvement_points", "hai1_measure_score",
    "hai2_achievement_threshold", "hai2_benchmark", "hai2_baseline_rate", "hai2_performance_rate",
    "hai2_achievement_points", "hai2_improvement_points", "hai2_measure_score",
    "combined_ssi_measure_score",
    "hai3_achievement_threshold", "hai3_benchmark", "hai3_baseline_rate", "hai3_performance_rate",
    "hai3_achievement_points", "hai3_improvement_points", "hai3_measure_score",
    "hai4_achievement_threshold", "hai4_benchmark", "hai4_baseline_rate", "hai4_performance_rate",
    "hai4_achievement_points", "hai4_improvement_points", "hai4_measure_score",
    "hai5_achievement_threshold", "hai5_benchmark", "hai5_baseline_rate", "hai5_performance_rate",
    "hai5_achievement_points", "hai5_improvement_points", "hai5_measure_score",
    "hai6_achievement_threshold", "hai6_benchmark", "hai6_baseline_rate", "hai6_performance_rate",
    "hai6_achievement_points", "hai6_improvement_points", "hai6_measure_score",
    "sep1_achievement_threshold", "sep1_benchmark", "sep1_baseline_rate", "sep1_performance_rate",
    "sep1_achievement_points", "sep1_improvement_points", "sep1_measure_score",
    "source_url", "raw",
]

COL_TYPES = [
    "String", "String", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)",
    "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "String",
]


def _num(v):
    if v in (None, "", "Not Available", "N/A", "NA"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _txt(v):
    if v in (None, "", "Not Available"):
        return None
    return str(v).strip() or None


def _measure_cols(rec: dict, measure_prefix: str) -> list:
    """Return [thresh, bench, baseline, perf, ach_pts, impr_pts, score]
    for one measure (HAI-1, HAI-2, etc) by CSV column convention."""
    return [
        _num(rec.get(f"{measure_prefix} Achievement Threshold")),
        _num(rec.get(f"{measure_prefix} Benchmark")),
        _num(rec.get(f"{measure_prefix} Baseline Rate")),
        _num(rec.get(f"{measure_prefix} Performance Rate")),
        _txt(rec.get(f"{measure_prefix} Achievement Points")),
        _txt(rec.get(f"{measure_prefix} Improvement Points")),
        _txt(rec.get(f"{measure_prefix} Measure Score")),
    ]


def _row_to_tuple(rec: dict) -> tuple | None:
    ccn = (rec.get("Facility ID") or "").strip()
    fy = (rec.get("Fiscal Year") or "").strip()
    if not ccn:
        return None
    base = [
        ccn, fy,
        rec.get("Facility Name") or None,
        rec.get("Address") or None,
        rec.get("City/Town") or None,
        rec.get("State") or None,
        rec.get("ZIP Code") or None,
        rec.get("County/Parish") or None,
    ]
    parts = []
    # HAI-1, HAI-2 (then combined_ssi_measure_score), HAI-3, HAI-4, HAI-5, HAI-6, SEP-1
    parts += _measure_cols(rec, "HAI-1")
    parts += _measure_cols(rec, "HAI-2")
    parts.append(_txt(rec.get("Combined SSI Measure Score")))
    parts += _measure_cols(rec, "HAI-3")
    parts += _measure_cols(rec, "HAI-4")
    parts += _measure_cols(rec, "HAI-5")
    parts += _measure_cols(rec, "HAI-6")
    parts += _measure_cols(rec, "SEP-1")
    return tuple(base + parts + [CSV_URL, json.dumps(rec, ensure_ascii=False)])


def ingest(batch_size: int = 2000) -> tuple[int, int]:
    log.info("Downloading %s", CSV_URL)
    r = requests.get(CSV_URL, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("Downloaded %d bytes", len(r.content))
    text = r.content.decode("utf-8-sig", errors="replace")
    rows: list[tuple] = []
    for rec in csv.DictReader(io.StringIO(text)):
        t = _row_to_tuple(rec)
        if t:
            rows.append(t)

    # Dedup by (ccn, fiscal_year)
    seen: dict[tuple, tuple] = {}
    for row in rows:
        seen[(row[0], row[1])] = row
    rows = list(seen.values())

    fetched = len(rows)
    upserted = 0
    status = "success"
    error: str | None = None
    with db.connect() as conn:
        handle = db.start_ingest_log(conn, {"id": DATASET_ID, "name": DATASET_NAME})
        try:
            for i in range(0, len(rows), batch_size):
                chunk = rows[i : i + batch_size]
                conn.insert("hospital_vbp_safety", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("HVBP Safety upserted %d rows", upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    f, u = ingest()
    print(f"Done. fetched={f}, upserted={u}")
