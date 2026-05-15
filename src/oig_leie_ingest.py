"""OIG LEIE — List of Excluded Individuals/Entities ingester.

Source: oig.hhs.gov/exclusions/downloadables/UPDATED.csv (~15 MB monthly).

Each row is a person or entity excluded from Medicare/Medicaid/all federal
healthcare programs. The CSV columns are documented in
LEIEDataFileFormat.pdf:

  LASTNAME, FIRSTNAME, MIDNAME, BUSNAME, GENERAL, SPECIALTY, UPIN, NPI, DOB,
  ADDRESS, CITY, STATE, ZIP, EXCLTYPE, EXCLDATE, REINDATE, WAIVERDATE, WVRSTATE

Natural key for the ReplacingMergeTree is the hashed combo of
(lastname, firstname, busname, npi, excldate) — same person can have multiple
exclusion events. We store `leie_id` as that hash.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import logging

import requests

from . import db

log = logging.getLogger(__name__)

LEIE_URL = "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"
DATASET = {"id": "oig_leie", "name": "OIG List of Excluded Individuals/Entities"}

COLS = [
    "leie_id", "last_name", "first_name", "middle_name", "business_name",
    "general", "specialty", "upin", "npi", "dob",
    "address", "city", "state", "zip",
    "exclusion_type", "exclusion_date", "reinstate_date",
    "waiver_date", "waiver_state",
    "source_url", "raw",
]

# Explicit ClickHouse column types so clickhouse_connect doesn't fall back to
# python-type inference. All date columns are stored as Nullable(String)
# (ISO 'YYYY-MM-DD') to dodge the driver's all-None Nullable(Date) bug.
COL_TYPES = [
    "UInt64",
    "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)",  # dob
    "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)",  # exclusion_type
    "Nullable(String)", "Nullable(String)",  # exclusion_date, reinstate_date
    "Nullable(String)", "Nullable(String)",  # waiver_date, waiver_state
    "Nullable(String)", "String",
]


def _parse_leie_date(s: str | None) -> str | None:
    """Return ISO 'YYYY-MM-DD' string or None.

    Stored as String in ClickHouse to dodge the driver's all-None Nullable(Date)
    inference bug. Parseable at query time with parseDateTimeBestEffortOrNull().
    """
    if not s:
        return None
    s = s.strip()
    if not s or s == "00000000":
        return None
    # LEIE dates are YYYYMMDD strings
    if len(s) == 8 and s.isdigit():
        try:
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        except ValueError:
            return None
    # Some recent rows are MM/DD/YYYY
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _leie_id(last: str, first: str, bus: str, npi: str, excldate: str) -> int:
    """Deterministic 63-bit id for ReplacingMergeTree."""
    key = "|".join([
        (last or "").strip().upper(),
        (first or "").strip().upper(),
        (bus or "").strip().upper(),
        (npi or "").strip(),
        (excldate or "").strip(),
    ])
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:15], 16)


def download_csv() -> bytes:
    log.info("Downloading %s", LEIE_URL)
    r = requests.get(LEIE_URL, timeout=120,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    log.info("Downloaded %d bytes", len(r.content))
    return r.content


def parse_rows(csv_bytes: bytes):
    """Yield row tuples ready for ClickHouse insert."""
    text = csv_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        # CSV headers in the LEIE file are uppercase
        last = r.get("LASTNAME") or ""
        first = r.get("FIRSTNAME") or ""
        bus = r.get("BUSNAME") or ""
        npi = r.get("NPI") or ""
        excldate_raw = r.get("EXCLDATE") or ""
        yield (
            _leie_id(last, first, bus, npi, excldate_raw),
            last or None,
            first or None,
            r.get("MIDNAME") or None,
            bus or None,
            r.get("GENERAL") or None,
            r.get("SPECIALTY") or None,
            r.get("UPIN") or None,
            npi or None,
            _parse_leie_date(r.get("DOB")),
            r.get("ADDRESS") or None,
            r.get("CITY") or None,
            r.get("STATE") or None,
            r.get("ZIP") or None,
            r.get("EXCLTYPE") or None,
            _parse_leie_date(excldate_raw),
            _parse_leie_date(r.get("REINDATE")),
            _parse_leie_date(r.get("WAIVERDATE")),
            r.get("WVRSTATE") or None,
            LEIE_URL,
            json.dumps(r, ensure_ascii=False),
        )


def ingest(batch_size: int = 5000, limit: int | None = None) -> tuple[int, int]:
    """Download, parse, upsert in batches. Returns (fetched, upserted)."""
    payload = download_csv()
    rows = list(parse_rows(payload))
    if limit:
        rows = rows[:limit]

    # Dedup within this batch by leie_id (CSV occasionally has dupes)
    seen: dict[int, tuple] = {}
    for r in rows:
        seen[r[0]] = r
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
                conn.insert(
                    "oig_leie_exclusions", chunk,
                    column_names=COLS, column_type_names=COL_TYPES,
                )
                upserted += len(chunk)
            log.info("LEIE upserted %d rows", upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            log.exception("LEIE ingest failed")
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    f, u = ingest()
    print(f"Done. fetched={f}, upserted={u}")
