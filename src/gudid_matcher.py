"""Auto-populate bridge_hcpcs_to_product_code from GUDID text matching.

Strategy (pure SQL, runs in seconds against the GIN-indexed device_description):

  1. For each device-family HCPCS (is_device=true), extract 2+-character
     words from long_desc as a plainto_tsquery.
  2. Full-text match against fda_gudid_devices.device_description using the
     GIN index (idx_gudid_desc_tsv).
  3. Group the resulting GUDID rows by product_code and take the one that
     appears most often — that is the best-supported mapping for this HCPCS.
  4. Classify confidence from the support count (how many GUDID records
     backed the winning product_code).
  5. Insert new (hcpcs_code, product_code) pairs ONLY. Manual-seed rows are
     never overwritten (ON CONFLICT DO NOTHING).

What's intentionally simple:
  - No TF-IDF, no fuzzy. Postgres full-text is already stem/stop-aware and
    the device family is small enough (~1.8k HCPCS codes).
  - One winner per HCPCS. Ties are broken by lexical order of product_code.
"""

import logging

log = logging.getLogger(__name__)


MATCH_SQL = """
WITH device_hcpcs AS (
    SELECT hcpcs_code, long_desc
      FROM hcpcs_master
     WHERE is_device = TRUE
       AND long_desc IS NOT NULL
       AND length(long_desc) >= 8
),
matches AS (
    SELECT
        h.hcpcs_code,
        h.long_desc,
        g.product_code,
        count(*) AS support
      FROM device_hcpcs h
      JOIN fda_gudid_devices g
        ON g.product_code IS NOT NULL
       AND g.device_description IS NOT NULL
       AND to_tsvector('english', g.device_description)
           @@ plainto_tsquery('english', h.long_desc)
     GROUP BY h.hcpcs_code, h.long_desc, g.product_code
),
ranked AS (
    SELECT
        hcpcs_code,
        product_code,
        support,
        row_number() OVER (PARTITION BY hcpcs_code
                           ORDER BY support DESC, product_code ASC) AS rk
      FROM matches
     WHERE support >= %(min_support)s
)
SELECT hcpcs_code, product_code, support
  FROM ranked
 WHERE rk = 1;
"""


UPSERT_SQL = """
    INSERT INTO bridge_hcpcs_to_product_code
        (hcpcs_code, product_code, device_category, match_method, confidence, source_notes)
    VALUES (%s, %s, %s, 'gudid_description', %s, %s)
    ON CONFLICT (hcpcs_code, product_code) DO NOTHING;
"""


def _confidence(support):
    if support >= 10:
        return "high"
    if support >= 3:
        return "medium"
    return "low"


def auto_populate(conn, min_support=2):
    """Run the match and insert candidates. Returns (proposed, inserted).

    min_support — minimum number of GUDID records backing a (HCPCS, PC) pair
    before we're willing to propose it (raise this to filter noise).
    """
    proposed = 0
    inserted = 0

    with conn.cursor() as cur:
        cur.execute(MATCH_SQL, {"min_support": min_support})
        candidates = cur.fetchall()

    log.info("gudid matcher: %d HCPCS codes have candidates", len(candidates))

    with conn.cursor() as cur:
        for hcpcs_code, product_code, support in candidates:
            proposed += 1
            conf = _confidence(support)
            note = f"auto-matched via GUDID description (support={support})"
            cur.execute(UPSERT_SQL, (
                hcpcs_code, product_code,
                None,  # device_category — let a downstream step label
                conf, note,
            ))
            if cur.rowcount == 1:
                inserted += 1
    conn.commit()

    log.info("gudid matcher: proposed=%d inserted=%d (rest already in bridge)",
             proposed, inserted)
    return proposed, inserted
