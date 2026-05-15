"""Medicare Provider and Supplier Taxonomy Crosswalk ingester.

Source: data.cms.gov 'Medicare Provider and Supplier Taxonomy Crosswalk'
(~900 rows). Maps CMS Medicare specialty codes to NUCC taxonomy codes.
"""

from __future__ import annotations

import json
import logging

from . import db
from .cms_dcat_helpers import resolve_csv_url, download_csv, iter_dict_rows

log = logging.getLogger(__name__)

DATASET_TITLE = "Medicare Provider and Supplier Taxonomy Crosswalk"
DATASET = {"id": "taxonomy_crosswalk", "name": DATASET_TITLE}

COLS = [
    "medicare_specialty_code", "medicare_provider_supplier_type",
    "provider_taxonomy_code", "provider_taxonomy_description",
    "source_url", "raw",
]
COL_TYPES = [
    "String", "Nullable(String)", "String",
    "Nullable(String)", "Nullable(String)", "String",
]


def _find_col(row: dict, *keys: str):
    """Headers in this CSV have erratic whitespace/case — match loosely."""
    keys_lower = [k.lower() for k in keys]
    for col, v in row.items():
        col_lc = (col or "").lower().strip()
        if any(k in col_lc for k in keys_lower):
            return v
    return None


def ingest(batch_size: int = 1000) -> tuple[int, int]:
    url = resolve_csv_url(DATASET_TITLE)
    payload = download_csv(url)
    rows: list[tuple] = []
    for r in iter_dict_rows(payload):
        spec_code = (_find_col(r, "medicare specialty code") or "").strip()
        tax_code = (_find_col(r, "provider taxonomy code") or "").strip()
        if not spec_code or not tax_code:
            continue
        rows.append((
            spec_code,
            _find_col(r, "medicare provider/supplier type", "supplier type description"),
            tax_code,
            _find_col(r, "provider taxonomy description"),
            url,
            json.dumps(r, ensure_ascii=False),
        ))

    # Dedup by (spec_code, taxonomy_code)
    seen: dict[tuple[str, str], tuple] = {}
    for r in rows:
        seen[(r[0], r[2])] = r
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
                conn.insert("npi_taxonomy_crosswalk", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("Taxonomy crosswalk upserted %d rows", upserted)
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
