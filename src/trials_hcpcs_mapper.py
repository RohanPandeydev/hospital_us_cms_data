"""Map ClinicalTrials.gov intervention text → HCPCS code using Groq.

The interventions table has free-text names like "Drug-eluting stent placement"
that have no machine-readable HCPCS code attached. For the HCPCS-centric
queries this project supports, we want every trial intervention linked to
the HCPCS code(s) it describes.

Strategy:
  1. Pull every (nct_id, intervention_name, intervention_type, description)
     row where `hcpcs_code IS NULL` and `intervention_type IN ('DEVICE',
     'PROCEDURE')` (drug/biological/behavioral interventions are skipped —
     they're rarely HCPCS-codable).
  2. Build a candidate-HCPCS shortlist for each row by ngram-matching the
     intervention text against `hcpcs_master.long_desc` (top 20).
  3. Send (text, candidates) batches to Groq with a strict JSON-mode
     prompt: return the chosen HCPCS code or "none".
  4. Update `clinical_trial_interventions` in place via ReplacingMergeTree
     re-insert (same key + new fetched_at supersedes the old row).

Rate limit / fallback behavior is identical to risk_llm.classify_recall:
primary model → fallback 8B → give up after N consecutive failures.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import List, Optional, Tuple

from dotenv import load_dotenv

from . import db

load_dotenv()
log = logging.getLogger(__name__)

_API_KEY = os.getenv("GROQ_API_KEY", "")
_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
_FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")
_TIMEOUT = float(os.getenv("GROQ_TIMEOUT", "60"))
_BATCH_SLEEP = float(os.getenv("GROQ_BATCH_SLEEP", "1.5"))

_SYSTEM_PROMPT = """You map clinical-trial intervention descriptions to HCPCS codes.

For each input item you receive {"text": ..., "candidates": [{"code": ..., "desc": ...}, ...]},
pick the HCPCS code that best describes the intervention, or "none" if no
candidate is a good fit. Do NOT invent codes — only return codes from the
candidates list, or the literal string "none".

Return ONLY a JSON array of strings, one per input, in input order.
No prose, no markdown, no extra keys. Length must match the input array.
"""


_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> set[str]:
    return set(t for t in _PUNCT_RE.sub(" ", (text or "").lower()).split() if len(t) >= 3)


def _build_candidates(conn, limit_per_row: int = 20) -> dict[str, list[tuple]]:
    """Pre-load every HCPCS code's tokenized long_desc once.

    Returns {hcpcs_code: (long_desc, token_set)}. Used to ngram-score
    candidate codes for each intervention text without re-querying.
    """
    rows = conn.query(
        "SELECT hcpcs_code, long_desc FROM hcpcs_master FINAL "
        "WHERE long_desc IS NOT NULL"
    ).result_rows
    out: dict[str, tuple[str, set[str]]] = {}
    for code, desc in rows:
        out[code] = (desc, _normalize(desc))
    log.info("loaded %d HCPCS code descriptions", len(out))
    return out


def _shortlist(text: str, hcpcs_index: dict, top_k: int = 20) -> list[tuple]:
    """Score every HCPCS code by token-overlap with `text`, return top_k."""
    text_tokens = _normalize(text)
    if not text_tokens:
        return []
    scored: list[tuple[int, str, str]] = []
    for code, (desc, tokens) in hcpcs_index.items():
        if not tokens:
            continue
        score = len(text_tokens & tokens)
        if score >= 2:  # require at least 2 token overlap to be a candidate
            scored.append((score, code, desc))
    scored.sort(reverse=True)
    return [(c, d) for _, c, d in scored[:top_k]]


def _groq_client():
    if not _API_KEY:
        return None
    try:
        os.environ.pop("GROQ_BASE_URL", None)
        from groq import Groq
        return Groq(api_key=_API_KEY, timeout=_TIMEOUT, max_retries=2)
    except Exception as e:
        log.warning("Groq client init failed: %s", e)
        return None


def _is_rate_limit(err: Exception) -> bool:
    msg = str(err).lower()
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return True
    return getattr(err, "status_code", None) == 429


def _call_groq(client, model: str, payload: list[dict],
               valid_codes: set[str]) -> list[Optional[str]]:
    user_msg = json.dumps(payload, ensure_ascii=False)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        max_tokens=2048,
    )
    content = resp.choices[0].message.content or ""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return [None] * len(payload)
    if isinstance(data, dict):
        for k in ("results", "codes", "answers", "data"):
            if k in data and isinstance(data[k], list):
                data = data[k]
                break
        else:
            if len(data) == 1:
                v = next(iter(data.values()))
                if isinstance(v, list):
                    data = v
    if not isinstance(data, list):
        return [None] * len(payload)
    out: list[Optional[str]] = []
    for item in data[:len(payload)]:
        if not isinstance(item, str):
            out.append(None)
            continue
        s = item.strip().upper()
        if s == "NONE" or not s:
            out.append(None)
        elif s in valid_codes:
            out.append(s)
        else:
            out.append(None)
    while len(out) < len(payload):
        out.append(None)
    return out


INSERT_COLUMNS = [
    "nct_id", "intervention_name", "intervention_type", "description",
    "hcpcs_code", "hcpcs_confidence",
]


def map_all(batch_size: int = 10,
            limit: Optional[int] = None,
            min_overlap: int = 2) -> dict:
    """Run the trial-intervention → HCPCS mapper.

    batch_size  — items per Groq call
    limit       — cap total interventions processed (smoke testing)
    min_overlap — minimum token-overlap to send to Groq (default 2)
    """
    client = _groq_client()
    if client is None:
        log.warning("GROQ_API_KEY missing — mapper would be a no-op; aborting.")
        return {"error": "GROQ_API_KEY not set"}

    with db.connect() as conn:
        hcpcs_index = _build_candidates(conn)
        valid_codes = set(hcpcs_index.keys())

        # Pull unmapped device/procedure interventions only — drug rows are
        # rarely HCPCS-codable and waste tokens.
        sql = """
            SELECT nct_id, intervention_name, intervention_type, description
              FROM clinical_trial_interventions FINAL
             WHERE hcpcs_code IS NULL
               AND intervention_type IN ('DEVICE', 'PROCEDURE')
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.query(sql).result_rows
        log.info("mapping %d unmapped interventions", len(rows))

        updates: list[tuple] = []
        consecutive_fails = 0
        current_model = _MODEL
        swapped = False

        batch_payload: list[dict] = []
        batch_meta: list[tuple] = []
        n_with_candidates = 0
        n_mapped = 0

        def _flush():
            nonlocal consecutive_fails, current_model, swapped, n_mapped
            if not batch_payload:
                return
            parsed: Optional[list[Optional[str]]] = None
            last_err: Optional[Exception] = None
            for attempt_model in (
                [current_model] if swapped else [current_model, _FALLBACK_MODEL]
            ):
                try:
                    parsed = _call_groq(client, attempt_model,
                                          batch_payload, valid_codes)
                    if attempt_model != current_model:
                        log.info("Groq: switched to fallback %s", attempt_model)
                        current_model = attempt_model
                        swapped = True
                    break
                except Exception as e:
                    last_err = e
                    if _is_rate_limit(e):
                        time.sleep(2.0)
                        continue
                    break
            if parsed is None:
                consecutive_fails += 1
                log.warning("Groq batch failed (fails=%d): %s",
                            consecutive_fails, last_err)
                return
            consecutive_fails = 0
            for (nct, name, typ, desc), code in zip(batch_meta, parsed):
                if code:
                    n_mapped += 1
                    updates.append(
                        (nct, name, typ, desc, code, "groq")
                    )

        for i, (nct, name, typ, desc) in enumerate(rows):
            text = " ".join(filter(None, [name, desc]))
            candidates = _shortlist(text, hcpcs_index, top_k=20)
            if not candidates:
                continue
            n_with_candidates += 1
            batch_payload.append({
                "text": text[:500],
                "candidates": [{"code": c, "desc": d[:120]}
                                for c, d in candidates],
            })
            batch_meta.append((nct, name, typ, desc))
            if len(batch_payload) >= batch_size:
                _flush()
                batch_payload, batch_meta = [], []
                if _BATCH_SLEEP > 0:
                    time.sleep(_BATCH_SLEEP)
            if consecutive_fails >= 5:
                log.error("Groq fallback exhausted — aborting at row %d", i)
                break

        if batch_payload:
            _flush()

        if updates:
            conn.insert(
                "clinical_trial_interventions",
                updates, column_names=INSERT_COLUMNS,
            )
            log.info("inserted %d mappings into clinical_trial_interventions",
                     len(updates))

    return {
        "rows_considered":   len(rows),
        "rows_with_candidates": n_with_candidates,
        "rows_mapped":       n_mapped,
    }
