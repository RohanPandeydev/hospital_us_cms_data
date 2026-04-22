"""Main ingestion pipeline."""

import logging
from tqdm import tqdm

from . import db
from .cms_client import CMSClient
from .cms_data_api_client import CMSDataApiClient
from .fda_client import FDAClient
from .datasets import DATASETS


log = logging.getLogger(__name__)


# 50 states + DC + US territories that show up in CMS provider data. Used to
# split large data-api queries into per-state shards that don't time out.
US_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH",
    "NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT",
    "VT","VA","WA","WV","WI","WY","PR","VI","GU","AS","MP",
]


def _upsert_page(conn, dataset, rows):
    kind = dataset["kind"]
    if kind == "hospital_master":
        return db.upsert_hospitals(conn, rows)
    if kind == "facility_measure":
        return db.upsert_facility_measures(conn, dataset["id"], rows)
    if kind == "state_measure":
        return db.upsert_state_measures(conn, dataset["id"], rows)
    if kind == "national_measure":
        return db.upsert_national_measures(conn, dataset["id"], rows)
    if kind == "wide_facility":
        return db.upsert_wide_facility(conn, dataset["id"], rows)
    if kind == "footnote_crosswalk":
        return db.upsert_footnote_crosswalk(conn, rows)
    if kind == "fda_events":
        return db.upsert_fda_events(conn, rows)
    if kind == "fda_gudid":
        return db.upsert_gudid(conn, rows)
    if kind == "cms_summary":
        return db.upsert_cms_summary(conn, dataset["id"], rows)
    if kind == "open_payments":
        return db.upsert_open_payments(conn, dataset["id"], rows)
    raise ValueError(f"Unknown dataset kind: {kind}")


def _make_client(dataset):
    source = dataset.get("source", "cms_provider_data")
    if source in ("openfda", "openfda_udi"):
        return FDAClient()
    if source == "cms_data_api":
        return CMSDataApiClient()
    # open_payments reuses CMSClient (DKAN shape) with a different base URL
    return CMSClient()


def _iter_pages(client, dataset, limit):
    """Dispatch to the right pagination method based on dataset source."""
    source = dataset.get("source", "cms_provider_data")
    if source == "openfda":
        batch = []
        for row in client.iter_events(limit=limit):
            batch.append(row)
            if len(batch) >= 500:
                yield batch
                batch = []
        if batch:
            yield batch
    elif source == "openfda_udi":
        batch = []
        for row in client.iter_udi(limit=limit):
            batch.append(row)
            if len(batch) >= 500:
                yield batch
                batch = []
        if batch:
            yield batch
    elif source == "cms_data_api":
        filter_field = dataset.get("filter_field")
        columns = dataset.get("columns")
        if filter_field:
            # Per-state iteration: avoids server-side timeouts on big datasets
            for page in client.iter_pages_per_value(
                dataset["uuid"], filter_field, US_STATES,
                limit=limit, columns=columns,
            ):
                yield page
        else:
            for page in client.iter_pages(
                dataset["uuid"], limit=limit, columns=columns,
            ):
                yield page
    elif source == "open_payments":
        # Open Payments uses the same DKAN shape but different host
        op_base = "https://openpaymentsdata.cms.gov/api/1/datastore/query"
        for page in client.iter_pages(dataset["uuid"], limit=limit, base_url=op_base):
            yield page
    else:
        for page in client.iter_pages(dataset["id"], limit=limit):
            yield page


def _count_total(client, dataset, limit):
    if limit is not None:
        return limit
    source = dataset.get("source", "cms_provider_data")
    if source == "openfda":
        return client.count("/device/event.json")
    if source == "cms_data_api":
        # Uses /data-viewer/stats — cheap, authoritative row count
        return client.count_dataset(dataset["uuid"])
    return client.count_dataset(dataset["id"])


def ingest_dataset(dataset, limit=None, client=None):
    # Special-case the HCPCS master — it's a static ZIP, not an API
    if dataset.get("source") == "cms_hcpcs_static":
        from . import hcpcs_ingest
        log.info("=== %s (%s) ===", dataset["id"], dataset["name"])
        fetched, upserted = hcpcs_ingest.ingest(limit=limit)
        return fetched, upserted, "success"

    client = client or _make_client(dataset)
    log.info("=== %s (%s) ===", dataset["id"], dataset["name"])
    conn = db.connect()
    log_id = db.start_ingest_log(conn, dataset)
    fetched = 0
    upserted = 0
    status = "success"
    error = None
    pbar = None
    try:
        total = _count_total(client, dataset, limit)
        pbar = tqdm(desc=dataset["id"], unit="row", total=total)
        for page in _iter_pages(client, dataset, limit):
            try:
                upserted += _upsert_page(conn, dataset, page)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            fetched += len(page)
            pbar.update(len(page))
        log.info("%s done  fetched=%d  upserted=%d", dataset["id"], fetched, upserted)
    except KeyboardInterrupt:
        status = "aborted"
        error = "KeyboardInterrupt"
        log.warning("%s aborted by user (fetched=%d)", dataset["id"], fetched)
        raise
    except Exception as e:
        status = "error"
        error = repr(e)
        log.exception("Ingest failed for %s", dataset["id"])
    finally:
        if pbar is not None:
            pbar.close()
        try:
            db.finish_ingest_log(conn, log_id, fetched, upserted, status, error)
        finally:
            conn.close()
    return fetched, upserted, status


def ingest_all(dataset_ids=None, limit=None):
    targets = DATASETS
    if dataset_ids:
        targets = [d for d in DATASETS if d["id"] in dataset_ids]
        missing = set(dataset_ids) - {d["id"] for d in targets}
        if missing:
            log.warning("Unknown dataset ids skipped: %s", missing)
    results = {}
    interrupted = False
    for ds in targets:
        if interrupted:
            results[ds["id"]] = {"skipped": True}
            continue
        try:
            fetched, upserted, status = ingest_dataset(ds, limit=limit)
            results[ds["id"]] = {
                "fetched": fetched, "upserted": upserted, "status": status,
            }
        except KeyboardInterrupt:
            results[ds["id"]] = {"status": "aborted"}
            interrupted = True
        except Exception as e:
            results[ds["id"]] = {"status": "error", "error": repr(e)}
    return results
