# Hospital CMS × FDA Device Data — Project Overview

**For:** Owner / stakeholder briefing
**Scope:** What we've built, what's in the database, and how this powers AI/ML predictions for hospital device safety.

---

## 1. The goal in one sentence

Build a unified data warehouse that lets us answer:
**"Given a hospital, how likely is a device-related complication, readmission, or adverse event — and why?"**

To answer that we have to connect two federal data silos that don't share a primary key:
- **CMS Provider Data** — every US hospital + its quality/outcome scores
- **FDA MAUDE + GUDID** — every reported medical-device adverse event + device master data

The connection between them is the engineering problem. We built the plumbing, the bridge, and the query layer.

---

## 2. What's in the database today

| Layer | Source | Rows | What it is |
|---|---|---:|---|
| Hospitals | CMS `xubh-q36u` | **5,426** | Every Medicare-certified US hospital, keyed by CCN |
| Hospital measures | CMS (multiple datasets) | **60,000** | 144 distinct measures — mortality, readmission, complications, PSI, HAI, HCAHPS, HRRP, VBP, maternal health, cancer |
| State benchmarks | CMS | 1,120 | State-level rates for comparisons |
| National benchmarks | CMS | 20 | National averages for normalization |
| HCPCS master | CMS quarterly ZIP | **9,068** (2,478 device-class) | Every billing code, with short/long description, device flag |
| FDA MAUDE events | openFDA API | **10,000** | Device adverse-event reports (injury, malfunction, death) |
| FDA MAUDE devices | openFDA API | 10,018 | The devices named in those events, with product_code, brand, manufacturer |
| FDA GUDID | openFDA UDI API | **5,000** (sample) | Device master — primary_di, brand, description, product_code |
| **Bridge: HCPCS ↔ product_code** | hand-seed + GUDID auto-match | **56** | The engineered crosswalk between CMS billing and FDA device classification |

All of this is in Postgres with idempotent upsert (safe to re-run), ingestion logging, and full-text indexing for matching.

---

## 3. The central engineering asset — the bridge

The single innovation that makes everything else work.

**Problem**: CMS uses HCPCS codes (billing). FDA uses CDRH product_codes (device classification). They have no shared key. Without a bridge, you cannot ask "what FDA events are relevant to this CMS billing pattern?"

**Solution**: `bridge_hcpcs_to_product_code` — a many-to-many crosswalk.

- **49 manual seed rows**: hand-curated from the FDA CDRH Product Classification Database, covering high-volume device families (pacemakers, ICDs, stents, catheters, IOLs, insulin pumps, CPAP, wheelchairs, neurostimulators)
- **7 auto-generated rows** (2 at conservative setting, +5 with lower threshold): Postgres full-text matching between HCPCS descriptions and GUDID device descriptions, with support-count-based confidence scoring

Every bridge row carries: `match_method` (manual_seed / gudid_description), `confidence` (high/medium/low), and `source_notes` for auditability.

**This is the file that turns two disconnected federal datasets into one queryable graph.**

---

## 4. How the data flows

```
  CMS Provider Data API ──┐
  CMS Data API            ├──►  Postgres warehouse  ──►  FastAPI UI + CSV exports
  CMS HCPCS ZIP           │         ▲
  FDA openFDA (MAUDE)     │         │
  FDA openFDA (GUDID)  ───┘         │
                                    │
                          bridge_hcpcs_to_product_code
                       (manual seed + GUDID auto-matcher)
```

- **Ingestion** is one command: `python run.py ingest` — pulls every registered dataset, upserts, and refreshes the bridge
- **Materialized views** aggregate MAUDE events per HCPCS (`hcpcs_device_profile`) and pre-join hospital × HCPCS × events (`hospital_hcpcs_enriched`)
- **UI** at `ui/app.py` (launch: `./run_server.sh`) gives a browsable view: hospitals, measures, codes, linkage, leaderboard
- **Exports** in `exports/` — 6 CSVs (12k rows) for inspection in Excel

---

## 5. What we can answer TODAY

| Question | Layer used |
|---|---|
| "Show me every US hospital + its quality scores" | CMS hospitals × measures |
| "Which hospitals have worse-than-national complication rates for hip/knee surgery?" | CMS `COMP_HIP_KNEE` measure |
| "What FDA adverse events are linked to pacemaker billing codes?" | bridge → MAUDE |
| "Given HCPCS C1785 (pacemaker), show every related MAUDE malfunction" | bridge + MAUDE |
| "What devices in GUDID match this HCPCS description?" | full-text match → GUDID |
| "Which manufacturers appear most often in MAUDE death/injury reports?" | MAUDE aggregate |

## 6. What we can't yet answer (and why)

| Gap | Why | Fix |
|---|---|---|
| "Which CMS hospital had THIS MAUDE event?" | MAUDE rarely names the CCN; needs a fuzzy facility matcher | Build `bridge_maude_to_ccn` (ZIP + Jaro-Winkler name match). Expected 50–60% coverage. |
| "What's the adverse-event RATE (per 1000 procedures) at this hospital?" | Need Medicare billing denominator from `cms_provider_summary` | Blocked: CMS data-api/v1 is currently down. Code is ready. |
| "Did this manufacturer pay this hospital?" | Open Payments dataset registered but not ingested | Run `python run.py ingest --dataset op_2024_general` |
| Broader auto-matching | GUDID pull is 5k sample of ~2M records | Run full GUDID ingest (~6h background) |

---

## 7. How this powers AI / ML prediction

The dataset is designed as a **multi-layer feature matrix** for supervised learning. Every column is either a feature, a label, or a join key.

### 7.1 Prediction targets (the labels)

| Target | Source | Model type |
|---|---|---|
| 30-day readmission rate | CMS `READM_30_*` | Regression |
| "Worse than national" complication flag | CMS `compared_to_national` | Binary classification |
| Excess Readmit Ratio (ERR) | CMS HRRP | Continuous regression — the gold-standard CMS metric |
| PSI composite (patient safety) | CMS `PSI_90` | Regression |
| Device-specific HAC rate | CMS `HAI_1` / `HAI_2` | Per-device classification |
| Device adverse-event count per 1000 procedures | MAUDE ÷ CMS denominator | Rate regression (once billing unlocks) |

### 7.2 Feature layers

**Layer 1 — Hospital identity** (from `cms_hospitals`)
Type, ownership, star rating, state, emergency services flag, IPPS flag. Categorical + geographic features.

**Layer 2 — Hospital performance history** (from `cms_hospital_measures`)
Historical mortality, readmission, complication, HAI, HCAHPS scores. Continuous features normalized against national benchmarks (the `cms_national_measures` table is the normalization anchor).

**Layer 3 — Procedure mix** (from `cms_provider_summary`, once unlocked)
Medicare billing volume by HCPCS — tells the model what this hospital actually does. A pacemaker failure model cares whether the hospital implants pacemakers.

**Layer 4 — Device adverse-event signal** (from MAUDE + bridge)
For each hospital × device-family × 12-month window: count of injuries, malfunctions, deaths. Joined via `bridge_hcpcs_to_product_code`. Rate-normalized by Layer 3 volume.

**Layer 5 — Device identity** (from GUDID + MAUDE device join)
Brand, manufacturer risk tier, device class I/II/III. One-hot or embedding features.

### 7.3 Why this architecture is ML-friendly

- **Structured labels**: CMS `compared_to_national` is already 3-class ordinal (Better / No Different / Worse). Many models can train directly on this.
- **Denominator-normalized features**: `cms_state_measures` + `cms_national_measures` let us remove volume bias — essential because MAUDE has severe under-reporting bias.
- **Confidence-weighted bridge**: every HCPCS↔product_code link has a confidence column. Models can downweight low-confidence joins.
- **Temporal anchor**: CMS `start_date` / `end_date` give explicit reporting windows. We can align MAUDE events to the right CMS period.
- **Text fields preserved**: MAUDE `mdr_text` and GUDID `device_description` are kept raw — available for NLP/embedding features.
- **Full JSON raw payloads** are stored alongside parsed columns, so we can re-derive features without re-ingesting.

### 7.4 Example ML use cases

1. **Hospital device-risk scoring**
   Input: hospital CCN. Output: predicted readmission rate for a given device procedure. Uses Layers 1+2+3+4. Production model.

2. **Early-warning signal detection**
   Input: rolling 90-day MAUDE counts per (manufacturer, device family). Output: anomaly score flagging device recalls before FDA announces. Uses Layer 4 + temporal windowing.

3. **Hospital-benchmark clustering**
   Input: full feature matrix. Output: peer groups of hospitals with similar procedure mix + outcome profiles. Unsupervised; feeds fair-comparison reporting.

4. **Adverse-event triage classifier**
   Input: MAUDE `mdr_text` narrative. Output: predicted severity tier (death / hospitalization / malfunction only). NLP on Layer 5 text + metadata.

5. **Procedure safety recommender**
   Input: patient + procedure + location. Output: ranked hospitals within radius by predicted outcome for that specific device procedure. Combines all layers.

---

## 8. What the exports show (for stakeholder inspection)

Files in `exports/` (12,102 rows total across 6 CSVs):

| File | Rows | Use |
|---|---:|---|
| `01_bridge_hcpcs_to_product_code.csv` | 56 | The bridge itself — open this to see the HCPCS↔product_code crosswalk with MAUDE event counts per row |
| `02_cms_hospitals.csv` | 5,426 | Every hospital |
| `03_cms_measures_device_relevant.csv` | 2,000 | Sample of device-relevant quality measures |
| `04_maude_events_linked_to_hcpcs.csv` | 2,500 | MAUDE adverse events joined through the bridge → HCPCS |
| `05_gudid_linked_to_hcpcs.csv` | 120 | GUDID devices with HCPCS linkage |
| `06_combined_fda_event_to_cms_state.csv` | 2,000 | End-to-end: FDA event → product_code → HCPCS → state-level CMS context |

Concrete examples that are working in the exports:
- Bausch+Lomb IOL injury → V2630/V2632 (anterior/posterior IOL HCPCS)
- Sorin defibrillator malfunction → C1882 (AICD)
- Medtronic atherectomy balloon → C1725 (atherectomy catheter)

---

## 9. Build status at a glance

| Component | Status |
|---|---|
| CMS hospital + measures ingestion | ✅ Complete |
| CMS HCPCS master ingestion | ✅ Complete |
| FDA MAUDE ingestion (sample) | ✅ Complete |
| FDA GUDID ingestion (sample) | ✅ Complete |
| HCPCS↔product_code bridge | ✅ 56 rows, auto-matcher working |
| Materialized views | ✅ `hcpcs_device_profile`, `hospital_hcpcs_enriched` |
| FastAPI browser UI | ✅ Launch via `./run_server.sh` |
| CSV exports | ✅ 6 files in `exports/` |
| CMS Medicare billing ingestion | ⏸ Blocked — CMS data-api/v1 upstream outage (code ready) |
| MAUDE → CCN facility matcher | ⏸ Next priority (highest leverage) |
| Full GUDID pull (2M records) | ⏸ Not yet run |
| Open Payments ingestion | ⏸ Registered, not yet ingested |

---

## 10. What to tell the owner in one paragraph

We built a self-updating warehouse that ingests every US hospital's CMS quality data, the full HCPCS billing catalog, and FDA device adverse-event reports — and we engineered the bridge that connects them (the federal government does not publish one). Today we can query hospital quality outcomes, device adverse events, and the linkage between them across 5,426 hospitals, 60,000 measure scores, 10,000 adverse events, and a 56-row hand-curated + auto-matched HCPCS↔product_code crosswalk. The dataset is structured as a multi-layer ML feature matrix — ready for hospital device-risk scoring, early-warning signal detection, and patient-facing procedure-safety recommendations. One upstream CMS API is currently down, which blocks Medicare billing volume; once it's back, every adverse-event metric becomes rate-normalized (events per 1000 procedures), which is the industry-standard form for ML training.
