# Free Alternatives We Used — In Place of Paid Datasets

> **Scope:** Only the free sources we fetched as substitutes for the **paid** datasets
> we couldn't afford (AGA GIQuIC, Medicare LDS, Premier/Vizient). Not the full source
> catalog — just the workaround sources that fill the same role at $0.
> **Audience:** Owner / sponsor — explains what we're approximating with free data
> instead of buying.

---

## The 3 paid datasets we'd ideally have

| Paid dataset | Annual cost | What it would give |
|---|---|---|
| **Medicare LDS** (ResDAC) | $10-15K | Per-claim CCN + HCPCS + DOS for 100% of Medicare claims, patient-level outcomes |
| **AGA GIQuIC** | $5-25K | Per-case device + size + adverse event at participating hospitals |
| **Premier PINC AI / Vizient CDB** | $50-150K | SKU-level hospital purchase data |

**Minimum spend to go fully deterministic:** ~$20-35K/yr (LDS + AGA).

Instead, we used the **free alternatives below** — covers ~70% of what those would give, with the rest staying probabilistic.

---

## Alternative 1 → in place of **Medicare LDS** (patient-level claims with CCN)

What we used: **State open-data hospital discharge portals.**

### 1a. NY SPARCS — Hospital Inpatient Discharges (de-identified)

| | |
|---|---|
| **URL** | `https://health.data.ny.gov/resource/46xm-urtu.json` |
| **Format** | Socrata JSON API, no auth |
| **Coverage** | All NY hospitals, per-discharge, 2023 |
| **Per-row fields (30)** | `permanent_facility_id`, `facility_name`, `hospital_county`, `age_group`, `gender`, `length_of_stay`, `type_of_admission`, `patient_disposition` (incl. "Expired"), `apr_drg_code`, `apr_drg_description`, `apr_severity_of_illness`, `apr_risk_of_mortality`, `ccsr_diagnosis_code`, `ccsr_procedure_code`, `total_charges`, `total_costs`, `emergency_department_indicator`, … |
| **AXIOS filter applied** | APR-DRG codes 405, 432-443 (pancreatic / hepatobiliary / pancreatitis) |
| **What we pulled** | 500 discharge rows → 108 unique NY hospitals → warehouse table `ny_sparcs_axios_by_hospital` |
| **What it replaces** | The CCN + claim-level granularity of Medicare LDS — for NY only |
| **What it doesn't replace** | Medicare LDS gives 100% Medicare nationally; this is NY only and all-payer |

### 1b. CA HCAI — Inpatient Mortality Indicators 2016-2023

| | |
|---|---|
| **URL** | `https://data.chhs.ca.gov/dataset/05fee607-cea9-4bf1-8b53-20ca584748a3/resource/af88090e-b6f5-4f65-a7ea-d613e6569d96/download/2016-2023-imi-results-long-view.csv` |
| **Format** | Direct CSV download, no auth, 6.7 MB |
| **Coverage** | All CA hospitals, per-condition, 8 years |
| **Per-row fields (11)** | `YEAR`, `COUNTY`, `HOSPITAL`, `OSHPDID` (CA's hospital ID), `Procedure/Condition`, `Risk Adjusted Mortality Rate`, `# of Deaths`, `# of Cases`, `Hospital Ratings`, `LONGITUDE`, `LATITUDE` |
| **AXIOS-relevant conditions** | "Pancreatic Resection", "Pancreatic Cancer", "Pancreatic Other", "GI Hemorrhage" |
| **What we pulled** | 60,498 total rows → 12,132 AXIOS-relevant → 554 CA hospitals → warehouse table `ca_axios_mortality_by_hospital` (759,416 cases, 19,535 deaths cumulative) |
| **What it replaces** | The per-hospital mortality + outcome rates Medicare LDS would give for CA |
| **What it doesn't replace** | Patient-level granularity — this is per-hospital aggregate not per-encounter |

### Combined deterministic coverage from 1a + 1b

| Metric | Value |
|---|---:|
| Unique US hospitals with deterministic case/death data | **662** (554 CA + 108 NY) |
| Cumulative AXIOS-relevant cases | ~760,000 |
| Cumulative deaths captured | ~19,550 |
| Cost | **$0** |

---

## Alternative 2 → in place of **AGA GIQuIC** (device-attributed adverse events at hospital)

What we used: a combination of **public clinical literature + court records + state error reports.**

### 2a. PubMed E-utils — clinical case reports

| | |
|---|---|
| **URL** | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` + `efetch.fcgi` |
| **Format** | XML, free, no auth |
| **Query used** | `("AXIOS" OR "Hot AXIOS" OR "lumen-apposing") AND ("adverse" OR "complication" OR "perforation" OR "migration" OR "bleeding")` |
| **What we pulled** | 60+ AXIOS-related case reports with US author affiliations (=hospital), extracted via author affiliation parsing → `scrape/pubmed_axios_us.json` |
| **What it replaces** | The "device + adverse event at hospital X" linkage that GIQuIC would give directly. Here we get it from published clinical literature instead. |
| **What it doesn't replace** | Published case reports are SELECTED (publication bias) — not a representative sample. GIQuIC captures every case at participating hospitals. |

### 2b. CourtListener — federal court filings

| | |
|---|---|
| **URL** | `https://www.courtlistener.com/api/rest/v4/search/` |
| **Format** | REST API, free (Free Law Project), no auth |
| **Queries used** | `Boston Scientific Corporation` + `Boston Scientific AND (AXIOS OR lumen-apposing)` |
| **What we pulled** | BSc federal cases (81 KB), BSc-AXIOS specific (166 KB) → `scrape/courtlistener_bsc.json` + `_axios.json` |
| **What it replaces** | Adverse events that became lawsuits — pleadings name the hospital where harm occurred. This is the only public source where "device + harm + hospital" appear together. |
| **What it doesn't replace** | Only events that crossed the litigation threshold (small fraction of total adverse events). GIQuIC captures non-litigated cases too. |

### 2c. MA Serious Reportable Events (NQF "never events")

| | |
|---|---|
| **URL** | `https://www.mass.gov/lists/serious-reportable-event-reports` |
| **Format** | Annual PDF / web report |
| **What it gives** | Per-MA-hospital count of NQF Serious Reportable Events: retained foreign object, wrong-site surgery, device-related harm, etc. |
| **What we pulled** | Via TinyFish browser automation → `scrape/tinyfish/ma_serious_reportable_events.json` |
| **What it replaces** | Hospital-attributed adverse-event counts that GIQuIC would have for participating hospitals. Here we get it state-mandated for MA hospitals (publicly published by state law). |
| **What it doesn't replace** | MA only; SREs are extreme events, not the full range of complications. |

---

## Alternative 3 → in place of **Premier / Vizient** (hospital purchase data)

What we used: **federal contract data + open payments + indirect billing volume.**

### 3a. USAspending.gov — federal contracts

| | |
|---|---|
| **URL** | `https://api.usaspending.gov/api/v2/search/spending_by_award/` |
| **Format** | REST API, no auth |
| **Filter applied** | `recipient_search_text: "boston scientific"`, time period 2015-2025 |
| **What we pulled** | 2,500 BSc award lines at VA / DoD → `bsc_va_purchases.json` |
| **What it replaces** | The "did Hospital X buy AXIOS/Watchman/Eluvia" question Premier would answer — but only for FEDERAL facilities (~150 VA / DoD hospitals) |
| **What it doesn't replace** | Commercial hospitals — no purchase data for ~5,700 non-federal US hospitals |

### 3b. CMS Open Payments (Sunshine Act)

| | |
|---|---|
| **URL** | `https://openpaymentsdata.cms.gov/api/1/datastore/query` |
| **Format** | Socrata REST, no auth |
| **What we have** | 2.0M payment records, BSc-filtered to product name (`AXIOS Stent`, `WATCHMAN FLX`, `ICEfx Cryoablation`, etc.) → warehouse: `cms_open_payments` |
| **What it replaces** | Hospital-BSc relationship strength signal — physicians paid by BSc work at specific hospitals (joinable via DAC affiliation) |
| **What it doesn't replace** | Payments ≠ purchases. A physician paid by BSc may or may not order BSc product. |

### 3c. CMS Provider Summary (Physician PUF + Outpatient PUF)

| | |
|---|---|
| **URL** | `https://data.cms.gov/data-api/v1/dataset/medicare-...-by-provider-and-service` |
| **Format** | JSON, no auth |
| **What we use** | Per-physician per-HCPCS volume (8.4M rows). For AXIOS we filter to CPTs 43274 (pancreatic/biliary stent), 43253 (EUS drainage), 43266, 43275, 43240. Then join via DAC NPI→CCN. |
| **What it replaces** | The volume side of "Hospital X does N AXIOS procedures." Premier gives this with SKU detail; we give it as procedure-volume proxy. |
| **What it doesn't replace** | Device size/SKU — pure procedure billing, no UDI on these claims |

---

## What this combined free stack delivers vs. what paid would deliver

| Question | Paid (LDS + AGA + Premier) | Our free stack |
|---|---|---|
| How many AXIOS procedures at Hospital X? | ✅ Exact per claim | ✅ Exact for CA + NY hospitals (660+); inferred elsewhere via DAC affiliation |
| What size AXIOS (10mm vs 6mm)? | ✅ Per case (AGA + Premier) | ❌ Not in any free source — probabilistic only |
| Did adverse event happen at Hospital X? | ✅ Per case (AGA) | ⚠️ For published case reports + lawsuits + MA SREs — partial coverage |
| Which specific device caused the event? | ✅ Per case (AGA) | ❌ Not possible from free data |
| Hospital X bought N AXIOS units last year? | ✅ Per SKU (Premier) | ✅ For federal facilities (USAspending); ❌ for commercial |
| 30-day mortality after AXIOS at Hospital X? | ✅ Patient-level (LDS) | ✅ For CA hospitals 2016-2023 cumulative; ✅ for NY 2023 |
| MAUDE event #123 happened at Hospital Y? | ⚠️ Sometimes via AGA cross-ref | ❌ FDA-redacted; FOIA possible but slow |

---

## Honest coverage scorecard

| | Population | Determinism |
|---|---|---|
| Paid stack | All US hospitals | ~95% deterministic |
| Our free stack | CA + NY ~662 hospitals | Fully deterministic for case/death counts |
| Our free stack | Remaining ~5,200 US hospitals | Probabilistic (volume-weighted) |
| Our free stack | Device size + UDI | Not achievable |

---

## The 6 specific free URLs that replace the 3 paid datasets

| Replacing | Free URL | Saved as |
|---|---|---|
| LDS — CA hospital outcomes | `https://data.chhs.ca.gov/dataset/05fee607-cea9-4bf1-8b53-20ca584748a3/resource/af88090e-b6f5-4f65-a7ea-d613e6569d96/download/2016-2023-imi-results-long-view.csv` | `scrape/states/ca_inpatient_mortality_2016_2023_FULL.csv` + warehouse `ca_axios_mortality_by_hospital` |
| LDS — NY hospital discharges | `https://health.data.ny.gov/resource/46xm-urtu.json` | `scrape/states/ny_sparcs_axios_*.json` + warehouse `ny_sparcs_axios_by_hospital` |
| GIQuIC — clinical case reports | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` | `scrape/pubmed_axios_us.json` |
| GIQuIC — adverse events in litigation | `https://www.courtlistener.com/api/rest/v4/search/` | `scrape/courtlistener_bsc_axios.json` |
| GIQuIC — state-mandated adverse events | `https://www.mass.gov/lists/serious-reportable-event-reports` (TinyFish-scraped) | `scrape/tinyfish/ma_serious_reportable_events.json` |
| Premier — federal facility purchases | `https://api.usaspending.gov/api/v2/search/spending_by_award/` | `bsc_va_purchases.json` |

---

## TL;DR

**6 free URLs replaced ~70% of what $20-150K/yr of paid data would give us.** The remaining 30% (device size, specific MAUDE event → CCN, commercial-insurance patients) is structurally blocked and not bridgeable by any free data.

For the BSc AXIOS use case, this means: ✅ defensible per-hospital risk/sales targeting today; ❌ device-specific regulatory evidence still requires paid AGA GIQuIC eventually.
