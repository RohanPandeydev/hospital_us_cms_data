"""Auto-populate bridge_hcpcs_to_product_code from GUDID text matching.

ClickHouse port: we tokenize HCPCS long_desc in Python and, for each code,
pull GUDID rows whose device_description contains ALL tokens (case-insensitive).
Not as smart as Postgres full-text search (no stemming / stop-words), but good
enough for the seeded device families, and deliberately simple.
"""

import logging
import re

log = logging.getLogger(__name__)


STOPWORDS = {
    "a", "an", "and", "or", "the", "of", "for", "to", "in", "on", "with",
    "without", "as", "at", "by", "is", "are", "be", "this", "that",
    "each", "per", "any", "other", "all", "than", "not", "no",
}
_WORD_RE = re.compile(r"[A-Za-z]{4,}")


def _tokens(text):
    """Return up to 5 distinguishing lowercase tokens from a description."""
    if not text:
        return []
    out = []
    seen = set()
    for w in _WORD_RE.findall(text):
        w = w.lower()
        if w in STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= 5:
            break
    return out


COLUMNS = [
    "hcpcs_code", "product_code", "device_category",
    "match_method", "confidence", "source_notes",
]


def _confidence(support):
    if support >= 10:
        return "high"
    if support >= 3:
        return "medium"
    return "low"


def auto_populate(conn, min_support=2):
    """Match device-family HCPCS to GUDID product_codes by token overlap.

    Returns (proposed, inserted). 'inserted' is best-effort — ClickHouse's
    ReplacingMergeTree doesn't surface per-row conflict state, so we report
    it as the count of pairs we inserted (duplicates against manual_seed
    get collapsed on background merge).
    """
    client = conn.client if hasattr(conn, "client") else conn

    device_rows = client.query(
        "SELECT hcpcs_code, long_desc FROM hcpcs_master FINAL "
        "WHERE is_device = 1 AND long_desc != '' AND length(long_desc) >= 8"
    ).result_rows
    log.info("gudid matcher: scanning %d device-family HCPCS codes", len(device_rows))

    proposed = 0
    to_insert = []
    for hcpcs_code, long_desc in device_rows:
        toks = _tokens(long_desc)
        if len(toks) < 2:
            continue
        # Build AND-of-positionCaseInsensitive conditions — one parameter per token
        conds = []
        params = {}
        for i, t in enumerate(toks):
            key = f"t{i}"
            conds.append(f"positionCaseInsensitive(device_description, {{{key}:String}}) > 0")
            params[key] = t
        sql = (
            "SELECT product_code, count() AS support FROM fda_gudid_devices FINAL "
            "WHERE product_code IS NOT NULL AND device_description IS NOT NULL "
            "  AND " + " AND ".join(conds) +
            " GROUP BY product_code "
            " HAVING support >= {ms:UInt32} "
            " ORDER BY support DESC, product_code ASC LIMIT 1"
        )
        params["ms"] = min_support
        res = client.query(sql, parameters=params).result_rows
        if not res:
            continue
        product_code, support = res[0]
        proposed += 1
        note = f"auto-matched via GUDID description (support={support})"
        to_insert.append((
            hcpcs_code, product_code, None,
            "gudid_description", _confidence(support), note,
        ))

    inserted = 0
    if to_insert:
        client.insert("bridge_hcpcs_to_product_code", to_insert, column_names=COLUMNS)
        inserted = len(to_insert)

    log.info("gudid matcher: proposed=%d inserted=%d", proposed, inserted)
    return proposed, inserted
