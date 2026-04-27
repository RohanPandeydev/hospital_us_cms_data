"""Ingest TinyFish JSON outputs into their Postgres tables.

Each TinyFish run writes a single JSON file to downloads/tinyfish/ shaped as:
    {"type":"COMPLETE", "run_id":..., "result": {"result": [<row>, ...]}}

The inner array holds the structured rows we asked the agent to extract. This
module normalizes those into the schema tables defined at the bottom of
risk_schema.sql (leapfrog_grade, fda_483_inspection, fda_warning_letter,
eudamed_safety_notice).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from datetime import datetime
from typing import Iterable

import psycopg2.extras

from . import risk_db

log = logging.getLogger(__name__)

DOWNLOADS = "downloads/tinyfish"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_SUFFIX_RE = re.compile(
    r"\b(corp(oration)?|inc(orporated)?|ltd|llc|co|company|limited|gmbh|sa|nv|ag|plc|lp)\b\.?",
    re.I,
)
_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def norm_firm(name: str) -> str:
    """Normalize a firm/company name for cross-source joining."""
    if not name:
        return ""
    s = _SUFFIX_RE.sub(" ", name.lower())
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(s.split())


def _rows_from_run(path: str) -> list[dict]:
    """Pull the `result.result` array out of a TinyFish run dump."""
    with open(path) as f:
        ev = json.load(f)
    r = ev.get("result") or {}
    # TinyFish wraps differently depending on goal shape — handle both:
    #   {"result": {"result": [...]}}
    #   {"result": [...]}
    inner = r.get("result") if isinstance(r, dict) else r
    if isinstance(inner, list):
        return inner
    # Agent sometimes returns a "ready to scrape" plan with no data — log + skip
    log.warning("%s: no rows (agent may not have executed extraction)", path)
    return []


def _to_date(v):
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------
# Loaders — one per source
# ------------------------------------------------------------------

def load_leapfrog(conn, paths: Iterable[str]) -> int:
    """Load every leapfrog_*.json into leapfrog_grade (upsert on name+state)."""
    rows: list[tuple] = []
    for p in paths:
        for r in _rows_from_run(p):
            name = (r.get("hospital_name") or "").strip()
            state = (r.get("state") or "").strip().upper()
            if not name or not state:
                continue
            rows.append((
                name, r.get("city"), state, r.get("zip"),
                r.get("safety_grade"), str(r.get("score") or r.get("safety_grade") or ""),
                r.get("hospital_profile_url"),
            ))
    if not rows:
        return 0
    sql = """
        INSERT INTO leapfrog_grade
          (hospital_name, city, state, zip, safety_grade, score, profile_url)
        VALUES %s
        ON CONFLICT (hospital_name, state) DO UPDATE SET
          city        = EXCLUDED.city,
          zip         = EXCLUDED.zip,
          safety_grade= EXCLUDED.safety_grade,
          score       = EXCLUDED.score,
          profile_url = EXCLUDED.profile_url,
          fetched_at  = now()
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=200)
    return len(rows)


def link_leapfrog_ccn(conn) -> int:
    """Resolve leapfrog_grade.ccn by strict normalized-exact match only.

    Strips whitespace/punctuation + lowercases on both sides, then requires
    an equality match within the same state. No pg_trgm, no similarity,
    no LLM. Rows that don't match stay NULL — we surface the miss rate so
    the caller can decide whether to accept the gap or scrape more."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE leapfrog_grade l
               SET ccn = h.facility_id
              FROM cms_hospitals h
             WHERE l.ccn IS NULL
               AND l.state = h.state
               AND regexp_replace(upper(l.hospital_name), '[^A-Z0-9]+', '', 'g')
                 = regexp_replace(upper(h.facility_name), '[^A-Z0-9]+', '', 'g')
        """)
        return cur.rowcount


def load_483(conn, paths: Iterable[str]) -> int:
    rows: list[tuple] = []
    for p in paths:
        for r in _rows_from_run(p):
            firm = (r.get("firm_name") or "").strip()
            if not firm:
                continue
            rows.append((
                firm, norm_firm(firm),
                r.get("firm_city"), r.get("firm_state"), r.get("firm_country"),
                r.get("classification"), r.get("product_type"),
                _to_date(r.get("inspection_end_date")),
                _to_date(r.get("posted_date")),
            ))
    if not rows:
        return 0
    sql = """
        INSERT INTO fda_483_inspection
          (firm_name, firm_name_norm, firm_city, firm_state, firm_country,
           classification, product_type, inspection_end_date, posted_date)
        VALUES %s
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=200)
    return len(rows)


def load_eudamed(conn, paths: Iterable[str]) -> int:
    rows: list[tuple] = []
    for p in paths:
        for r in _rows_from_run(p):
            eid = (r.get("eudamed_id") or r.get("notice_id") or "").strip()
            if not eid:
                # EUDAMED entries without an id can't be dedup-keyed — skip.
                continue
            mfr = (r.get("manufacturer_name") or "").strip()
            rows.append((
                eid, _to_date(r.get("notice_date")),
                mfr, norm_firm(mfr),
                r.get("device_trade_name"),
                r.get("risk_description") or r.get("risk"),
                r.get("action_taken") or r.get("corrective_action"),
                r.get("eudamed_detail_url") or r.get("detail_url"),
            ))
    if not rows:
        return 0
    sql = """
        INSERT INTO eudamed_safety_notice
          (eudamed_id, notice_date, manufacturer_name, manufacturer_norm,
           device_trade_name, risk_description, action_taken, detail_url)
        VALUES %s
        ON CONFLICT (eudamed_id) DO UPDATE SET
          notice_date       = EXCLUDED.notice_date,
          manufacturer_name = EXCLUDED.manufacturer_name,
          manufacturer_norm = EXCLUDED.manufacturer_norm,
          device_trade_name = EXCLUDED.device_trade_name,
          risk_description  = EXCLUDED.risk_description,
          action_taken      = EXCLUDED.action_taken,
          detail_url        = EXCLUDED.detail_url,
          fetched_at        = now()
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=200)
    return len(rows)


# ------------------------------------------------------------------
# Driver — one-shot ingest of everything sitting in downloads/tinyfish/
# ------------------------------------------------------------------

def ingest_all(conn) -> dict:
    out: dict = {}
    out["leapfrog"] = load_leapfrog(conn, glob.glob(f"{DOWNLOADS}/leapfrog*.json"))
    out["leapfrog_matched_ccn"] = link_leapfrog_ccn(conn)
    out["483"] = load_483(conn, glob.glob(f"{DOWNLOADS}/fda_483*.json"))
    out["eudamed"] = load_eudamed(conn, glob.glob(f"{DOWNLOADS}/eudamed*.json"))
    log.info("tinyfish ingest: %s", out)
    return out
