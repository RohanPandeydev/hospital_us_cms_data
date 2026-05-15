# Warehouse Inventory — `hospital_us_cms_data`

Live snapshot of every table, what it contains, what it covers, and where the data came from.

> **Total: 18,044,536 rows across 12 tables.**
> Connection: `warehouse.dev.rp360.io:443` (ClickHouse Cloud) · database `hospital_us_cms_data`.

---

## At-a-glance

| # | Table | Raw rows | After `FINAL` | What it is |
|---|---|---:|---:|---|
| 1 | `cms_provider_summary` | 14,216,294 | 14,216,294 | Medicare physician + DMEPOS + inpatient/outpatient billing |
| 2 | `cms_open_payments` | 2,042,538 | 2,042,538 | Manufacturer-to-physician payments (Sunshine Act) |
| 3 | `npi_facility_affiliation` | 1,638,956 | 1,638,956 | Physician NPI → hospital CCN bridge |
| 4 | `opps_addendum_b` | 47,528 | 47,528 | OPPS hospital-outpatient payment rules |
| 5 | `clinical_trials` | 35,806 | 23,451 | ClinicalTrials.gov device-relevant studies |
| 6 | `cms_hospital_measures` | 34,316 | 34,316 | Hospital quality scores (readmits, mortality, PSI, HAI) |
| 7 | `clinical_trial_interventions` | 26,136 | 21,542 | Trial × intervention rows (Groq-mapped to HCPCS) |
| 8 | `hcpcs_master` | 18,136 | 9,068 | HCPCS Level II code dictionary |
| 9 | `cms_hospitals` | 5,852 | 5,426 | Medicare-certified hospital roster |
| 10 | `stark_dhs_codes` | 5,237 | 5,237 | Stark Law Designated Health Services list |
| 11 | `cms_ingestion_log` | 108 | 108 | Pipeline audit log |
| 12 | `device_to_procedure` | 72 | 72 | C-code device → parent CPT crosswalk |

---

## 1. `cms_provider_summary` — 14.2 M rows

The Medicare-billing backbone. Four `dataset_id` slices stacked into one schema:

| dataset_id | Rows | Grain | What's filled |
|---|---:|---|---|
| `medicare_physician_by_provider_service` | 8,411,562 | physician × HCPCS × year | NPI, HCPCS, services, payment |
| `medicare_dmepos_by_supplier_service` | 5,542,054 | DME supplier × HCPCS × year | NPI, HCPCS, services, payment |
| `medicare_inpatient_by_provider_service` | 145,879 | hospital × DRG × year | CCN, DRG, services, payment |
| `medicare_outpatient_by_provider_service` | 116,799 | hospital × APC × year | CCN, APC in `raw` JSON |

**Source:** data.cms.gov bulk CSV (catalog-fetched via `scripts/fetch_cms_api_csvs.py`).
**Year range:** 2021–2023 (most recent reporting years).

> **Always filter by `dataset_id`** — most columns are NULL for slices that don't apply.

---

## 2. `cms_open_payments` — 2.04 M rows

Industry payments to physicians under the Physician Payments Sunshine Act.

| Metric | Value |
|---|---:|
| Distinct manufacturers | 788 |
| Distinct physicians paid | 313,072 |
| Total Medicare years tracked | 2022 – 2024 |
| **Total paid** | **$3.99 Billion** |

### Boston Scientific specifically

| Metric | Value |
|---|---:|
| Payment records | 20,236 |
| Physicians paid | 1,806 |
| Total amount | **$32.0 M** |
| Distinct products | 47 |

Top BSc products by spend: WATCHMAN Access System, WATCHMAN FLX, Intracept, Precision, Wolverine Coronary Cutting Balloon, Vercise, ICEfx, FARAPULSE, TheraSphere.

**Source:** openpaymentsdata.cms.gov (DKAN API, year-by-year datasets).

---

## 3. `npi_facility_affiliation` — 1.64 M rows

The direct **NPI → hospital CCN** bridge. Without this, you can't answer "which hospital does Dr. X work at."

| Metric | Value |
|---|---:|
| Distinct NPIs | 840,728 |
| Distinct hospital CCNs | 38,954 |

**Source:** CMS Facility Affiliation Data (provider-data.cms.gov · ID 27ea-46a8), 91 MB CSV.

---

## 4. `opps_addendum_b` — 47,528 rows

CMS Hospital Outpatient Prospective Payment System rulebook. Each row = one (HCPCS × quarter).

### Status Indicator breakdown

| SI | Rows | Meaning |
|---|---:|---|
| `J1` | 9,728 | Comprehensive APC — bundled payment |
| `N`  | 6,556 | Packaged — no separate hospital payment |
| `A`  | 5,945 | Paid via other fee schedule |
| `C`  | 4,900 | Inpatient-only |
| `Q4` | 3,967 | Conditionally-packaged labs |
| `T`  | 3,149 | Significant procedures, discounted |
| `B`  | 3,013 | Not paid under OPPS |
| `Y`  | 2,324 | DME paid under DMEPOS fee schedule |
| `Q1` | 2,193 | STV-packaged |
| `S`  | 1,999 | Significant procedures, full |
| `K`  | 1,513 | Separately-paid drugs |
| `Q3` | 547  | Composite APCs |

**Source:** CMS quarterly Addendum B ZIPs at cms.gov (2025Q1, 2025Q2, 2026Q1 loaded).

---

## 5. `clinical_trials` — 23,451 unique trials

ClinicalTrials.gov studies scoped to device-research buckets.

| Metric | Value |
|---|---:|
| Distinct trials | 23,451 |
| Distinct device-family buckets | 33 |
| **Total patients enrolled** | **57,318,564** |
| BSc-sponsored trials | 219 |

Buckets include: pacemaker, defibrillator, drug-eluting stent, cardiac valve, hip/knee implant, neurostim implant, intraocular lens, LAA closure, cpap, breast implant, surgical mesh, cochlear implant, insulin pump, etc.

**Source:** clinicaltrials.gov/api/v2 (ingested across 30 device-keyword buckets).

---

## 6. `cms_hospital_measures` — 34,316 rows

Hospital quality scorecard.

| Metric | Value |
|---|---:|
| Distinct measures | 76 |
| Hospitals reporting | 1,809 |

### Measure families covered

- **Readmissions** — `READM_30_*` (raw %) + `READM-30-*-HRRP` (Excess Readmission Ratio)
  - AMI, CABG, COPD, Heart Failure, Hip/Knee, Pneumonia + Hospital-Wide Hybrid
- **Mortality** — `MORT_30_*` (AMI, CABG, COPD, HF, PN, Stroke) + Hybrid HWM
- **Patient Safety (PSI)** — pressure ulcer, iatrogenic pneumothorax, post-op DVT/PE, sepsis, etc. (PSI 03/04/06/08/09/10/11/12/13/14/15/90)
- **Hospital-Acquired Infections (HAI)** — CLABSI, CAUTI, MRSA, C. diff, SSI
- **EDAC** — excess days in acute care
- **OP measures** — outpatient follow-up visits

**Source:** CMS Hospital Compare datasets `ynj2-r877`, `632h-zaca`, `9n3s-kdb3`, `77hc-ibv8`, `muwa-iene`, `dgck-syfz`.

---

## 7. `clinical_trial_interventions` — 21,542 rows

Trial × intervention pairs. ~2,600 mapped to HCPCS codes via Groq AI classification.

**Source:** Same CTG.gov pull as `clinical_trials`, exploded by intervention.

---

## 8. `hcpcs_master` — 9,068 codes

The Medicare billing dictionary.

| Family | Codes | What it covers |
|---|---:|---|
| `G` | 2,032 | Temporary procedure codes |
| `J` | 1,236 | Injectable drugs |
| `L` | 949  | Prosthetics, orthotics, supplies |
| `A` | 893  | Transportation, supplies, miscellaneous |
| `E` | 687  | Durable medical equipment (CPAP, oxygen, walkers) |
| `Q` | 662  | Temporary procedure codes (other) |
| `C` | 636  | Outpatient hospital pass-through device codes |
| `S` | 554  | Non-Medicare commercial / Medicaid codes |
| `M` | 514  | Medical services |
| `V` | 223  | Vision and audiology |

> **CPT codes (5-digit numeric) are NOT in `hcpcs_master`** because they're AMA-licensed. The UI falls back to `opps_addendum_b.short_descriptor` for those.

**Source:** CMS HCPCS Quarterly ZIP (cms.gov/files/zip/april-2026-alpha-numeric-hcpcs-file.zip).

---

## 9. `cms_hospitals` — 5,426 unique hospitals

Every Medicare-certified hospital with name, address, type, ownership, ER capability, 1–5 CMS star rating.

**Source:** CMS Hospital General Information dataset `xubh-q36u`.

---

## 10. `stark_dhs_codes` — 5,237 rows

Stark Law Designated Health Services list — HCPCS codes subject to anti-self-referral rules. Covers 4 years (2023–2026) × 5 categories (Clinical Lab, Radiology, Radiation Therapy, PT/OT/SLP, Preventive Screening).

**Source:** cms.gov annual Stark DHS ZIP (AMA-license POST workflow).

---

## 11. `device_to_procedure` — 72 mappings, 52 devices

C-code device → parent CPT crosswalk. Each row tagged with `fda_class` (II/III) and `manufacturer_product`.

| Metric | Value |
|---|---:|
| Distinct device codes | 52 |
| Distinct parent CPT codes | 46 |
| Boston Scientific–tagged mappings | 28 |

### Boston Scientific Class II/III coverage

| Family | FDA | Products |
|---|---|---|
| Drug-Eluting Stent (coronary) | III | Promus / Synergy |
| Pacemaker | III | ACCOLADE / INGEVITY |
| AICD / S-ICD | III | EMBLEM / INOGEN / RESONATE |
| LAA Closure | III | Watchman / Watchman FLX |
| Deep Brain Stimulation | III | Vercise Genus / Cartesia |
| Spinal Cord Stimulation | III | Precision Plus / WaveWriter |
| Pulse-Field Ablation | III | FARAPULSE |
| Peripheral DES | III | Eluvia |
| Drug-Coated Balloon | III | Ranger |
| Cryoablation | III | ICEfx |
| LAMS (endoscopy) | II | AXIOS / Hot AXIOS |
| Cutting Balloon | II | Wolverine |
| BPH water vapor | II | Rezum |
| Laser BPH | II | GreenLight |
| Cholangioscopy | II | SpyGlass DS |
| Bronchoscopy | II | EXALT Model B |

**Source:** Hand-curated from CMS OPPS rules + BSc DBS-HCPCS Crosswalk PDF + clinical knowledge.

---

## 12. `cms_ingestion_log` — 108 rows

Pipeline audit log. Every ingest run writes one row.

---

# Coverage by use-case

| Question | Can you answer it? | Where |
|---|---|---|
| "What does HCPCS X mean?" | ✅ Yes | `hcpcs_master`, `opps_addendum_b` |
| "How does Medicare pay for X?" | ✅ Yes | `opps_addendum_b` (SI, APC, $) |
| "Which doctors bill HCPCS X?" | ✅ Yes (CPT, A, E, J, K, L codes) | `cms_provider_summary` |
| "Which hospitals have those doctors?" | ✅ Yes | `npi_facility_affiliation` + `cms_hospitals` |
| "What's hospital Y's readmission rate?" | ✅ Yes | `cms_hospital_measures` |
| "Which BSc products map to HCPCS X?" | ✅ Yes | `device_to_procedure` |
| "How much did BSc pay doctors?" | ✅ Yes | `cms_open_payments` |
| "Which trials study X-class device?" | ✅ Yes | `clinical_trials` + `interventions` |
| "Is X subject to Stark Law?" | ✅ Yes | `stark_dhs_codes` |
| "What's the readmission rate for a specific device?" | ❌ **No** | Requires CMS LDS (DUA + $) |
| "MAUDE adverse events for a BSc device" | ❌ **No (dropped)** | Re-enable `src/fda_client.py` |
| "FDA recalls for BSc device" | ❌ **No (dropped)** | Re-ingest via openFDA `/device/recall.json` |
| "ICD-10-PCS device-procedure crosswalk" | ❌ **No** | Build GUDID UDI/GMDN bridge |
| "HCUP NIS inpatient discharge data" | ❌ **No** | Public, would need ingest |
| "State adverse-event registry (MN AHE / MA SRE)" | ❌ **No (dropped)** | Scrapers available |

---

## Honest gaps (what we can't show)

1. **Device-attributed readmissions** — CMS publishes by condition (HF, AMI), not by device. Only the licensed CMS LDS (~$10-30K + DUA) gives patient-level claims with device + readmit linkage.
2. **MAUDE adverse events** — dropped during cleanup; can be re-enabled.
3. **FDA Recalls** — same as above.
4. **State adverse-event registries (MN AHE, MA SRE)** — scrapers exist, never re-ingested after the FDA cleanup.
5. **Real-world device performance** — proprietary (Definitive Healthcare, IQVIA, Komodo).

---

## Where to learn more

- **Schema details**: `schema_clickhouse.sql`
- **Per-table reference + 17 copy-paste queries**: `DATA_GUIDE.md`
- **5-minute mental model**: `DEV_ONBOARDING.md`
- **Step-by-step tutorial**: `QUERY_TUTORIAL.md`
- **Live UI**: `https://cms-hospital-explorer.onrender.com/`

---

*Generated from a live ClickHouse query against `warehouse.dev.rp360.io`. Numbers will drift as future ingests run; rerun `python -c "from src import db; ..."` to refresh.*
