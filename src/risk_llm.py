"""Groq LLM helpers for the risk-intelligence pipeline.

Currently used for one job: classifying recall product_descriptions into the
fixed bridge `device_category` vocabulary when the regex tagger can't get a
match. Designed so callers can pass a list of strings and get back a list of
{category | None} answers in one go (per-item parsing, single API round-trip
per batch).

Reads GROQ_API_KEY / GROQ_MODEL from .env. If the key is unset, all classify
calls return None so callers can fall back transparently.

Rate-limit handling:
  The Groq SDK already retries 429s with backoff, but TPM (tokens/minute)
  limits on the large 70B model still force long waits on big ingests.
  We layer three things on top:
    1. Primary → fallback model chain. First sustained 429 on the primary
       swaps us to `llama-3.1-8b-instant` (much higher TPM ceiling, same
       JSON-mode, sufficient quality for short categorisation prompts).
    2. Inter-batch pacing: a configurable sleep between batches keeps us
       under TPM for long runs.
    3. Hard budget: after N consecutive failures on both models, we stop
       and return None for the rest — regex hits still land; the LLM pass
       just contributes nothing for that call instead of blocking forever.
"""

import json
import logging
import os
import time
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_API_KEY  = os.getenv("GROQ_API_KEY", "")
_MODEL    = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
_FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")
_TIMEOUT  = float(os.getenv("GROQ_TIMEOUT", "60"))
# Inter-batch sleep in seconds. 1.2s × 25 items/batch keeps the 70B model
# under its free-tier TPM ceiling for most recall runs.
_BATCH_SLEEP = float(os.getenv("GROQ_BATCH_SLEEP", "1.2"))
# Stop trying the LLM entirely after this many consecutive batch failures.
_MAX_CONSECUTIVE_FAILS = int(os.getenv("GROQ_MAX_CONSECUTIVE_FAILS", "5"))

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not _API_KEY:
        return None
    try:
        # Don't let GROQ_BASE_URL from .env leak into the SDK — its default
        # (https://api.groq.com) already routes /openai/v1/chat/completions.
        # Passing the env value causes path duplication and 404s.
        import os as _os
        _os.environ.pop("GROQ_BASE_URL", None)
        from groq import Groq
        # max_retries=2 keeps the SDK's built-in 429 backoff short — our own
        # loop below handles the longer "give up and fall back" decision.
        _client = Groq(api_key=_API_KEY, timeout=_TIMEOUT, max_retries=2)
        return _client
    except Exception as e:
        log.warning("Groq client init failed: %s", e)
        return None


def is_enabled() -> bool:
    return _get_client() is not None


def _is_rate_limit(err: Exception) -> bool:
    """Detect a 429 / rate-limit from the Groq SDK."""
    msg = str(err).lower()
    if "429" in msg or "rate_limit" in msg or "rate limit" in msg or "too many requests" in msg:
        return True
    status = getattr(err, "status_code", None) or getattr(err, "status", None)
    return status == 429


# ------------------------------------------------------------------
# Recall device-category classifier
# ------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a medical-device categorization assistant.

You will receive a JSON list of recall descriptions. For each one, classify it
into exactly ONE of the categories from this fixed vocabulary, or "none" if no
category fits. Return ONLY a JSON array of strings — one per input item, same
order. No prose, no markdown, no extra keys.

VOCABULARY:
{vocab}

Rules:
- If the description clearly matches a category by device type or function, return that category id.
- If the description is too generic, ambiguous, or about software/labeling/packaging only, return "none".
- Return exactly N strings for N inputs, in input order.
- Never invent categories that aren't in the vocabulary."""


def _call_once(client, model: str, sys_msg: str, user_msg: str,
               expected_len: int, valid: set) -> List[Optional[str]]:
    """Single Groq call. Raises on rate-limit so callers can switch models."""
    # 4096 tokens of output is plenty for 10-25 short category strings; we
    # hit JSON-mode "json_validate_failed" with max_tokens=2000 when the
    # smaller 8B model was verbose. Cheap to leave generous.
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": sys_msg},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        max_tokens=4096,
    )
    content = resp.choices[0].message.content or ""
    return _parse_response(content, expected_len, valid)


def classify_recall_descriptions(
    descriptions: List[str],
    vocabulary: List[str],
    batch_size: int = 10,
    batch_sleep: Optional[float] = None,
) -> List[Optional[str]]:
    """Classify each description into one of `vocabulary` or None.

    Returns a list of the same length as `descriptions`.

    Fallback behavior (designed so the regex "manual" pass is never blocked):
      * If the Groq client isn't enabled → returns [None] * len(descriptions).
      * On a rate-limit or SDK error, we swap from GROQ_MODEL to
        GROQ_FALLBACK_MODEL (default llama-3.1-8b-instant) for subsequent
        batches — the 8B model has a much higher TPM ceiling.
      * After MAX_CONSECUTIVE_FAILS failed batches on BOTH models, we stop
        calling the API and pad the rest of the result with None. The caller
        keeps whatever regex tagged already; LLM contribution is additive.
    """
    if not descriptions:
        return []
    client = _get_client()
    if client is None:
        log.info("Groq disabled — regex-only tagging")
        return [None] * len(descriptions)

    vocab_str = "\n".join(f"- {v}" for v in vocabulary) + '\n- none'
    sys_msg = _SYSTEM_PROMPT.format(vocab=vocab_str)
    valid = set(vocabulary) | {"none"}
    sleep_s = _BATCH_SLEEP if batch_sleep is None else batch_sleep

    out: List[Optional[str]] = []
    current_model = _MODEL
    swapped = False      # have we fallen back to the smaller model yet?
    consecutive_fails = 0
    giving_up = False

    total_batches = (len(descriptions) + batch_size - 1) // batch_size
    for bi, i in enumerate(range(0, len(descriptions), batch_size), start=1):
        chunk = descriptions[i:i + batch_size]

        if giving_up:
            out.extend([None] * len(chunk))
            continue

        user_msg = json.dumps(chunk, ensure_ascii=False)
        parsed: Optional[List[Optional[str]]] = None
        last_err: Optional[Exception] = None

        # Try the current model; on 429 fall back once to the smaller model.
        for attempt_model in ([current_model] if swapped else [current_model, _FALLBACK_MODEL]):
            try:
                parsed = _call_once(client, attempt_model, sys_msg, user_msg,
                                     len(chunk), valid)
                if attempt_model != current_model:
                    log.info("Groq: switched to fallback model %s after 429 on %s",
                             attempt_model, current_model)
                    current_model = attempt_model
                    swapped = True
                break
            except Exception as e:
                last_err = e
                if _is_rate_limit(e):
                    log.warning(
                        "Groq rate-limit on model=%s (batch %d/%d, %d items); "
                        "falling back to %s",
                        attempt_model, bi, total_batches, len(chunk), _FALLBACK_MODEL,
                    )
                    # Short cooldown so the next request isn't instantly rejected
                    time.sleep(2.0)
                    continue
                # Non-rate-limit: don't cascade-switch, just fail this batch.
                log.warning("Groq batch failed model=%s (%d items): %s",
                             attempt_model, len(chunk), e)
                break

        if parsed is None:
            consecutive_fails += 1
            log.warning("Groq batch %d/%d gave up (consecutive_fails=%d/%d): %s",
                         bi, total_batches, consecutive_fails,
                         _MAX_CONSECUTIVE_FAILS, last_err)
            out.extend([None] * len(chunk))
            if consecutive_fails >= _MAX_CONSECUTIVE_FAILS:
                log.error("Groq fallback exhausted after %d consecutive failures — "
                          "remaining %d rows will be regex-only",
                          consecutive_fails,
                          len(descriptions) - (i + len(chunk)))
                giving_up = True
        else:
            consecutive_fails = 0
            out.extend(parsed)

        # Pace between batches to stay under TPM. Skip on the final batch.
        if sleep_s > 0 and bi < total_batches and not giving_up:
            time.sleep(sleep_s)

    return out


def _parse_response(content: str, expected_len: int,
                    valid: set) -> List[Optional[str]]:
    """Parse the model output into a list of length `expected_len`.

    JSON-mode forces a JSON object reply, but our prompt asks for an array —
    accept either {"results":[...]} / {"categories":[...]} or a bare array."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        log.warning("Groq returned non-JSON content (truncated): %s", content[:200])
        return [None] * expected_len

    if isinstance(data, dict):
        for key in ("results", "categories", "answers", "data"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break
        else:
            # Single-key dict — take its value
            if len(data) == 1:
                v = next(iter(data.values()))
                if isinstance(v, list):
                    data = v

    if not isinstance(data, list):
        log.warning("Groq response not a list after extraction: %r", type(data))
        return [None] * expected_len

    out: List[Optional[str]] = []
    for item in data[:expected_len]:
        s = item.strip().lower() if isinstance(item, str) else ""
        if s in valid and s != "none":
            out.append(s)
        else:
            out.append(None)
    # Pad if model under-returned
    while len(out) < expected_len:
        out.append(None)
    return out
