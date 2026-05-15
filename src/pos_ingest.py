"""Provider of Services (POS) file ingester.

Every Medicare-certified facility — hospitals, ASCs, SNFs, hospices, RHCs,
FQHCs, ESRD, home-health, CMHC, psych RTCs. Source CSV pulled from CMS DCAT.

Currently the "Provider of Services File - Quality Improvement and Evaluation
Survey (QIES)" dataset (~45K rows / 473 columns). We extract a sane subset
and stash the entire raw row as JSON for forensics.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import logging

import requests

from . import db

log = logging.getLogger(__name__)

DCAT_URL = "https://data.cms.gov/data.json"
# The "Hospital and other" CSV is the broadest single-file POS extract.
DATASET_HINTS = (
    "provider of services file - quality improvement",
    "hospital_and_other",
)
DATASET = {"id": "pos_file", "name": "Provider of Services File (QIES)"}

COLS = [
    "ccn", "facility_name", "facility_type", "provider_category_cd",
    "provider_subcategory_cd", "address", "city", "state", "zip_code",
    "county_name", "phone", "bed_count",
    "certification_date", "termination_date", "termination_code",
    "medicaid_only", "chain_owner", "fiscal_year_end", "cbsa_code",
    "snapshot_quarter", "source_url", "raw",
]

COL_TYPES = [
    "String", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(Int32)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(UInt8)", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "String", "Nullable(String)", "String",
]


# CMS PRVDR_CTGRY_CD → human label. Values per CMS POS data dictionary.
PRVDR_CTGRY = {
    "01": "Hospital",
    "02": "Skilled Nursing Facility",
    "03": "Home Health Agency",
    "04": "Psychiatric Residential Treatment Facility",
    "05": "Comprehensive Outpatient Rehab",
    "06": "End-Stage Renal Disease",
    "07": "Portable X-ray",
    "08": "Outpatient Physical Therapy / Speech",
    "09": "End-Stage Renal Disease (alt)",
    "10": "Hospice",
    "11": "Rural Health Clinic",
    "12": "Ambulatory Surgical Center",
    "13": "Hospice / Rural Health",
    "14": "Community Mental Health Center",
    "15": "Ambulatory Surgical Center",
    "16": "Federally Qualified Health Center",
    "17": "Religious Non-Medical Health Care Institution",
    "18": "Federally Qualified Health Center",
    "19": "Critical Access Hospital",
    "20": "Transplant Center",
}


def _iso(s: str | None) -> str | None:
    if not s or s.strip() in ("", "00000000"):
        return None
    s = s.strip()
    # POS dates: YYYYMMDD or MM/DD/YYYY
    if len(s) == 8 and s.isdigit():
        try:
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        except ValueError:
            return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _int(v):
    if v in (None, "", "0"):
        return 0 if v == "0" else None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def discover_csv_url() -> str:
    """Resolve the latest 'Hospital_and_other' POS CSV from CMS DCAT.

    There are three POS dataset families on CMS DCAT (iQIES, CLIA, Hospital-and-other).
    We specifically want the 'Quality Improvement and Evaluation System' file with
    the broadest facility coverage (hospitals + ASCs + outpatient + dialysis +
    psychiatric + transplant centers). Filename always starts with
    'Hospital_and_other' (older quarters use 'other_data' or 'POS_OTHER').
    """
    log.info("Resolving POS file URL from DCAT catalog")
    r = requests.get(DCAT_URL, timeout=120)
    r.raise_for_status()
    candidates: list[str] = []
    for item in r.json().get("dataset", []):
        title = item.get("title", "").lower()
        if "quality improvement and evaluation system" not in title:
            continue
        if "internet" in title:  # skip iQIES family
            continue
        for dist in item.get("distribution", []):
            url = dist.get("downloadURL") or dist.get("accessURL", "")
            if url and url.endswith(".csv") and "Hospital_and_other" in url:
                candidates.append(url)
    if not candidates:
        raise RuntimeError("No POS Hospital_and_other CSV found in DCAT catalog")
    # Pick newest by quarter tag (or fall back to lexicographic last)
    import re
    def _qsort(u: str):
        m = re.search(r"Q(\d)_(\d{4})", u)
        if m:
            return (int(m.group(2)), int(m.group(1)))
        return (0, 0)
    chosen = max(candidates, key=_qsort)
    log.info("Resolved → %s", chosen)
    return chosen


def _infer_quarter(url: str) -> str:
    """Pull a 'YYYYQn' tag from a URL like '..._Q1_2026.csv'."""
    import re
    m = re.search(r"Q(\d)_(\d{4})", url)
    if m:
        return f"{m.group(2)}Q{m.group(1)}"
    return _dt.date.today().strftime("%YQ%m")


def parse_rows(csv_bytes: bytes, source_url: str, snapshot: str):
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        ccn = (r.get("PRVDR_NUM") or "").strip()
        if not ccn:
            continue
        ctgry = (r.get("PRVDR_CTGRY_CD") or "").strip()
        # Trim oversized raw payload — 473 columns × every row is heavy.
        # Keep useful subset for forensic queries.
        raw_keep = {k: v for k, v in r.items() if v and k in (
            "PRVDR_NUM", "FAC_NAME", "PRVDR_CTGRY_CD", "PRVDR_CTGRY_SBTYP_CD",
            "ST_ADR", "CITY_NAME", "STATE_CD", "ZIP_CD", "PHNE_NUM",
            "CRTFCTN_DT", "TRMNTN_EXPRTN_DT", "PGM_TRMNTN_CD",
            "MDCD_VNDR_NUM", "CBSA_CD", "FIPS_CNTY_CD", "FIPS_STATE_CD",
            "GNRL_CNTL_TYPE_CD",  # ownership type
            "FY_BGN_DT", "FY_END_DT",
            "BED_CNT", "CRTFD_BED_CNT", "TOT_OFC_LCTN_CNT",
        )}
        yield (
            ccn,
            r.get("FAC_NAME") or None,
            PRVDR_CTGRY.get(ctgry, f"Unknown ({ctgry})") if ctgry else None,
            ctgry or None,
            r.get("PRVDR_CTGRY_SBTYP_CD") or None,
            r.get("ST_ADR") or None,
            r.get("CITY_NAME") or None,
            r.get("STATE_CD") or None,
            r.get("ZIP_CD") or None,
            r.get("FIPS_CNTY_CD") or None,
            r.get("PHNE_NUM") or None,
            _int(r.get("CRTFD_BED_CNT") or r.get("BED_CNT")),
            _iso(r.get("CRTFCTN_DT")),
            _iso(r.get("TRMNTN_EXPRTN_DT")),
            r.get("PGM_TRMNTN_CD") or None,
            1 if r.get("MDCD_VNDR_NUM") and not r.get("PRVDR_NUM") else 0,
            r.get("GNRL_CNTL_TYPE_CD") or None,
            r.get("FY_END_DT") or None,
            r.get("CBSA_CD") or None,
            snapshot,
            source_url,
            json.dumps(raw_keep, ensure_ascii=False),
        )


def ingest(batch_size: int = 5000, limit: int | None = None) -> tuple[int, int]:
    url = discover_csv_url()
    log.info("Downloading %s", url)
    r = requests.get(url, timeout=600, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("Downloaded %d bytes", len(r.content))
    snapshot = _infer_quarter(url)
    rows = list(parse_rows(r.content, url, snapshot))
    if limit:
        rows = rows[:limit]
    # Dedup by (ccn, snapshot)
    seen: dict[tuple[str, str], tuple] = {}
    for row in rows:
        seen[(row[0], row[-3])] = row
    rows = list(seen.values())

    fetched = len(rows)
    upserted = 0
    status = "success"
    error: str | None = None
    with db.connect() as conn:
        handle = db.start_ingest_log(conn, DATASET)
        try:
            for i in range(0, len(rows), batch_size):
                chunk = rows[i : i + batch_size]
                conn.insert("pos_facilities", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("POS upserted %d rows", upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            log.exception("POS ingest failed")
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    f, u = ingest()
    print(f"Done. fetched={f}, upserted={u}")
