"""Medicare Order & Referring NPI ingester.

Source: data.cms.gov 'Order and Referring' dataset (~1M rows / ~50 MB).
Columns: NPI, LAST_NAME, FIRST_NAME, PARTB, DME, HHA, PMD, HOSPICE
Flags are Y/N indicating the NPI is eligible to order/refer for that
service category.
"""

from __future__ import annotations

import json
import logging

from . import db
from .cms_dcat_helpers import resolve_csv_url, download_csv, iter_dict_rows

log = logging.getLogger(__name__)

DATASET_TITLE = "Order and Referring"
DATASET = {"id": "order_referring", "name": DATASET_TITLE}

COLS = [
    "npi", "last_name", "first_name",
    "partb_eligible", "dme_eligible", "hha_eligible",
    "pmd_eligible", "hospice_eligible",
    "source_url", "raw",
]
COL_TYPES = [
    "String", "Nullable(String)", "Nullable(String)",
    "Nullable(UInt8)", "Nullable(UInt8)", "Nullable(UInt8)",
    "Nullable(UInt8)", "Nullable(UInt8)",
    "Nullable(String)", "String",
]


def _flag(v):
    if v is None:
        return None
    s = str(v).strip().upper()
    if s == "Y":
        return 1
    if s == "N":
        return 0
    return None


def ingest(batch_size: int = 10000, limit: int | None = None) -> tuple[int, int]:
    url = resolve_csv_url(DATASET_TITLE)
    payload = download_csv(url)
    rows: list[tuple] = []
    for r in iter_dict_rows(payload):
        npi = (r.get("NPI") or "").strip()
        if not npi:
            continue
        rows.append((
            npi,
            r.get("LAST_NAME") or None,
            r.get("FIRST_NAME") or None,
            _flag(r.get("PARTB")),
            _flag(r.get("DME")),
            _flag(r.get("HHA")),
            _flag(r.get("PMD")),
            _flag(r.get("HOSPICE")),
            url,
            json.dumps(r, ensure_ascii=False),
        ))
        if limit and len(rows) >= limit:
            break

    # Dedup by NPI
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
                conn.insert("order_referring_npi", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
            log.info("Order&Referring upserted %d rows", upserted)
        except Exception as e:
            status = "error"
            error = repr(e)
            log.exception("Order&Referring ingest failed")
            raise
        finally:
            db.finish_ingest_log(conn, handle, fetched, upserted, status, error)
    return fetched, upserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    f, u = ingest()
    print(f"Done. fetched={f}, upserted={u}")
