"""HCPCS Level II master ingester.

Downloads the current CMS HCPCS quarterly ZIP, extracts the fixed-width .txt
file, parses ~7,500 HCPCS codes, and upserts into `hcpcs_master`.

Record layout (char positions, 1-indexed):
  01-05  HCPCS code
  11-11  Record ID (only '1' = main record)
  12-91  Long description  (80 chars)
  92-119 Short description (28 chars)
  120-121 Pricing indicator
  230    Coverage code
  231-232 ASC payment group
  241-243 MOG payment group
  257-259 BETOS code
  261    Type of service
  293    Action code (A=added, C=changed, D=deleted)
"""
import io
import logging
import zipfile
import requests

from psycopg2.extras import execute_values

from . import db

log = logging.getLogger(__name__)

DEVICE_FAMILIES = {"C", "E", "K", "L"}  # device-dominant HCPCS prefixes

CURRENT_HCPCS_URL = "https://www.cms.gov/files/zip/april-2026-alpha-numeric-hcpcs-file.zip"
CURRENT_HCPCS_QTR = "APR2026"


def _parse_line(line):
    """Parse one fixed-width HCPCS record.

    CMS file format: each HCPCS code may span multiple lines.
      cols 1-5:  HCPCS code
      cols 6-8:  sequence number (001 = main record, 002+ = continuation)
      cols 9-11: record identification sub-code
    We only keep sequence "001" (main records) since descriptions are
    complete on that line (though long descriptions can wrap).
    """
    if len(line) < 120:
        return None
    code = line[0:5].strip()
    if not code:
        return None
    seq = line[5:8].strip()
    if seq != "001":
        return None  # only main records, skip continuations
    long_desc  = line[11:91].strip()
    short_desc = line[91:119].strip()
    pricing    = line[119:121].strip() if len(line) >= 121 else None
    coverage   = line[229:230].strip() if len(line) >= 230 else None
    asc_grp    = line[230:232].strip() if len(line) >= 232 else None
    mog_grp    = line[240:243].strip() if len(line) >= 243 else None
    betos      = line[256:259].strip() if len(line) >= 259 else None
    tos        = line[260:261].strip() if len(line) >= 261 else None
    action     = line[292:293].strip() if len(line) >= 293 else None
    family     = code[0] if code[0].isalpha() else None
    is_device  = family in DEVICE_FAMILIES if family else False
    return (
        code, short_desc or None, long_desc or None,
        betos or None, pricing or None, coverage or None,
        asc_grp or None, mog_grp or None, tos or None, action or None,
        family, is_device, CURRENT_HCPCS_QTR, line.rstrip(),
    )


def download_and_parse(url=None):
    url = url or CURRENT_HCPCS_URL
    log.info("downloading %s", url)
    r = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("downloaded %d bytes", len(r.content))

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        txt_name = next((n for n in z.namelist() if n.endswith("ANWEB.txt")), None)
        if not txt_name:
            raise RuntimeError(f"No ANWEB.txt in ZIP: {z.namelist()}")
        log.info("parsing %s", txt_name)
        with z.open(txt_name) as f:
            rows = []
            for raw in f:
                line = raw.decode("latin-1", errors="replace").rstrip("\n\r")
                row = _parse_line(line)
                if row:
                    rows.append(row)
    log.info("parsed %d HCPCS codes", len(rows))
    return rows


HCPCS_COLUMNS = [
    "hcpcs_code", "short_desc", "long_desc",
    "betos_code", "pricing_indicator", "coverage_code",
    "asc_payment_grp", "mog_payment_grp", "type_of_service", "action_code",
    "code_family", "is_device", "effective_qtr", "raw_line",
]


def ingest(limit=None):
    """Fetch + insert. limit caps rows (for smoke tests)."""
    rows = download_and_parse()
    if limit:
        rows = rows[:limit]

    conn = db.connect()
    dataset_meta = {"id": "hcpcs_master", "name": "HCPCS Level II Master"}
    log_id = db.start_ingest_log(conn, dataset_meta)
    fetched = len(rows)
    upserted = 0
    status = "success"
    error = None

    try:
        sql = f"""
            INSERT INTO hcpcs_master ({", ".join(HCPCS_COLUMNS)})
            VALUES %s
            ON CONFLICT (hcpcs_code) DO UPDATE SET
                short_desc        = EXCLUDED.short_desc,
                long_desc         = EXCLUDED.long_desc,
                betos_code        = EXCLUDED.betos_code,
                pricing_indicator = EXCLUDED.pricing_indicator,
                coverage_code     = EXCLUDED.coverage_code,
                asc_payment_grp   = EXCLUDED.asc_payment_grp,
                mog_payment_grp   = EXCLUDED.mog_payment_grp,
                type_of_service   = EXCLUDED.type_of_service,
                action_code       = EXCLUDED.action_code,
                code_family       = EXCLUDED.code_family,
                is_device         = EXCLUDED.is_device,
                effective_qtr     = EXCLUDED.effective_qtr,
                raw_line          = EXCLUDED.raw_line,
                fetched_at        = now();
        """
        with conn.cursor() as cur:
            execute_values(cur, sql, rows, page_size=500)
        conn.commit()
        upserted = len(rows)
        log.info("upserted %d rows", upserted)
    except Exception as e:
        status = "error"
        error = repr(e)
        conn.rollback()
        log.exception("HCPCS ingest failed")
        raise
    finally:
        db.finish_ingest_log(conn, log_id, fetched, upserted, status, error)
        conn.close()
    return fetched, upserted
