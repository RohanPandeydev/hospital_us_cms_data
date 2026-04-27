"""TinyFish AI client — browser-automation agent for unstructured web data.

Use when the source is behind a JS SPA, an HTML portal, or a login — anything
openFDA / CMS APIs don't expose. Typical candidates for this project:

  * EUDAMED         — EU medical device adverse-event database
  * FDA 483 letters — manufacturer inspection reports
  * State quality   — NY SPARCS, CA OSHPD device-level outcomes

SSE protocol (v1 /automation/run-sse):
    POST /v1/automation/run-sse
    headers: X-API-Key, Content-Type: application/json
    body:    {"url": str, "goal": str, ...}
    response: text/event-stream
        data: {"type":"STARTED",      "run_id":...}
        data: {"type":"STREAMING_URL", "streaming_url":...}
        data: {"type":"COMPLETE",      "status":"COMPLETED", "result":{...}}
        data: {"type":"HEARTBEAT",     ...}   # arrives after COMPLETE too

We stream events and return the first COMPLETE we see (the run's final
result). Callers that want every event can iterate `stream_run()`.
"""

from __future__ import annotations

import json
import logging
from typing import Iterator, Optional

import requests

from . import config

log = logging.getLogger(__name__)


class TinyFishError(RuntimeError):
    """Raised when the API rejects the call or the run itself fails."""


def _headers() -> dict:
    if not config.TINYFISH_API_KEY:
        raise TinyFishError(
            "TINYFISH_API_KEY is not set. Add it to .env before calling TinyFish."
        )
    return {
        "X-API-Key": config.TINYFISH_API_KEY,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


def stream_run(url: str, goal: str,
               extra: Optional[dict] = None,
               timeout: Optional[int] = None) -> Iterator[dict]:
    """Yield every SSE event dict from a single automation run.

    Low-level primitive — most callers should use `run()` instead, which
    collapses the stream to the single final-result dict. Use this one when
    you need progress signals (streaming_url, intermediate thought-events).
    """
    body = {"url": url, "goal": goal}
    if extra:
        body.update(extra)

    endpoint = f"{config.TINYFISH_BASE_URL.rstrip('/')}/v1/automation/run-sse"
    r = requests.post(endpoint, headers=_headers(), json=body,
                      stream=True, timeout=timeout or config.TINYFISH_TIMEOUT)
    if r.status_code != 200:
        raise TinyFishError(
            f"TinyFish {r.status_code}: {r.text[:400]}"
        )
    for raw in r.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if not payload:
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            log.warning("non-JSON SSE line: %r", payload[:120])


def run(url: str, goal: str,
        extra: Optional[dict] = None,
        timeout: Optional[int] = None,
        max_retries: int = 2) -> dict:
    """Run one automation and return the final result.

    Retries on transient SSE failures (ConnectionResetError, ChunkedEncoding
    errors, requests.exceptions.RequestException) up to `max_retries` times
    before giving up. Server-reported ERROR events are NOT retried — those
    are deterministic agent failures.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        run_id = None
        try:
            for ev in stream_run(url, goal, extra=extra, timeout=timeout):
                t = ev.get("type")
                if t == "STARTED":
                    run_id = ev.get("run_id")
                    log.info("tinyfish run_id=%s  url=%s", run_id, url)
                elif t == "COMPLETE":
                    if ev.get("status") and ev["status"] != "COMPLETED":
                        raise TinyFishError(
                            f"tinyfish run {run_id} ended with status="
                            f"{ev['status']}: {str(ev)[:400]}"
                        )
                    return ev
                elif t == "ERROR":
                    raise TinyFishError(
                        f"tinyfish run {run_id} ERROR: {ev.get('error') or ev}"
                    )
            raise TinyFishError(
                f"tinyfish run {run_id}: stream ended without COMPLETE event"
            )
        except (requests.RequestException, requests.exceptions.ChunkedEncodingError) as e:
            last_exc = e
            log.warning("tinyfish stream broke (%d/%d) for %s: %s",
                        attempt + 1, max_retries + 1, url, str(e)[:200])
            if attempt < max_retries:
                continue
            raise TinyFishError(
                f"tinyfish stream failed after {max_retries + 1} attempts: {e}"
            )
    raise TinyFishError(f"tinyfish unreachable: {last_exc}")
