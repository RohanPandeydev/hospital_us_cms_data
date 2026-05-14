"""Restructured BETOS Classification System (RBCS) ingester.

Source: data.cms.gov 'Restructured BETOS Classification System' (~10K HCPCS).
Maps each HCPCS code to a clinical category / subcategory / family —
e.g. cardiology-implants, neuro-stim. Useful for grouping HCPCS by
clinical lens rather than just by Medicare payment status.
"""

from __future__ import annotations

import json
import logging

from . import db
from .cms_dcat_helpers import resolve_csv_url, download_csv, iter_dict_rows

log = logging.getLogger(__name__)

DATASET_TITLE = "Restructured BETOS Classification System"
DATASET = {"id": "rbcs_taxonomy", "name": DATASET_TITLE}

COLS = [
    "hcpcs_code", "betos_code", "betos_description",
    "rbcs_category", "rbcs_subcategory", "rbcs_family",
    "source_url", "raw",
]
COL_TYPES = [
    "String", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "String",
]


def ingest(batch_size: int = 5000) -> tuple[int, int]:
    url = resolve_csv_url(DATASET_TITLE)
    payload = download_csv(url)
    rows: list[tuple] = []
    for r in iter_dict_rows(payload):
        hcpcs = (r.get("HCPCS_Cd") or r.get("hcpcs_cd") or "").strip()
        if not hcpcs:
            continue
        rows.append((
            hcpcs,
            r.get("RBCS_Id") or None,
            r.get("RBCS_Cat_Desc") or None,
            r.get("RBCS_Cat") or None,
            r.get("RBCS_Subcat_Desc") or r.get("RBCS_Cat_Subcat") or None,
            r.get("RBCS_Family_Desc") or None,
            url,
            json.dumps(r, ensure_ascii=False),
        ))

    # Dedup by hcpcs_code (some codes get multiple RBCS_Ids over time;
    # the newest assignment wins)
    seen: dict[str, tuple] = {}
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
                conn.insert("betos_crosswalk", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("RBCS BETOS upserted %d rows", upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    f, u = ingest()
    print(f"Done. fetched={f}, upserted={u}")
