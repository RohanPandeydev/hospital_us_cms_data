"""Layered device matcher for FDA recalls (and other device-identity tasks).

Given a piece of free text (e.g. a recall's product_description + firm_name +
reason_for_recall + raw openFDA payload), resolve it to a device identity:

    {k_number, product_code, device_category, method, confidence}

Method order (highest-confidence first — we stop at the first hit):

    1. k_number      — extract K-number via regex (pattern K\\d{6}) from
                       text OR raw.openfda.k_number[]. Look up in fda_510k
                       → product_code → bridge → device_category.
                       confidence = high.

    2. product_code  — raw.openfda.product_code exact match against bridge.
                       confidence = high.

    3. manufacturer  — substring match firm_name against fda_510k.applicant.
                       If all matched applicants share ONE product_code and
                       that code is in the bridge, accept. If they share a
                       single device_category (multiple PCs), accept that.
                       confidence = medium.

    4. regex         — the existing keyword-based tagger rules. Fast,
                       deterministic, but coarse. confidence = medium.

    5. llm           — Groq classifier over the full haystack + vocabulary.
                       Only called when the deterministic layers fail, so the
                       LLM spend stays minimal. confidence = low (model-based).

If nothing resolves: returns {method: None, confidence: None, ...: None}.
"""

import logging
import re
from typing import Optional, List

from . import risk_llm
from .risk_recall_tagger import infer_category as _regex_infer_category

log = logging.getLogger(__name__)

# K-numbers: always 'K' + 6 digits. FDA occasionally references 'DEN' (de novo)
# or 'P' (PMA) numbers; we only handle K for now since those are the 510(k)
# lookups we populate.
_K_NUMBER_RX = re.compile(r"\bK\d{6}\b", re.IGNORECASE)


# ------------------------------------------------------------------
# Data access helpers (read the small lookup tables once per run).
# ------------------------------------------------------------------

def load_bridge_index(conn) -> dict:
    """{product_code: device_category} — 1:many collapsed to first seen."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT product_code, device_category FROM bridge_hcpcs_to_product_code "
            "WHERE device_category IS NOT NULL"
        )
        out: dict[str, str] = {}
        for pc, cat in cur.fetchall():
            if pc and cat and pc not in out:
                out[pc] = cat
    return out


def load_510k_index(conn) -> dict:
    """Return two dicts:
        by_k:        {K000000: {product_code, applicant, device_name}}
        by_applicant:{applicant_lower: [product_code, ...]} (unique list)
    """
    by_k: dict = {}
    by_app: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT k_number, applicant, device_name, product_code "
            "FROM fda_510k WHERE k_number IS NOT NULL"
        )
        for k, app, name, pc in cur.fetchall():
            by_k[k] = {"applicant": app, "device_name": name, "product_code": pc}
            if app and pc:
                by_app.setdefault(app.lower(), set()).add(pc)
    by_app = {k: sorted(v) for k, v in by_app.items()}
    return {"by_k": by_k, "by_applicant": by_app}


# ------------------------------------------------------------------
# Layer 1: K-number extraction + 510k lookup
# ------------------------------------------------------------------

def _extract_k_numbers(text: str, raw: Optional[dict] = None) -> List[str]:
    """Pull K-numbers out of free text AND the raw openFDA payload."""
    found: list[str] = []

    if raw:
        openfda = raw.get("openfda") or {}
        for k in (openfda.get("k_number") or []):
            if k and _K_NUMBER_RX.match(str(k).strip()):
                found.append(str(k).strip().upper())

    if text:
        for m in _K_NUMBER_RX.finditer(text):
            found.append(m.group(0).upper())

    # dedupe, preserve order
    seen = set()
    out: list[str] = []
    for k in found:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _resolve_k(k_number: str, k_index: dict, bridge: dict) -> Optional[dict]:
    row = k_index["by_k"].get(k_number.upper())
    if not row:
        return None
    pc = row.get("product_code")
    cat = bridge.get(pc) if pc else None
    return {
        "k_number":        k_number.upper(),
        "product_code":    pc,
        "device_category": cat,
        "applicant":       row.get("applicant"),
        "device_name":     row.get("device_name"),
    }


# ------------------------------------------------------------------
# Layer 3: manufacturer substring matching
# ------------------------------------------------------------------

# Common corporate suffixes we strip before matching so "Boston Scientific
# Corporation" and "Boston Scientific Corp." collide on the same key.
_SUFFIX_RX = re.compile(
    r"\b(corp(oration)?|inc(orporated)?|ltd|llc|co\.?|company|gmbh|sa|nv|ag|plc|limited)\b\.?",
    re.IGNORECASE,
)
_PUNCT_RX = re.compile(r"[,\.;:/\\\-\(\)\[\]]+")


def _normalize_firm(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.lower()
    s = _SUFFIX_RX.sub(" ", s)
    s = _PUNCT_RX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _match_by_manufacturer(firm_name: str, k_index: dict,
                             bridge: dict) -> Optional[dict]:
    """Substring match firm_name against known 510k applicants, then reduce
    to a single product_code or device_category."""
    norm = _normalize_firm(firm_name)
    if len(norm) < 4:
        return None

    hits: list[str] = []   # product_codes from matching applicants
    for app_l, pcs in k_index["by_applicant"].items():
        app_norm = _normalize_firm(app_l)
        if not app_norm:
            continue
        if norm == app_norm or norm in app_norm or app_norm in norm:
            hits.extend(pcs)

    if not hits:
        return None

    hits_set = set(hits)

    # Ideal: exactly one product_code → direct hit, and it's in the bridge.
    if len(hits_set) == 1:
        pc = next(iter(hits_set))
        cat = bridge.get(pc)
        if cat:
            return {"k_number": None, "product_code": pc, "device_category": cat}

    # Fallback: many product codes, but they all share one device_category.
    cats = {bridge[pc] for pc in hits_set if pc in bridge}
    if len(cats) == 1:
        return {"k_number": None, "product_code": None, "device_category": next(iter(cats))}

    return None


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def match_device(
    text: str,
    firm_name: Optional[str],
    raw: Optional[dict],
    k_index: dict,
    bridge: dict,
    vocabulary: Optional[list] = None,
    use_llm: bool = False,
    llm_batch_pool: Optional[list] = None,
) -> dict:
    """Single-record matcher. Returns a uniform dict:
        {k_number, product_code, device_category, method, confidence,
         applicant?, device_name?}

    For batch runs, prefer `match_batch` below — it groups LLM queries so the
    Groq call cost is shared across the remaining unresolved rows."""
    result = {
        "k_number":        None,
        "product_code":    None,
        "device_category": None,
        "method":          None,
        "confidence":      None,
    }

    # ---- Layer 1: K-number ----
    k_candidates = _extract_k_numbers(text or "", raw)
    for k in k_candidates:
        res = _resolve_k(k, k_index, bridge)
        if res and res["device_category"]:
            return {**result, **res, "method": "k_number", "confidence": "high"}
        if res and res["product_code"]:
            # K# resolved to a PC but PC isn't in our bridge — still useful
            # to persist so humans can review.
            return {**result, **res, "method": "k_number", "confidence": "medium"}

    # ---- Layer 2: openfda.product_code exact ----
    openfda = (raw or {}).get("openfda") or {}
    for pc in (openfda.get("product_code") or []):
        if pc and pc in bridge:
            return {
                **result,
                "product_code": pc,
                "device_category": bridge[pc],
                "method": "product_code",
                "confidence": "high",
            }

    # ---- Layer 3: manufacturer ----
    if firm_name:
        m = _match_by_manufacturer(firm_name, k_index, bridge)
        if m and m["device_category"]:
            return {**result, **m, "method": "manufacturer", "confidence": "medium"}

    # ---- Layer 4: regex keyword tagger ----
    rgx_cat = _regex_infer_category(text or "")
    if rgx_cat:
        return {**result, "device_category": rgx_cat,
                "method": "regex", "confidence": "medium"}

    # ---- Layer 5: LLM (caller must batch — leave for match_batch) ----
    return result


def match_batch(conn, rows: list, use_llm: bool = True,
                llm_batch_size: int = 10,
                llm_max_rows: Optional[int] = None) -> list[dict]:
    """Run the layered matcher over a batch of rows.

    Each input row should be a dict with keys: `text`, `firm_name`, `raw`.
    Returns a list of match dicts in the same order, each including the input
    row's original key (caller can tag whatever context they need by putting
    it in a `_id` field — we pass it through).
    """
    bridge = load_bridge_index(conn)
    k_index = load_510k_index(conn)
    log.info("device matcher: bridge=%d PCs, 510k=%d K#s (%d applicants)",
             len(bridge), len(k_index["by_k"]), len(k_index["by_applicant"]))

    # Pass 1: deterministic layers
    out: list[dict] = []
    unresolved: list[int] = []
    for i, r in enumerate(rows):
        res = match_device(
            text=r.get("text") or "",
            firm_name=r.get("firm_name"),
            raw=r.get("raw"),
            k_index=k_index, bridge=bridge,
        )
        res["_id"] = r.get("_id")
        out.append(res)
        if res["device_category"] is None:
            unresolved.append(i)

    log.info("device matcher layer 1-4 resolved %d/%d (%d remaining for LLM)",
             len(rows) - len(unresolved), len(rows), len(unresolved))

    # Pass 2: LLM fallback for the remainder
    if use_llm and unresolved and risk_llm.is_enabled():
        candidates = unresolved if llm_max_rows is None else unresolved[:llm_max_rows]
        vocab = sorted({cat for cat in bridge.values() if cat})
        descriptions = [
            (rows[i].get("text") or "")[:600] for i in candidates
        ]
        log.info("device matcher: calling Groq for %d rows (primary=%s, fallback=%s)",
                 len(descriptions), risk_llm._MODEL, risk_llm._FALLBACK_MODEL)
        try:
            labels = risk_llm.classify_recall_descriptions(
                descriptions, vocabulary=vocab, batch_size=llm_batch_size,
            )
        except Exception as e:
            log.warning("device matcher: LLM raised: %s — deterministic-only results kept", e)
            labels = []
        for idx, label in zip(candidates, labels):
            if label:
                out[idx]["device_category"] = label
                out[idx]["method"] = "llm"
                out[idx]["confidence"] = "low"

    # Stats
    by_method: dict = {}
    for r in out:
        m = r.get("method") or "none"
        by_method[m] = by_method.get(m, 0) + 1
    log.info("device matcher final: %s", by_method)
    return out
