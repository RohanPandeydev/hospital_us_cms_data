"""Client for CMS data-api/v1 endpoints (Medicare utilization, DMEPOS, etc.).

These datasets live at data.cms.gov/data-api/v1/dataset/{uuid}/data and use
size/offset pagination. Per CMS API docs v1.179.1:
  - Max page size is 5000
  - Exact filter:  filter[FIELD]=VALUE
  - Column select: column=a,b,c  (cuts payload size on wide tables)
  - /data-viewer        — returns data_file_url (bulk CSV download) + headers
  - /data-viewer/stats  — cheap total row count

Key insight: unfiltered queries on huge datasets (outpatient, DMEPOS) can time
out server-side. Iterating by state (~50 smaller queries) is far more reliable.
"""

import time
import logging
import requests

from . import config

log = logging.getLogger(__name__)


class CMSDataApiClient:
    BASE = "https://data.cms.gov/data-api/v1"
    MAX_PAGE_SIZE = 5000  # CMS hard cap

    def __init__(self, timeout=None, max_retries=None, page_size=5000):
        self.timeout = timeout or max(config.CMS_HTTP_TIMEOUT, 120)
        self.max_retries = max_retries or config.CMS_MAX_RETRIES
        self.page_size = min(page_size, self.MAX_PAGE_SIZE)
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "rp360-cms-ingest/0.1",
        })

    # --------------------------- HTTP ---------------------------

    def _get(self, url, params):
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (429, 500, 502, 503, 504):
                    log.warning("CMS data-api %s (attempt %d/%d)",
                                r.status_code, attempt, self.max_retries)
                    time.sleep(min(2 ** attempt, 30))
                    continue
                r.raise_for_status()
            except requests.RequestException as e:
                last_err = e
                log.warning("CMS data-api error (%d/%d): %s",
                            attempt, self.max_retries, e)
                time.sleep(min(2 ** attempt, 30))
        raise RuntimeError(f"CMS data-api failed: {last_err}")

    # --------------------------- metadata ---------------------------

    def stats(self, uuid):
        """Return {'total_rows': N, 'headers': [...], 'data_file_url': ..., 'csvFileSHA1': ...}.

        Hits /data-viewer/stats — cheap and doesn't scan the dataset.
        Returns {} on failure (caller treats as unknown).
        """
        url = f"{self.BASE}/dataset/{uuid}/data-viewer/stats"
        try:
            meta = self._get(url, params=None)
            # Response shape per docs v1.179.1
            out = meta.get("meta", meta) if isinstance(meta, dict) else {}
            return {
                "total_rows":     out.get("total_rows"),
                "headers":        out.get("headers") or [],
                "data_file_url":  out.get("data_file_url"),
                "data_file_name": out.get("data_file_name"),
                "csv_sha1":       (out.get("data_file_meta_data") or {}).get("csvFileSHA1"),
                "csv_size":       (out.get("data_file_meta_data") or {}).get("csvFileSize"),
            }
        except Exception as e:
            log.warning("stats() failed for %s: %s", uuid, e)
            return {}

    def csv_url(self, uuid):
        """Direct CSV download URL for bulk fallback (from /data-viewer)."""
        return self.stats(uuid).get("data_file_url")

    # --------------------------- pagination ---------------------------

    def iter_pages(self, uuid, limit=None, filter_by=None, columns=None):
        """Yield pages from /dataset/{uuid}/data.

        filter_by  — dict of {field: value} for server-side exact filters
        columns    — list of columns to return (reduces payload)
        """
        url = f"{self.BASE}/dataset/{uuid}/data"
        offset = 0
        yielded = 0

        base_params = {}
        if filter_by:
            for field, value in filter_by.items():
                base_params[f"filter[{field}]"] = value
        if columns:
            base_params["column"] = ",".join(columns)

        while True:
            page = self.page_size
            if limit is not None:
                remaining = limit - yielded
                if remaining <= 0:
                    return
                page = min(page, remaining)
            params = dict(base_params)
            params["size"] = page
            params["offset"] = offset
            data = self._get(url, params)
            # data-api returns a plain list (or wrapped in {"data": [...]})
            rows = data if isinstance(data, list) else data.get("data") or []
            if not rows:
                return
            yield rows
            yielded += len(rows)
            if len(rows) < page:
                return
            offset += len(rows)

    def iter_pages_per_value(self, uuid, field, values, limit=None, columns=None):
        """Iterate pages by looping over distinct values for a filter field.

        Use this when a full-table scan times out — e.g. iterate over every
        US state abbreviation against Rndrng_Prvdr_State_Abrvtn.
        """
        for v in values:
            log.info("data-api %s: filter %s=%s", uuid, field, v)
            for page in self.iter_pages(
                uuid, limit=limit,
                filter_by={field: v}, columns=columns,
            ):
                yield page

    def count_dataset(self, uuid):
        """Row count via /data-viewer/stats. Returns None if unavailable."""
        return self.stats(uuid).get("total_rows")
