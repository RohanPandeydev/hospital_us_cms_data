"""openFDA API client for device adverse events and related endpoints.

openFDA uses api.fda.gov with optional api_key for higher rate limits.
Pagination: search=... & limit=1000 & skip=N ; skip is hard-capped at 25,000
per unauthenticated search path, so larger ingests need date-range chunking.
"""

import time
import logging
import requests

from . import config

log = logging.getLogger(__name__)

MAX_SKIP = 25000  # openFDA hard limit on skip


class FDAClient:
    def __init__(self, api_key=None, base_url=None, timeout=None, max_retries=None):
        self.api_key = api_key if api_key is not None else config.FDA_API_KEY
        self.base_url = (base_url or config.FDA_API_BASE).rstrip("/")
        self.timeout = timeout or config.FDA_HTTP_TIMEOUT
        self.max_retries = max_retries or config.FDA_MAX_RETRIES
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "rp360-fda-ingest/0.1",
        })

    def _get(self, url, params):
        p = dict(params)
        if self.api_key:
            p["api_key"] = self.api_key
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self.session.get(url, params=p, timeout=self.timeout)
                if r.status_code == 404:
                    # openFDA returns 404 when the search yields no results
                    return {"results": [], "meta": {}}
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (429, 500, 502, 503, 504):
                    log.warning("openFDA %s (attempt %d/%d)", r.status_code, attempt, self.max_retries)
                    time.sleep(min(2 ** attempt, 30))
                    continue
                r.raise_for_status()
            except requests.RequestException as e:
                last_err = e
                log.warning("openFDA error (%d/%d): %s", attempt, self.max_retries, e)
                time.sleep(min(2 ** attempt, 30))
        raise RuntimeError(f"openFDA failed after {self.max_retries} attempts: {last_err}")

    def count(self, endpoint, search=""):
        """Ask openFDA how many records match a search. Uses count=_exists_:report_number trick."""
        url = f"{self.base_url}{endpoint}"
        try:
            # A trivial count query — returns one bucket
            data = self._get(url, {
                "search": search or "_exists_:report_number",
                "count": "event_type.exact",
                "limit": 1,
            })
            # Sum all buckets for a total
            total = sum(b.get("count", 0) for b in data.get("results", []))
            return total or None
        except Exception:
            return None

    def iter_udi(self, search="", limit=None):
        """Paginate GUDID — FDA Unique Device Identifier database (/device/udi).

        Each row has brand_name, company_name, device_description,
        product_codes[], identifiers[] (UDI-DIs), and more. Used to
        auto-populate bridge_hcpcs_to_product_code via text matching.
        """
        url = f"{self.base_url}/device/udi.json"
        skip = 0
        yielded = 0
        while True:
            if skip >= MAX_SKIP:
                log.info("openFDA skip limit (%d) reached — stopping", MAX_SKIP)
                return
            page = config.FDA_PAGE_SIZE
            if limit is not None:
                remaining = limit - yielded
                if remaining <= 0:
                    return
                page = min(page, remaining)
            page = min(page, MAX_SKIP - skip)

            params = {"limit": page, "skip": skip}
            if search:
                params["search"] = search
            data = self._get(url, params)
            rows = data.get("results") or []
            if not rows:
                return
            for row in rows:
                yield row
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            skip += len(rows)
            if len(rows) < page:
                return

    def iter_events(self, search="", limit=None):
        """Paginate MAUDE device events. search uses openFDA Lucene-style syntax."""
        url = f"{self.base_url}/device/event.json"
        skip = 0
        yielded = 0
        while True:
            if skip >= MAX_SKIP:
                log.info("openFDA skip limit (%d) reached — stopping", MAX_SKIP)
                return
            page = config.FDA_PAGE_SIZE
            if limit is not None:
                remaining = limit - yielded
                if remaining <= 0:
                    return
                page = min(page, remaining)
            page = min(page, MAX_SKIP - skip)

            params = {"limit": page, "skip": skip}
            if search:
                params["search"] = search
            data = self._get(url, params)
            rows = data.get("results") or []
            if not rows:
                return
            for row in rows:
                yield row
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            skip += len(rows)
            if len(rows) < page:
                return
