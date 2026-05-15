"""Medicare FFS Public Provider Enrollment (PPEF) ingester.

Source: data.cms.gov 'Medicare Fee-For-Service Public Provider Enrollment'
(~2M rows / ~150 MB). The full set of providers enrolled to bill Medicare
FFS, with NPI, provider type, state, and basic identity.
"""

from __future__ import annotations

import json
import logging

from . import db
from .cms_dcat_helpers import resolve_csv_url, download_csv, iter_dict_rows

log = logging.getLogger(__name__)

DATASET_TITLE = "Medicare Fee-For-Service  Public Provider Enrollment"  # double space in DCAT
DATASET = {"id": "ffs_enrollment", "name": DATASET_TITLE}

COLS = [
    "npi", "pecos_asct_cntl_id", "enrollment_id",
    "provider_type_cd", "provider_type_desc",
    "state_cd", "first_name", "last_name", "org_name", "gndr_sw",
    "source_url", "raw",
]
COL_TYPES = [
    "String", "Nullable(String)", "Nullable(String)",
    "String", "Nullable(String)",
    "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)", "Nullable(String)",
    "Nullable(String)", "String",
]


def ingest(batch_size: int = 20000, limit: int | None = None) -> tuple[int, int]:
    # The dataset title has a quirky double-space in the DCAT catalog;
    # try both variants.
    try:
        url = resolve_csv_url(DATASET_TITLE)
    except RuntimeError:
        url = resolve_csv_url("Medicare Fee-For-Service Public Provider Enrollment")
    payload = download_csv(url)
    rows: list[tuple] = []
    for r in iter_dict_rows(payload):
        npi = (r.get("NPI") or "").strip()
        if not npi:
            continue
        rows.append((
            npi,
            r.get("PECOS_ASCT_CNTL_ID") or None,
            r.get("ENRLMT_ID") or None,
            r.get("PROVIDER_TYPE_CD") or "",
            r.get("PROVIDER_TYPE_DESC") or None,
            r.get("STATE_CD") or None,
            r.get("FIRST_NAME") or None,
            r.get("LAST_NAME") or None,
            r.get("ORG_NAME") or None,
            r.get("GNDR_SW") or None,
            url,
            json.dumps(r, ensure_ascii=False),
        ))
        if limit and len(rows) >= limit:
            break

    # Dedup by (npi, provider_type_cd) — an NPI can enroll under multiple types
    seen: dict[tuple[str, str], tuple] = {}
    for r in rows:
        seen[(r[0], r[3])] = r
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
                conn.insert("medicare_ffs_enrollment", chunk,
                            column_names=COLS, column_type_names=COL_TYPES)
                upserted += len(chunk)
                if upserted % 100000 == 0:
                    log.info("FFS enrollment progress: %d rows", upserted)
            log.info("FFS enrollment upserted %d rows", upserted)
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
