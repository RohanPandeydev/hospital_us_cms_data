"""Loaders for CMS Medicare provider-level utilization CSVs.

Two datasets:
  1. Medicare Inpatient Hospitals by Provider and Service — CCN × DRG × discharges
  2. Medicare Physician & Other Practitioners by Provider and Service — NPI × HCPCS × services

Both are published as plain CSV. We stream-parse to avoid loading the whole
file into memory (the physician file is ~500 MB) and filter on the fly.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import urllib.request
from typing import Iterator, Optional

import psycopg2.extras

log = logging.getLogger(__name__)


# --- Inpatient (CCN × DRG) ---

INPATIENT_CSV_URL = (
    "https://data.cms.gov/sites/default/files/2026-04/"
    "828defb5-c9e6-4442-8c1b-f27bc0799daf/MUP_INP_RY26_P03_V10_DY24_PrvSvc.CSV"
)
INPATIENT_YEAR = 2024  # year embedded in filename (DY24)


def _num(v: Optional[str]) -> Optional[float]:
    if v in (None, "", "NA", "N/A", "*"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Optional[str]) -> Optional[int]:
    n = _num(v)
    return int(n) if n is not None else None


def iter_inpatient_rows(path: str) -> Iterator[dict]:
    """Stream rows from the CMS inpatient CSV."""
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ccn = (row.get("Rndrng_Prvdr_CCN") or "").strip()
            drg = (row.get("DRG_Cd") or "").strip()
            if not ccn or not drg:
                continue
            yield {
                "ccn": ccn,
                "drg_code": drg,
                "drg_description": row.get("DRG_Desc"),
                "year": INPATIENT_YEAR,
                "total_discharges": _int(row.get("Tot_Dschrgs")),
                "avg_submitted_charge": _num(row.get("Avg_Submtd_Cvrd_Chrg")),
                "avg_total_payment": _num(row.get("Avg_Tot_Pymt_Amt")),
                "avg_medicare_payment": _num(row.get("Avg_Mdcr_Pymt_Amt")),
            }


def load_inpatient(conn, path: str, *, device_drgs_only: bool = True,
                   batch_size: int = 2000) -> int:
    """Load rows from the CMS inpatient CSV into cms_hospital_drg_volume.

    When `device_drgs_only=True`, we only keep rows whose DRG appears in
    drg_to_device_category — drastically shrinking the ~150k-row file to
    the device-relevant subset.
    """
    device_drgs: set[str] = set()
    if device_drgs_only:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT drg_code FROM drg_to_device_category")
            device_drgs = {r[0] for r in cur.fetchall()}
        # CMS DRG codes have a leading zero (e.g. "003", "028"). Ensure
        # our match set is normalised to the same zero-padded form.
        device_drgs = {d.zfill(3) for d in device_drgs}

    upsert_sql = """
        INSERT INTO cms_hospital_drg_volume (
            ccn, drg_code, drg_description, year,
            total_discharges, avg_submitted_charge,
            avg_total_payment, avg_medicare_payment
        ) VALUES %s
        ON CONFLICT (ccn, drg_code, year) DO UPDATE SET
            drg_description      = EXCLUDED.drg_description,
            total_discharges     = EXCLUDED.total_discharges,
            avg_submitted_charge = EXCLUDED.avg_submitted_charge,
            avg_total_payment    = EXCLUDED.avg_total_payment,
            avg_medicare_payment = EXCLUDED.avg_medicare_payment
    """

    batch: list[tuple] = []
    written = 0

    def flush():
        nonlocal batch, written
        if not batch:
            return
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, upsert_sql, batch, page_size=500)
        written += len(batch)
        batch = []

    for row in iter_inpatient_rows(path):
        if device_drgs and row["drg_code"].zfill(3) not in device_drgs:
            continue
        batch.append((
            row["ccn"], row["drg_code"], row["drg_description"], row["year"],
            row["total_discharges"], row["avg_submitted_charge"],
            row["avg_total_payment"], row["avg_medicare_payment"],
        ))
        if len(batch) >= batch_size:
            flush()
            log.info("  inpatient: %d device-DRG rows written", written)

    flush()
    log.info("Inpatient ingest complete: %d rows", written)
    return written


# --- Physician (NPI × HCPCS) ---

PHYSICIAN_CSV_URL = (
    "https://data.cms.gov/sites/default/files/2025-04/"
    "e3f823f8-db5b-4cc7-ba04-e7ae92b99757/MUP_PHY_R25_P05_V20_D23_Prov_Svc.csv"
)
PHYSICIAN_YEAR = 2023


def iter_physician_rows(path: str) -> Iterator[dict]:
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            npi   = (row.get("Rndrng_NPI") or "").strip()
            hcpcs = (row.get("HCPCS_Cd") or "").strip()
            if not npi or not hcpcs:
                continue
            yield {
                "npi": npi,
                "hcpcs_code": hcpcs,
                "year": PHYSICIAN_YEAR,
                "provider_type":   row.get("Rndrng_Prvdr_Type"),
                "place_of_service": row.get("Place_Of_Srvc") or "",
                "tot_benes":       _int(row.get("Tot_Benes")),
                "tot_services":    _int(row.get("Tot_Srvcs")),
                "avg_mdcr_pymt_amt": _num(row.get("Avg_Mdcr_Pymt_Amt")),
            }


def load_physician(conn, path: str, *, device_hcpcs_only: bool = True,
                   batch_size: int = 2000) -> int:
    """Load rows from CMS physician CSV into cms_physician_hcpcs_volume.

    When device_hcpcs_only=True, only rows whose HCPCS is in
    bridge_hcpcs_to_product_code are kept.
    """
    device_codes: set[str] = set()
    if device_hcpcs_only:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT hcpcs_code FROM bridge_hcpcs_to_product_code")
            device_codes = {r[0] for r in cur.fetchall()}

    upsert_sql = """
        INSERT INTO cms_physician_hcpcs_volume (
            npi, hcpcs_code, year, provider_type, place_of_service,
            tot_benes, tot_services, avg_mdcr_pymt_amt
        ) VALUES %s
        ON CONFLICT (npi, hcpcs_code, place_of_service, year) DO UPDATE SET
            provider_type    = EXCLUDED.provider_type,
            tot_benes        = EXCLUDED.tot_benes,
            tot_services     = EXCLUDED.tot_services,
            avg_mdcr_pymt_amt = EXCLUDED.avg_mdcr_pymt_amt
    """

    batch: list[tuple] = []
    written = 0

    def flush():
        nonlocal batch, written
        if not batch:
            return
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, upsert_sql, batch, page_size=500)
        written += len(batch)
        batch = []

    for row in iter_physician_rows(path):
        if device_codes and row["hcpcs_code"] not in device_codes:
            continue
        batch.append((
            row["npi"], row["hcpcs_code"], row["year"],
            row["provider_type"], row["place_of_service"],
            row["tot_benes"], row["tot_services"], row["avg_mdcr_pymt_amt"],
        ))
        if len(batch) >= batch_size:
            flush()
            log.info("  physician: %d device-HCPCS rows written", written)

    flush()
    log.info("Physician ingest complete: %d rows", written)
    return written


# --- Download helper ---

def download(url: str, dest: str, *, force: bool = False) -> str:
    if os.path.exists(dest) and not force and os.path.getsize(dest) > 10_000_000:
        log.info("Already downloaded: %s (%d bytes)", dest, os.path.getsize(dest))
        return dest
    log.info("Downloading %s -> %s", url, dest)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 risk-pipeline"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    log.info("Downloaded %d bytes", os.path.getsize(dest))
    return dest
