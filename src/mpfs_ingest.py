"""Medicare Physician Fee Schedule (PFS) RVU file ingester.

CMS publishes quarterly RVU files at cms.gov/files/zip/rvu{yy}{q}.zip
(redirects to the latest updated copy, e.g. rvu25d-updated-09/11/2025.zip).

The ZIP contains PPRRVU{yy}{q}.csv — the row-per-HCPCS RVU/payment file.
This is the only file we ingest right now; the others (GPCI, ANES, LOCCO,
NPC) can be added later if needed.

Payments are computed:
  facility_payment       = total_facility_rvu      * conversion_factor
  non_facility_payment   = total_non_facility_rvu  * conversion_factor

CF (Conversion Factor) is in the file header but not as a data column — we
hardcode the published CF for known years and recompute per quarter.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile

import requests

from . import db

log = logging.getLogger(__name__)

DATASET = {"id": "mpfs_rates", "name": "Medicare Physician Fee Schedule RVUs"}

# Conversion factors per program year. Updated by CMS each fall.
# Source: cms.gov/medicare/payment/fee-schedules/physician
CF = {
    2024: 32.7442,
    2025: 32.3465,
    2026: 33.2875,   # FY2026 final-rule placeholder; verify against final rule
}

COLS = [
    "hcpcs_code", "modifier", "effective_year", "effective_quarter",
    "short_descriptor", "status_code",
    "pe_facility_rvu", "pe_non_facility_rvu",
    "work_rvu", "malpractice_rvu",
    "total_facility_rvu", "total_non_facility_rvu",
    "facility_payment", "non_facility_payment",
    "conversion_factor", "global_days", "bilateral_indicator",
    "raw",
]
COL_TYPES = [
    "String", "String", "UInt16", "UInt8",
    "Nullable(String)", "Nullable(String)",
    "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(Float64)", "Nullable(Float64)",
    "Nullable(Float64)", "Nullable(String)", "Nullable(String)",
    "String",
]


def _num(v):
    if v in (None, "", "NA"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def quarter_url(year: int, quarter: int) -> str:
    """Resolve the redirect target for the quarterly RVU ZIP."""
    yy = str(year)[-2:]
    q = "abcd"[quarter - 1]
    base = f"https://www.cms.gov/files/zip/rvu{yy}{q}.zip"
    r = requests.head(base, allow_redirects=True, timeout=60,
                      headers={"User-Agent": "Mozilla/5.0"})
    return r.url


def find_pprrvu_csv(zip_bytes: bytes) -> tuple[str, bytes]:
    """Return (name, contents) of the PPRRVU CSV inside the ZIP."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = z.namelist()
        # Prefer the CSV explicitly. PPRRVU file is sometimes shipped as TXT.
        for n in names:
            up = n.upper()
            if up.startswith("PPRRVU") and up.endswith(".CSV"):
                return n, z.read(n)
        for n in names:
            up = n.upper()
            if "PPRRVU" in up and (up.endswith(".CSV") or up.endswith(".TXT")):
                return n, z.read(n)
    raise RuntimeError("No PPRRVU file in RVU ZIP")


# Map common PPRRVU header variants (CMS sometimes shifts naming or
# inserts new columns) to canonical keys.
HEADER_ALIASES = {
    "HCPCS": "HCPCS",
    "HCPCS_CD": "HCPCS",
    "MOD": "MOD",
    "MODIFIER": "MOD",
    "DESCRIPTION": "DESC",
    "STATUS CODE": "STATUS",
    "STATUS": "STATUS",
    "WORK RVU": "WORK_RVU",
    "WORK RVU'S": "WORK_RVU",
    "FULLY IMPLEMENTED FACILITY PE RVU": "PE_FAC",
    "NON-FAC PE RVU": "PE_NON_FAC",
    "NON-FACILITY PE RVU": "PE_NON_FAC",
    "FACILITY PE RVU": "PE_FAC",
    "MP RVU": "MP_RVU",
    "FACILITY TOTAL": "TOTAL_FAC",
    "NON-FACILITY TOTAL": "TOTAL_NON_FAC",
    "FULLY IMPLEMENTED FACILITY TOTAL": "TOTAL_FAC",
    "FULLY IMPLEMENTED NON-FACILITY TOTAL": "TOTAL_NON_FAC",
    "GLOB DAYS": "GLOBAL_DAYS",
    "GLOBAL": "GLOBAL_DAYS",
    "BILT SURG": "BILATERAL",
}


def _norm_header(h: str) -> str:
    h = h.strip().upper().replace("\n", " ")
    return HEADER_ALIASES.get(h, h)


def parse_pprrvu(text: str, year: int, quarter: int, source_url: str):
    """Yield row tuples for ClickHouse insert.

    PPRRVU is positional, not header-keyed — CMS splits column labels across
    rows 6–9 of a multi-line preamble, then puts code rows starting around
    row 10. We use fixed column indices based on the documented layout
    (NPC ZIP includes a `PPRRVUyy.docx` describing each column).
    """
    cf = CF.get(year, 32.3465)
    rows = list(csv.reader(io.StringIO(text)))
    # Find the first data row: first column is a 5-char HCPCS/CPT code.
    code_re = re.compile(r"^[A-Z0-9]{5}$")
    data_start = None
    for i, row in enumerate(rows):
        if row and row[0] and code_re.match(row[0].strip()):
            data_start = i
            break
    if data_start is None:
        log.warning("No HCPCS data rows found in PPRRVU")
        return

    # Column positions per CMS PPRRVU layout:
    #   0 HCPCS  1 MOD  2 DESCRIPTION  3 STATUS  4 NOT_USED
    #   5 WORK_RVU  6 NON_FAC_PE_RVU  7 NA_IND  8 FAC_PE_RVU  9 NA_IND
    #  10 MP_RVU  11 NON_FAC_TOTAL  12 FAC_TOTAL  13 PCTC  14 GLOBAL_DAYS
    #  19 MULT_PROC  20 BILAT_SURG  24 CONV_FACTOR
    #  28 NON_FAC_PAYMENT  29 FAC_PAYMENT
    IDX_HCPCS, IDX_MOD, IDX_DESC, IDX_STATUS = 0, 1, 2, 3
    IDX_WORK, IDX_PE_NON, IDX_PE_FAC = 5, 6, 8
    IDX_MP, IDX_TOT_NON, IDX_TOT_FAC = 10, 11, 12
    IDX_GLOBAL, IDX_BILAT, IDX_CF = 14, 20, 24
    IDX_PAY_NON, IDX_PAY_FAC = 28, 29

    for row in rows[data_start:]:
        if not row or not row[0]:
            continue
        hcpcs = row[IDX_HCPCS].strip()
        if not code_re.match(hcpcs):
            continue
        modifier = (row[IDX_MOD] if len(row) > IDX_MOD else "").strip()
        # Use the CSV-published payment columns (already CF-applied) when
        # present; fall back to RVU × CF otherwise.
        pay_non = _num(row[IDX_PAY_NON]) if len(row) > IDX_PAY_NON else None
        pay_fac = _num(row[IDX_PAY_FAC]) if len(row) > IDX_PAY_FAC else None
        tot_non = _num(row[IDX_TOT_NON]) if len(row) > IDX_TOT_NON else None
        tot_fac = _num(row[IDX_TOT_FAC]) if len(row) > IDX_TOT_FAC else None
        if pay_non is None and tot_non is not None:
            pay_non = tot_non * cf
        if pay_fac is None and tot_fac is not None:
            pay_fac = tot_fac * cf
        rec = {
            "hcpcs": hcpcs, "mod": modifier,
            "desc": row[IDX_DESC] if len(row) > IDX_DESC else None,
            "status": row[IDX_STATUS] if len(row) > IDX_STATUS else None,
            "work_rvu": row[IDX_WORK] if len(row) > IDX_WORK else None,
            "pe_non_fac": row[IDX_PE_NON] if len(row) > IDX_PE_NON else None,
            "pe_fac": row[IDX_PE_FAC] if len(row) > IDX_PE_FAC else None,
        }
        yield (
            hcpcs, modifier or "",
            year, quarter,
            (row[IDX_DESC] or None) if len(row) > IDX_DESC else None,
            (row[IDX_STATUS] or None) if len(row) > IDX_STATUS else None,
            _num(row[IDX_PE_FAC]) if len(row) > IDX_PE_FAC else None,
            _num(row[IDX_PE_NON]) if len(row) > IDX_PE_NON else None,
            _num(row[IDX_WORK]) if len(row) > IDX_WORK else None,
            _num(row[IDX_MP]) if len(row) > IDX_MP else None,
            tot_fac, tot_non,
            pay_fac, pay_non,
            _num(row[IDX_CF]) if len(row) > IDX_CF else cf,
            (row[IDX_GLOBAL] or None) if len(row) > IDX_GLOBAL else None,
            (row[IDX_BILAT] or None) if len(row) > IDX_BILAT else None,
            json.dumps(rec, ensure_ascii=False),
        )


def ingest_quarter(year: int, quarter: int,
                   batch_size: int = 5000) -> tuple[int, int]:
    url = quarter_url(year, quarter)
    log.info("Downloading PFS RVU %dQ%d → %s", year, quarter, url)
    r = requests.get(url, timeout=300, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("Downloaded %d bytes", len(r.content))
    name, csv_bytes = find_pprrvu_csv(r.content)
    log.info("Extracted %s (%d bytes)", name, len(csv_bytes))
    text = csv_bytes.decode("latin-1", errors="replace")
    rows = list(parse_pprrvu(text, year, quarter, url))
    log.info("Parsed %d rows from %s", len(rows), name)

    # Dedup by (hcpcs, modifier)
    seen: dict[tuple, tuple] = {}
    for r in rows:
        seen[(r[0], r[1])] = r
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
                conn.insert("mpfs_rates", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("PFS upserted %d rows for %dQ%d", upserted, year, quarter)
        except Exception as e:
            status = "error"
            error = repr(e)
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted


def ingest(year: int = 2025, quarter: int = 4) -> tuple[int, int]:
    """Default: ingest the latest known quarter."""
    return ingest_quarter(year, quarter)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    y = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    q = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    f, u = ingest_quarter(y, q)
    print(f"Done. {y}Q{q}: fetched={f}, upserted={u}")
