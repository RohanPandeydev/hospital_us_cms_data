"""Thin client around the CMS Provider Data datastore (Socrata-style) API."""

import time
import logging
import requests

from . import config

log = logging.getLogger(__name__)


class CMSClient:
    def __init__(self, base_url=None, page_size=None, timeout=None, max_retries=None):
        self.base_url = (base_url or config.CMS_API_BASE).rstrip("/")
        self.page_size = page_size or config.CMS_PAGE_SIZE
        self.timeout = timeout or config.CMS_HTTP_TIMEOUT
        self.max_retries = max_retries or config.CMS_MAX_RETRIES
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "rp360-cms-ingest/0.1",
        })

    def _get(self, url, params):
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
                if 500 <= r.status_code < 600 or r.status_code == 429:
                    log.warning(
                        "CMS API %s on %s (attempt %d/%d)",
                        r.status_code, url, attempt, self.max_retries,
                    )
                    time.sleep(min(2 ** attempt, 30))
                    continue
                r.raise_for_status()
            except requests.RequestException as e:
                last_err = e
                log.warning(
                    "CMS API error %s (attempt %d/%d): %s",
                    url, attempt, self.max_retries, e,
                )
                time.sleep(min(2 ** attempt, 30))
        raise RuntimeError(f"CMS API failed after {self.max_retries} attempts: {last_err}")

    def count_dataset(self, dataset_id):
        """Best-effort total row count. Returns None if unavailable."""
        url = f"{self.base_url}/{dataset_id}/0"
        try:
            payload = self._get(url, {"limit": 1, "offset": 0, "count": "true"})
        except Exception:
            return None
        for key in ("count", "total", "totalCount", "rowCount"):
            if key in payload and isinstance(payload[key], int):
                return payload[key]
        return None

    def iter_pages(self, dataset_id, limit=None, base_url=None):
        """Yield successive pages (lists of rows) from a dataset."""
        url = f"{(base_url or self.base_url).rstrip('/')}/{dataset_id}/0"
        offset = 0
        yielded = 0
        while True:
            page_size = self.page_size
            if limit is not None:
                remaining = limit - yielded
                if remaining <= 0:
                    return
                page_size = min(page_size, remaining)

            payload = self._get(url, {"limit": page_size, "offset": offset})
            rows = payload.get("results") or []
            if not rows:
                return
            yield rows
            yielded += len(rows)
            if limit is not None and yielded >= limit:
                return
            if len(rows) < page_size:
                return
            offset += len(rows)

    def iter_dataset(self, dataset_id, limit=None, base_url=None):
        for page in self.iter_pages(dataset_id, limit=limit, base_url=base_url):
            for row in page:
                yield row
