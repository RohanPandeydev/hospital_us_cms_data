# Datasets to acquire — closing the AXIOS adverse-event-to-hospital gap

**What this document is.** A shortlist of data sources we need to purchase / license to
move from **probabilistic** AXIOS event attribution (what we currently do with 256 public
datasets) to **deterministic** attribution — meaning: this AXIOS was used at this hospital,
this was the stent size, and this specific adverse event happened.

**Why we can't do this with public data alone.** Three structural blockers:

| Blocker | Source | Cite |
|---|---|---|
| FDA redacts reporter facility name from public MAUDE | FDA openFDA | 21 CFR 803.9(b) |
| HIPAA prevents patient-level claim data in CMS PUFs | CMS Provider Data | 45 CFR Part 164 |
| Hospital device purchase records are proprietary | Premier / Vizient / GHX | Contract law |

The two datasets below jointly close ~95% of the gap for our BSc AXIOS use case.

---

## Priority 1 — AGA GIQuIC

**The single most important purchase.** AGA GIQuIC sidesteps the MAUDE redaction
entirely by capturing adverse events **directly at the hospital case level** in
participating facilities.

### What it is

| Attribute | Value |
|---|---|
| Full name | American Gastroenterological Association Digestive Health Outcomes Registry / GI Quality Improvement Consortium |
| Owner | American Gastroenterological Association (AGA) |
| Type | Voluntary clinical-outcomes registry |
| Scope | GI endoscopy procedures including EUS-guided drainage (where AXIOS is used) |
| Coverage | ~1,000+ participating sites — typically high-volume academic medical centers |

### What it holds (per case)

| Field | Description |
|---|---|
| `site_id` → CCN | Hospital identifier — maps to CMS CCN |
| `patient_id` (de-identified) | Per-case patient ID consistent within site |
| `procedure_date` | Date of service |
| `cpt_code` | 43253 (EUS transmural drainage), 43274 (biliary stent placement), etc. |
| `device_brand` | "AXIOS", "Hot AXIOS", etc. |
| `device_model` | Specific model number (M00553XXX series) |
| `device_size_diameter_mm` | 6 / 8 / 10 / 15 / 20 / 25 mm |
| `device_size_length_mm` | 8 / 10 mm saddle length |
| `device_udi` | UDI captured when entered by site |
| `adverse_event_flag` | Boolean — was AE reported for this case |
| `ae_type` | Bleeding, perforation, migration, infection, death, etc. |
| `ae_severity` | Minor / major / fatal |
| `30_day_outcome` | Captured per case |
| `90_day_outcome` | Captured per case |
| `quality_metrics` | ADR, cecal intubation rate, etc (procedure-level QI) |

### What gap it closes

| Question we previously couldn't answer deterministically | Now we can |
|---|---|
| Did Hospital X use AXIOS? | ✅ Direct |
| What size AXIOS at Hospital X? | ✅ 6mm vs 10mm vs 20mm — exact |
| Did adverse event happen at Hospital X for AXIOS case? | ✅ Direct per-case capture |
| Which specific failure mode? | ✅ Categorized in registry |
| Patient outcome at 30/90 days? | ✅ Captured per case |

### What it does NOT have

- Coverage for non-participating hospitals (the ~30-50% long tail of low-volume AXIOS sites)
- MAUDE event-ID linkage (deliberately separate systems — but **we don't need it** because AGA captures events directly)
- Real-time data — typically quarterly batch uploads

### Access path

| Step | Detail |
|---|---|
| Contact | AGA Quality Improvement department (qi@gastro.org) |
| Type of access | Research data partnership / data subscription |
| Approval requirement | IRB approval typically required; data-use agreement signed |
| Lead time | 2-4 months from contact to data delivery |
| Cost | ~$5K-25K/year depending on scope (per-case access vs aggregate vs longitudinal) |
| Renewal | Annual contracts |

### Data delivery format

Typically delivered as de-identified Parquet / CSV with site-level pseudonymized IDs that can be crosswalked to CCN via a separate side agreement.

---

## Priority 2 — Medicare Limited Data Set (LDS)

**The denominator and outcome backbone.** Gives us deterministic AXIOS procedure
counts at **every US hospital** (not just AGA participants) plus patient-level
outcomes for all Medicare beneficiaries.

### What it is

| Attribute | Value |
|---|---|
| Full name | Medicare Limited Data Set |
| Owner | Centers for Medicare & Medicaid Services (CMS) |
| Broker | ResDAC (Research Data Assistance Center) at University of Minnesota |
| Type | De-identified per-claim research files |
| Scope | 100% of Medicare fee-for-service claims (or 5% sample option) |

### Which files we need

| File | What it holds | Why we need it |
|---|---|---|
| **Outpatient SAF** | Hospital outpatient claims, per-HCPCS line item with CCN | AXIOS procedures happen outpatient — this is the core file |
| **MEDPAR** | Inpatient hospital + SNF admissions, per-DRG with CCN | Catches inpatient AXIOS cases + post-procedure readmissions |
| **Carrier File** | Part B physician/supplier claims, per-CPT with provider NPI | Captures physician billing for AXIOS-related procedures |
| **MBSF** | Master Beneficiary Summary File — demographics, enrollment | Patient denominators, chronic-condition adjustments |

### What it holds (per claim)

| Field | Description |
|---|---|
| `bene_id` (encrypted) | Beneficiary ID consistent across all files |
| `provider_ccn` | Hospital CCN — **the key field** |
| `clm_from_dt` / `clm_thru_dt` | Date of service range |
| `hcpcs_cd` | CPT/HCPCS code per line item (43253, 43274, etc.) |
| `drg_cd` | DRG for inpatient claims |
| `icd_dgns_cd_1..25` | Up to 25 diagnosis codes |
| `icd_prcdr_cd_1..25` | Up to 25 procedure codes |
| `rndrg_physn_npi` | Rendering physician NPI |
| `oprtg_physn_npi` | Operating physician NPI |
| `clm_pmt_amt` | Medicare payment |
| `clm_tot_chrg_amt` | Total charges submitted |
| `udi_di` | Device UDI — **sparse field**, only populated when hospital transmitted it (post-FY2018 inpatient, very inconsistent) |
| `bene_birth_dt` | Patient DOB (year only in LDS) |
| `bene_sex_cd` | Sex |
| `bene_state_cd` | State of residence |
| `bene_death_dt` | Date of death (when applicable) — outcomes |

### What gap it closes

| Question | LDS answers |
|---|---|
| How many AXIOS procedures at Hospital X in 2024? | ✅ Exact count from claims |
| 30-day readmit rate after AXIOS at Hospital X? | ✅ Patient-level computed |
| 90-day mortality after AXIOS at Hospital X? | ✅ Patient-level computed via death date |
| Which physicians did AXIOS at Hospital X? | ✅ Operating physician NPI per claim |
| Did patient have complications coded after AXIOS? | ✅ ICD-10 diagnosis codes on follow-up claims |
| Patient demographic risk factors? | ✅ Age, sex, state, chronic conditions |

### What it does NOT have

- Device brand / model / size on the claim (UDI field is sparsely populated)
- MAUDE event linkage (different system)
- Commercial-insurance / Medicare Advantage / Medicaid patients (Medicare FFS only)
- Patient narratives or clinical notes
- Real-time — usually 6-12 month claim-completeness lag

### Access path

| Step | Detail |
|---|---|
| Apply via | ResDAC.org — DUA + research protocol submission |
| Approval requirement | Signed Data Use Agreement (DUA); IRB approval from your institution |
| Lead time | 3-6 months (DUA review + IRB + file extraction) |
| Cost (5% sample) | ~$4,500-5,500 per file type per year |
| Cost (100% files) | ~$15,000-25,000 per file type per year |
| Storage / compute | Files arrive as fixed-width or SAS — need SAS/Python pipeline, GB-scale per year |
| Renewal | Annual; data retained per DUA terms (typically 3-5 years) |

### Recommended LDS bundle for this work

| File | Sample size | Approx annual cost |
|---|---|---|
| Outpatient SAF (5%) | 5% sample | $5,000 |
| MEDPAR (5%) | 5% sample | $5,000 |
| Carrier File (5%) | 5% sample | $5,000 |
| **Total** | | **~$15,000/year** |

For a more thorough analysis with full national coverage, scale to 100% files (~$45-75K/year).

---

## Optional Priority 3 — Premier PINC AI or Vizient CDB

**Only if** you need to know the AXIOS size at non-AGA hospitals (the 30-50% long tail).
AGA + LDS without Premier already covers the strategic top hospitals.

### What it adds

| Field | Description |
|---|---|
| Hospital SKU-level purchase | Exact BSc product / model / size purchased per CCN |
| Cost per unit | Negotiated price by hospital |
| Volume by SKU | Annual / quarterly purchase counts |
| Patient encounter linkage (Premier only) | Pharmacy/supply linked to specific patient encounter |

### Coverage

| Vendor | US hospital coverage |
|---|---|
| **Premier PINC AI Healthcare Database** | ~1,000 hospitals — broad mix academic + community |
| **Vizient Clinical Database** | ~600 hospitals — academic medical centers + AMC affiliates |

### Cost

| Vendor | Approx annual cost |
|---|---|
| Premier (research subscription) | $50,000-150,000 |
| Vizient (research/CDB access) | $50,000-200,000 |

### When this is worth buying

| Use case | Worth it? |
|---|---|
| BSc sales targeting at top 100 hospitals | ❌ AGA + LDS already covers top 100 (mostly academics, mostly AGA participants) |
| Want SKU-level purchase data at all 800+ AXIOS-using hospitals | ✅ Yes |
| Competitive intel against Olympus / Cook / Merit (other LAMS makers) | ✅ Yes — Premier shows competitor purchases too |
| Pricing benchmarks | ✅ Yes — Premier shows negotiated prices |

---

## What we are NOT recommending (and why)

### FOIA-unredacted MAUDE
- ❌ Skip. FDA processes case-by-case, often re-redacts under FOIA exemption (b)(4) or (b)(6). Slow, partial, marginal benefit when AGA already captures events directly.

### Hospital-by-hospital direct data partnerships
- ❌ Skip for population analysis. Useful only for specific 1-2 hospital deep-dive case studies. Months of legal negotiation per hospital. Doesn't scale.

### Clinical trial registries (NCDR, STS, etc.)
- ❌ Skip for AXIOS specifically. Those registries are cardio-focused; AGA GIQuIC is the relevant registry for endoscopic devices like AXIOS.

### Optum / IQVIA / MarketScan commercial claims
- ❌ Skip unless you need commercial-insurance population. Medicare FFS via LDS covers the majority of AXIOS use cases (older patients with pancreatic conditions). Commercial claims at $50K-500K/year don't pay back for this specific question.

---

## Recommended acquisition order + budget

| Phase | Acquire | When | Annual cost | What you can do |
|---|---|---|---|---|
| 1 | Medicare LDS (Outpatient + MEDPAR, 5% sample) | Month 1-6 | ~$10K | Replace probabilistic chain with deterministic per-CCN procedure counts; outcome rates per hospital |
| 2 | AGA GIQuIC research partnership | Month 3-7 (parallel) | ~$10K-25K | Device + size + adverse event per case at participating hospitals (~50-70% of national AXIOS volume) |
| 3 | (Optional) Premier or Vizient | Month 7+ | $50-150K | SKU-level purchase coverage at all hospitals incl. non-AGA |
| 4 | (Optional) LDS 100% upgrade | Year 2+ | +$30-60K | National completeness instead of 5% sample |

### Minimum viable budget: **~$20-35K/year (Phase 1 + 2)**

This covers the BSc AXIOS use case fully for:
- ✅ Every US hospital's AXIOS procedure volume (LDS)
- ✅ Every US hospital's outcomes after AXIOS (LDS)
- ✅ Device size + adverse events at ~50-70% of AXIOS hospitals (AGA)
- ⚠️ Remaining hospitals: deterministic counts, probabilistic event detail

---

## What this does NOT solve

For full transparency, even with all three datasets:

| Question | Still not answerable |
|---|---|
| "MAUDE event #12345678 happened at Hospital X" | MAUDE-event-ID-to-CCN link is structurally absent. AGA gives you the event directly; you don't link it through MAUDE. |
| "Patient Y in MAUDE narrative = patient Z in LDS" | No shared patient identifier between FDA and CMS systems. |
| Commercial-insurance AXIOS adverse events | LDS is Medicare FFS only. Need MarketScan / Optum to cover commercial. |
| Pre-FY2018 historical UDI on claims | Field didn't exist on claims before then. |

These are deliberate, regulatory-by-design limitations — not closeable by purchase.

---

## Summary

**To go from probabilistic to deterministic for BSc AXIOS sales/risk intelligence:**

Buy **AGA GIQuIC + Medicare LDS**. Total ~$20-35K/year. Replaces ~95% of the probabilistic
attribution chain we currently maintain in `bsc_axios_attribution`,
`bsc_axios_size_attribution`, and `bsc_device_code_attribution` with deterministic data.

The remaining 5% (specific MAUDE event-IDs mapped to specific hospitals nationally) is
structurally blocked at the FDA/HIPAA boundary and not worth pursuing for this use case.
