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
    """Yield row tuples for ClickHouse insert."""
    cf = CF.get(year, 32.3465)
    # PPRRVU files have a multi-line preamble (release notes) before the
    # data table. Scan until we find a row whose first field looks like
    # a HCPCS code.
    rows = list(csv.reader(io.StringIO(text)))
    header_idx = None
    for i, row in enumerate(rows):
        joined = ",".join((c or "").upper() for c in row)
        if "HCPCS" in joined and ("MOD" in joined or "STATUS" in joined):
            header_idx = i
            break
    if header_idx is None:
        log.warning("No header row found in PPRRVU; treating row 0 as header.")
        header_idx = 0
    raw_header = rows[header_idx]
    header = [_norm_header(h) for h in raw_header]
    for row in rows[header_idx + 1:]:
        if not row or not row[0] or len(row[0]) > 6:
            continue
        rec = dict(zip(header, row))
        hcpcs = (rec.get("HCPCS") or "").strip()
        if not hcpcs or not re.match(r"^[A-Z0-9]{5}$", hcpcs):
            continue
        modifier = (rec.get("MOD") or "").strip()
        work = _num(rec.get("WORK_RVU"))
        pe_fac = _num(rec.get("PE_FAC"))
        pe_non = _num(rec.get("PE_NON_FAC"))
        mp = _num(rec.get("MP_RVU"))
        tot_fac = _num(rec.get("TOTAL_FAC"))
        tot_non = _num(rec.get("TOTAL_NON_FAC"))
        pay_fac = tot_fac * cf if tot_fac is not None else None
        pay_non = tot_non * cf if tot_non is not None else None
        yield (
            hcpcs, modifier or "",
            year, quarter,
            rec.get("DESC") or None,
            rec.get("STATUS") or None,
            pe_fac, pe_non,
            work, mp,
            tot_fac, tot_non,
            pay_fac, pay_non,
            cf, rec.get("GLOBAL_DAYS") or None,
            rec.get("BILATERAL") or None,
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
