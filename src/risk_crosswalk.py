"""Build the unified device_code_crosswalk from all source tables.

This materializes every known link between device identifiers:
    HCPCS ↔ DRG ↔ product_code ↔ K_number ↔ UDI-DI ↔ brand ↔ manufacturer ↔ GMDN

so the UI and assembler can answer "given device_category=foo, show every
billing code, every FDA product code, every brand name, every manufacturer
that maps into it" — in a single indexed table.

Sources walked:
  1. bridge_hcpcs_to_product_code  → HCPCS + product_code per category
  2. drg_to_device_category        → DRG per category
  3. fda_510k (by product_code)    → K# + applicant per category
  4. fda_gudid_devices             → UDI-DI + brand + company per product_code
  5. fda_device_classification     → authoritative device_name per product_code
  6. hcpcs_master                  → human-readable HCPCS descriptions

Run once after ingestion; it's a pure DB operation.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def build(conn) -> dict:
    """Rebuild device_code_crosswalk from source tables. Returns summary counts."""
    stats: dict[str, int] = {}

    with conn.cursor() as cur:
        cur.execute("TRUNCATE device_code_crosswalk RESTART IDENTITY")

        # ---------- 1. HCPCS ↔ device_category ↔ product_code ----------
        cur.execute("""
            INSERT INTO device_code_crosswalk
                (device_category, code_type, code_value, display_name,
                 source, confidence, linked_product_code)
            SELECT b.device_category, 'hcpcs', b.hcpcs_code,
                   COALESCE(h.short_desc, h.long_desc, b.source_notes),
                   'bridge', 'high', b.product_code
              FROM bridge_hcpcs_to_product_code b
              LEFT JOIN hcpcs_master h ON h.hcpcs_code = b.hcpcs_code
             WHERE b.device_category IS NOT NULL
        """)
        stats["hcpcs"] = cur.rowcount

        # ---------- 2. product_code ↔ device_category (from bridge) ----------
        cur.execute("""
            INSERT INTO device_code_crosswalk
                (device_category, code_type, code_value, display_name,
                 source, confidence, linked_product_code)
            SELECT DISTINCT b.device_category, 'product_code', b.product_code,
                   fc.device_name, 'bridge', 'high', b.product_code
              FROM bridge_hcpcs_to_product_code b
              LEFT JOIN fda_device_classification fc ON fc.product_code = b.product_code
             WHERE b.device_category IS NOT NULL
               AND b.product_code IS NOT NULL
        """)
        stats["product_code"] = cur.rowcount

        # ---------- 3. DRG ↔ device_category ----------
        cur.execute("""
            INSERT INTO device_code_crosswalk
                (device_category, code_type, code_value, display_name,
                 source, confidence)
            SELECT d.device_category, 'drg', d.drg_code,
                   d.rationale, 'drg_map',
                   CASE WHEN d.weight >= 0.9 THEN 'high'
                        WHEN d.weight >= 0.5 THEN 'medium'
                        ELSE 'low' END
              FROM drg_to_device_category d
        """)
        stats["drg"] = cur.rowcount

        # ---------- 4. K# ↔ device_category (via product_code) ----------
        cur.execute("""
            INSERT INTO device_code_crosswalk
                (device_category, code_type, code_value, display_name,
                 source, confidence, linked_brand, linked_manufacturer,
                 linked_product_code)
            SELECT DISTINCT b.device_category, 'k_number', k.k_number,
                   k.device_name, '510k', 'high',
                   k.device_name, k.applicant, k.product_code
              FROM fda_510k k
              JOIN bridge_hcpcs_to_product_code b ON b.product_code = k.product_code
             WHERE b.device_category IS NOT NULL
        """)
        stats["k_number"] = cur.rowcount

        # ---------- 5. UDI-DI / brand / manufacturer (from GUDID) ----------
        # Only rows whose product_code maps to a category in our bridge.
        cur.execute("""
            INSERT INTO device_code_crosswalk
                (device_category, code_type, code_value, display_name,
                 source, confidence, linked_brand, linked_manufacturer,
                 linked_product_code)
            SELECT DISTINCT b.device_category, 'udi_di', g.primary_di,
                   g.device_description, 'gudid', 'high',
                   g.brand_name, g.company_name, g.product_code
              FROM fda_gudid_devices g
              JOIN bridge_hcpcs_to_product_code b ON b.product_code = g.product_code
             WHERE b.device_category IS NOT NULL
               AND g.primary_di IS NOT NULL
        """)
        stats["udi_di"] = cur.rowcount

        # ---------- 6. Brand and manufacturer as separate rows (searchable) ----------
        cur.execute("""
            INSERT INTO device_code_crosswalk
                (device_category, code_type, code_value, display_name,
                 source, confidence, linked_manufacturer, linked_product_code)
            SELECT DISTINCT b.device_category, 'brand', g.brand_name,
                   g.brand_name, 'gudid', 'high',
                   g.company_name, g.product_code
              FROM fda_gudid_devices g
              JOIN bridge_hcpcs_to_product_code b ON b.product_code = g.product_code
             WHERE b.device_category IS NOT NULL
               AND g.brand_name IS NOT NULL
               AND g.brand_name <> ''
        """)
        stats["brand"] = cur.rowcount

        cur.execute("""
            INSERT INTO device_code_crosswalk
                (device_category, code_type, code_value, display_name,
                 source, confidence, linked_product_code)
            SELECT DISTINCT b.device_category, 'manufacturer', g.company_name,
                   g.company_name, 'gudid', 'high', g.product_code
              FROM fda_gudid_devices g
              JOIN bridge_hcpcs_to_product_code b ON b.product_code = g.product_code
             WHERE b.device_category IS NOT NULL
               AND g.company_name IS NOT NULL
               AND g.company_name <> ''
        """)
        stats["manufacturer"] = cur.rowcount

        # ---------- 7. GMDN Preferred Term (generic device nomenclature) ----------
        cur.execute("""
            INSERT INTO device_code_crosswalk
                (device_category, code_type, code_value, display_name,
                 source, confidence, linked_product_code)
            SELECT DISTINCT b.device_category, 'gmdn', g.gmdn_pt_name,
                   g.gmdn_pt_name, 'gudid', 'medium', g.product_code
              FROM fda_gudid_devices g
              JOIN bridge_hcpcs_to_product_code b ON b.product_code = g.product_code
             WHERE b.device_category IS NOT NULL
               AND g.gmdn_pt_name IS NOT NULL
               AND g.gmdn_pt_name <> ''
        """)
        stats["gmdn"] = cur.rowcount

    total = sum(stats.values())
    log.info("Crosswalk built: %s (total=%d)", stats, total)
    stats["total"] = total
    return stats
