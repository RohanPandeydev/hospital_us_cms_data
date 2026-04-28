"""One-shot TinyFish probe of candidate hospital-level adverse-event sources.

Goal: before we commit to ingesting any of these, verify each source is real,
machine-extractable, and actually exposes a HOSPITAL NAME alongside the event.
Public US federal MAUDE doesn't carry a hospital identifier — these are the
non-federal candidates that might.

Runs N probes in parallel and prints a one-line verdict for each. Full JSON
goes to downloads/tinyfish/probe_<slug>.json so we can re-read later.
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import tinyfish_client as tf  # noqa: E402

OUT_DIR = "downloads/tinyfish"
os.makedirs(OUT_DIR, exist_ok=True)

GOAL = (
    "I am evaluating this site as a public data source for hospital-level "
    "medical-device adverse events. Without logging in, browse the page and "
    "any obvious sub-pages (Reports / Data / Annual Report / Downloads) and "
    "answer ALL of the following as a single JSON object: "
    "{\"page_loads\": bool, "
    "\"shows_named_hospitals\": bool — does any public report on the site "
    "list specific hospital names alongside specific adverse events?, "
    "\"covers_devices\": bool — are medical-device-related events (device "
    "failure, retained item, wrong implant, infection from device) included?, "
    "\"latest_year\": int or null, "
    "\"download_formats\": [\"pdf\"|\"csv\"|\"xlsx\"|\"json\"|\"api\"|\"html\"] — "
    "what machine-extractable formats are offered?, "
    "\"direct_download_urls\": [str] — up to 3 direct URLs to the most recent "
    "report/data file, "
    "\"access_model\": \"free\"|\"foia_required\"|\"paywalled\"|\"login_required\", "
    "\"sample_event_excerpt\": str — one short verbatim quote of an event "
    "tied to a named hospital, if any, "
    "\"verdict\": str — one sentence on whether this is usable for an "
    "automated CCN-keyed ingest pipeline}"
)

SOURCES = [
    ("mn_adverse_events",
     "https://www.health.state.mn.us/facilities/patientsafety/adverseevents/"),
    ("ma_sre_reports",
     "https://www.mass.gov/lists/serious-reportable-events-reports"),
    ("nj_patient_safety",
     "https://www.nj.gov/health/healthcarequality/health-care-professionals/patient-safety-initiative/"),
    ("pa_psa",
     "http://patientsafety.pa.gov"),
    ("courtlistener",
     "https://www.courtlistener.com"),
    ("ct_dph_adverse",
     "https://portal.ct.gov/dph/Health-Care-Quality-Safety/Adverse-Event-Program/Adverse-Event-Reports"),
    ("jpml_mdl",
     "https://www.jpml.uscourts.gov/multidistrict-litigation"),
    ("ca_cdph_adverse",
     "https://www.cdph.ca.gov/Programs/CHCQ/LCP/Pages/AdverseEventsLanding.aspx"),
]


def probe(slug: str, url: str) -> dict:
    started = time.time()
    out = {"slug": slug, "url": url}
    try:
        result = tf.run(url, GOAL, timeout=420)
        out["ok"] = True
        out["raw"] = result
        # tinyfish wraps payload as result.result (sometimes nested)
        inner = result.get("result")
        if isinstance(inner, dict):
            inner = inner.get("result", inner)
        out["extracted"] = inner
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)[:400]
    out["elapsed_s"] = round(time.time() - started, 1)
    path = os.path.join(OUT_DIR, f"probe_{slug}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    return out


def fmt(o: dict) -> str:
    if not o.get("ok"):
        return f"  [{o['slug']}]  ERROR  {o.get('error','?')[:160]}"
    e = o.get("extracted") or {}
    if isinstance(e, list) and e:
        e = e[0]
    if not isinstance(e, dict):
        return f"  [{o['slug']}]  ?  raw={str(o.get('extracted'))[:160]}"
    return (
        f"  [{o['slug']}]  "
        f"loads={e.get('page_loads')}  "
        f"named_hosp={e.get('shows_named_hospitals')}  "
        f"devices={e.get('covers_devices')}  "
        f"yr={e.get('latest_year')}  "
        f"fmts={e.get('download_formats')}  "
        f"access={e.get('access_model')}\n"
        f"      verdict: {e.get('verdict','')}"
    )


def main():
    print(f"Probing {len(SOURCES)} sources via TinyFish (parallel)…\n")
    results = []
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futs = {ex.submit(probe, s, u): s for s, u in SOURCES}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            print(f"[{r['elapsed_s']}s] done: {r['slug']}")
    print("\n=== VERDICTS ===")
    for r in sorted(results, key=lambda x: x["slug"]):
        print(fmt(r))
    print(f"\nFull JSON in {OUT_DIR}/probe_*.json")


if __name__ == "__main__":
    main()
