"""Extend bridge_hcpcs_to_product_code for DRG-only device categories.

Our original bridge was built around outpatient HCPCS codes. The new
DRG-derived categories (hip_knee_implant, cardiac_valve, spinal_fusion_device,
heart_assist_device, cabg_conduit) don't have a clean 1-to-1 HCPCS mapping —
they're inpatient-bundled devices. But they do have well-known FDA product
codes, and MAUDE/recalls are joined to bridge.product_code.

This module adds synthetic bridge rows keyed on 'DRG-xxx' HCPCS placeholders
so the MAUDE/recall aggregators can follow product_code → device_category
for these new categories too.

Product-code selection is driven by FDA device-classification regex rules
vetted against the fda_device_classification table (device_name + specialty).
"""

from __future__ import annotations
import logging
import psycopg2.extras

log = logging.getLogger(__name__)


# Each rule: (device_category, name_regex, specialty_codes, extra_product_codes)
# specialty codes: CV=Cardiovascular, OR=Orthopedic, NE=Neurology, GU=General-Plastic
_RULES = [
    # Hip and knee joint implants (all classes, since we include hemi-prosthesis)
    ("hip_knee_implant",
     r"(prosthesis|implant).*(knee|hip|femoral|acetab|patell|tibia)|knee.*(prosthes|polymer)|hip.*(prosthes|polymer)",
     ["OR"], []),

    # Cardiac valves — both surgical and transcatheter
    ("cardiac_valve",
     r"valve,.*(replacement|prosthesis|heart)|heart valve|bioprosth.*(aortic|mitral|tricuspid|pulmonic)|transcatheter aortic valve|annuloplasty",
     ["CV"], ["DYE", "LWS", "LWR", "NPT", "MWI", "NPV"]),

    # CABG / coronary bypass devices (grafts, conduit, anastomosis aids)
    ("cabg_conduit",
     r"coronary.*(bypass|graft|conduit|anastomos)|cardiopulmonary bypass|vein graft",
     ["CV"], ["DSR", "KRD", "KRE"]),

    # Spinal fusion hardware (rods, cages, screws, plates, interbody)
    ("spinal_fusion_device",
     r"spinal.*(fusion|fixation|rod|screw|plate|cage|interbody|pedicle)|vertebral.*(body|replacement)|intervertebral.*(fusion|device|spacer)",
     ["OR", "NE"], ["MAX", "KWQ", "MNH", "KWP", "KCU", "KWW", "OVD"]),

    # Heart-assist devices (VADs, total-artificial-heart, impella)
    ("heart_assist_device",
     r"ventricular assist|heart.*(assist|pump)|cardiac (pump|assist)|total artificial heart|impella",
     ["CV"], ["DSQ", "LOZ", "NPS", "NPT"]),

    # Neurostimulators (spinal cord, DBS, vagus nerve)
    ("neurostim_implant",
     r"spinal cord stimulat|deep brain stimulat|neurostimulat|vagus nerve stimulat|sacral nerve stimulat",
     ["NE"], ["GZF", "MHY", "LGW", "LYJ", "EZW"]),
]


def extend_bridge(conn) -> dict:
    """Add product_code rows to bridge_hcpcs_to_product_code for DRG-only
    device categories. Returns summary counts."""
    stats: dict[str, int] = {}
    with conn.cursor() as cur:
        for category, name_rx, specs, extras in _RULES:
            # Find product codes that match the name regex OR specialty OR
            # are in the curated extras list.
            cur.execute("""
                SELECT DISTINCT product_code
                  FROM fda_device_classification
                 WHERE (device_name ~* %s
                        OR medical_specialty = ANY(%s))
                   AND product_code IS NOT NULL
                   AND product_code <> ''
            """, (name_rx, specs))
            codes = {r[0] for r in cur.fetchall()}
            codes.update(extras)

            # Synthetic HCPCS placeholder — keyed by category so we don't
            # collide with real bridge rows.
            synthetic_hcpcs = f"DRG-BUNDLE-{category.upper()[:20]}"
            rows = [
                (synthetic_hcpcs, pc, category, "drg_bundle", "medium",
                 f"auto-extended via FDA classification; DRG-bundled device category")
                for pc in codes
            ]
            if rows:
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO bridge_hcpcs_to_product_code
                        (hcpcs_code, product_code, device_category,
                         match_method, confidence, source_notes)
                    VALUES %s
                    ON CONFLICT (hcpcs_code, product_code) DO UPDATE
                        SET device_category = EXCLUDED.device_category,
                            source_notes    = EXCLUDED.source_notes
                """, rows)
                stats[category] = len(rows)
    log.info("Bridge extended: %s", stats)
    stats["total_added"] = sum(stats.values())
    return stats
