# CMS Hospital Data → FDA Device Linkage — Implementation Plan

**Goal**: Assemble every publicly available CMS hospital dataset + FDA device adverse-event data into a single local Postgres warehouse, linked so we can answer:

> *"For hospital X, what's the correlation between device Y adverse events and patient outcomes (readmission, complication, mortality)?"*

## How to run

```bash
cd /Users/rohankumarpandey/Finn/asylum/hospital_us_cms_data
source .venv/bin/activate
python run.py ingest       # ONE command. Creates DB, applies schema, fetches everything.
python run.py status       # audit history
```

Re-runs are idempotent (`ON CONFLICT` upserts).

---

## Phase 1 — CMS Provider Data (READY, 13 datasets) ✅

Source: `https://data.cms.gov/provider-data/api/1/datastore/query/{id}/0` (DKAN/Socrata)

### Hospital master + outcomes
| ID | Dataset | Row count | Purpose |
|---|---|---:|---|
| `xubh-q36u` | Hospital General Information | 5,426 | **Master facility table** (CCN, address, type, 5★ rating) |
| `ynj2-r877` | Complications & Deaths | 95,780 | PSI scores, MORT rates, complication rates |
| `632h-zaca` | Unplanned Hospital Visits | 67,046 | READM_30, EDAC leading-indicator |
| `9n3s-kdb3` | HRRP Readmissions | 18,330 | **ERR = primary ML regression target** |
| `77hc-ibv8` | Healthcare Associated Infections | 172,404 | **SIR for CLABSI, CAUTI, MRSA — direct device-caused** |
| `muwa-iene` | PSI-90 Composite | ~5k | Safety composite |
| `dgck-syfz` | HCAHPS Patient Survey | ~50k | Patient experience |

### Device-relevant additions
| ID | Dataset | Why |
|---|---|---|
| `tqkv-mgxq` | CJR Joint Replacement Model | **Direct hip/knee device outcomes** |
| `4jcv-atw7` | ASC Quality Measures | **Has NPI — bridge to FDA** |
| `48nr-hqxx` | ASC OAS CAHPS Survey | Surgery center patient experience |

### Benchmarks + reference
| ID | Dataset | Why |
|---|---|---|
| `bs2r-24vh` | Complications - State | Geographic benchmark |
| `qqw3-t4ie` | Complications - National | National benchmark |
| `y9us-9xdf` | Footnote Crosswalk | Decodes 45k+ footnote codes |

### Storage model

```
cms_hospitals                 — master (PK: facility_id, 5,426 rows)
cms_hospital_measures         — unified per-facility measures (all "- Hospital" datasets)
cms_state_measures            — state benchmarks
cms_national_measures         — national benchmarks
cms_wide_facility_snapshots   — wide-format datasets (CJR, ASC) — has NPI
cms_footnote_crosswalk        — reference table for footnote codes
cms_ingestion_log             — per-run audit
```

Every table keeps the full source row in a `raw JSONB` column — no field is lost.

---

## Phase 2 — CMS `data-api/v1` datasets (NEXT)

Source: `https://data.cms.gov/data-api/v1/dataset/{uuid}/data?size=N&offset=M` — same pagination idea, different URL prefix, much larger datasets (millions of rows).

### Device-billing (highest device-linkage value)
| UUID | Dataset | Why critical |
|---|---|---|
| `a2d56d3f-3531-4315-9d87-e29986516b41` | Medicare DMEPOS by Supplier | Supplier NPI + device HCPCS codes |
| `1746a83e-bb65-4300-8e02-21edbab77c6b` | **Medicare DMEPOS by Supplier and Service** | **Per-HCPCS device billing volumes** |
| `f8603e5b-9c47-4c52-9b47-a4ef92dfada4` | Medicare DMEPOS by Referring Provider | Referring physician NPI |
| `86b4807a-d63a-44be-bfdf-ffd398d5e623` | DMEPOS by Referring Provider & Service | Per-physician device volumes |
| `27c150fd-8578-43b1-bba5-6388987e32af` | DMEPOS by Geography & Service | Regional device usage |

### Hospital procedure volumes (denominators for rate normalization)
| UUID | Dataset |
|---|---|
| `ee6fb1a5-39b9-46b3-a980-a7284551a732` | Medicare Inpatient Hospitals - by Provider |
| `690ddc6c-2767-4618-b277-420ffb2bf27c` | **Inpatient by Provider & Service (MS-DRG-level)** |
| `ccbc9a44-40d4-46b4-a709-5caa59212e50` | Outpatient by Provider & Service (APC-level) |

### Physician volumes (NPI↔device linkage)
| UUID | Dataset |
|---|---|
| `8889d81e-2ee7-448f-8713-f071038289b5` | Medicare Physician - by Provider |
| `92396110-2aed-4d63-a6a2-5d6207d46a29` | Physician - by Provider & Service (HCPCS) |

### Other
- **Open Payments** — on `openpaymentsdata.cms.gov` (DKAN, same shape as our current client). Gives manufacturer → provider $ ties.
- **NPPES NPI Registry** — monthly full-file download (~8 GB), not an API.
- **Hospital Change of Ownership** — `c04031db-54ce-461c-85d1-d2613d71f167`

### Phase 2 build tasks
1. Add `CMSDataApiClient` (new class, same pagination contract as `CMSClient` but different URL shape)
2. Add `source` field to dataset registry ("provider_data" | "data_api" | "open_payments")
3. Update `ingest.py` to pick the right client per dataset
4. Create tables: `cms_dmepos_by_supplier`, `cms_dmepos_by_supplier_service`, `cms_inpatient_by_provider`, etc. (these datasets have distinct schemas — dedicated typed tables beat JSONB here)
5. Add NPI index across all tables
6. Handle dataset-specific quirks (DMEPOS suppresses cells with <10 beneficiaries)

---

## Phase 3 — FDA Device Side (THE POINT OF THIS)

Source: openFDA APIs (no key needed, rate limit 240 req/min unauth)

| Endpoint | What it gives |
|---|---|
| `https://api.fda.gov/device/event.json` | **MAUDE adverse events** — brand, manufacturer, product_code, UDI, lot, outcome, facility name/ZIP |
| `https://api.fda.gov/device/udi.json` | GUDID — UDI master, maps UDI → product_code, brand, manufacturer |
| `https://api.fda.gov/device/recall.json` | Device recalls — product_code, firm, reason |
| `https://api.fda.gov/device/510k.json` | 510(k) clearances |
| `https://api.fda.gov/device/pma.json` | PMA approvals |

### Tables
```
fda_maude_events           — raw adverse events (report_number PK)
fda_maude_devices          — per-event device info (UDI, product_code, brand, model, lot)
fda_maude_patients         — per-event patient outcome codes
fda_gudid_devices          — UDI master (primary_di PK)
fda_recalls                — device recalls
fda_product_codes          — static crosswalk (CDRH product code catalog)
```

---

## Phase 4 — LINKAGE (the ML foundation)

### Bridge tables built in Postgres

```sql
-- Bridge 1: product_code ↔ CMS measure (static seed, maintained manually)
CREATE TABLE bridge_product_code_to_measure (
    product_code  TEXT,
    cms_measure_id TEXT,
    device_category TEXT,
    confidence    TEXT,  -- 'direct' | 'indirect' | 'proxy'
    PRIMARY KEY (product_code, cms_measure_id)
);
-- Seed examples:
--   KZH → READM_30_HIP_KNEE, COMP_HIP_KNEE   (hip prosthesis)
--   LWS → READM_30_AMI, MORT_30_AMI           (coronary stent)
--   FRN → HAI_1 (CLABSI)                      (CV catheter)
--   GBI → HAI_2 (CAUTI)                       (urinary catheter)

-- Bridge 2: FDA facility → CMS facility_id (populated via ZIP + fuzzy name match)
CREATE TABLE bridge_fda_facility_to_ccn (
    fda_facility_key  TEXT,  -- hash of facility_name + state + zip
    cms_facility_id   TEXT,  -- CCN
    match_method      TEXT,  -- 'zip_exact' | 'fuzzy_jaro_0.92' | 'npi'
    match_score       NUMERIC,
    verified          BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (fda_facility_key)
);

-- Bridge 3: NPI ↔ CCN (via ASC dataset + NPPES)
CREATE TABLE bridge_npi_to_ccn (
    npi             TEXT PRIMARY KEY,
    facility_id     TEXT,
    source          TEXT,   -- 'asc_4jcv' | 'nppes' | 'dmepos'
    confidence      TEXT
);
```

### Unified view / materialized view

```sql
CREATE MATERIALIZED VIEW hospital_device_risk AS
SELECT
    h.facility_id,
    h.facility_name,
    h.state,
    h.overall_rating,
    -- CMS outcomes
    err.score_num  AS readm_30_hip_knee_err,
    psi.score_num  AS psi_09_hemorrhage_rate,
    sir.score_num  AS hai_1_clabsi_sir,
    -- FDA signals
    fda.event_count              AS maude_events_hip_knee,
    fda.hospitalization_count    AS maude_hosp_hip_knee,
    fda.death_count              AS maude_death_hip_knee,
    -- Volume denominator
    ipps.total_discharges        AS inpatient_discharges,
    ipps.hip_knee_discharges     AS hip_knee_procedure_volume,
    -- Normalized rate
    fda.event_count::float / NULLIF(ipps.hip_knee_discharges, 0) * 100
        AS maude_events_per_100_procedures
FROM cms_hospitals h
LEFT JOIN cms_hospital_measures err
       ON err.facility_id = h.facility_id AND err.measure_id = 'READM-30-HIP-KNEE-HRRP'
LEFT JOIN cms_hospital_measures psi
       ON psi.facility_id = h.facility_id AND psi.measure_id = 'PSI_09'
LEFT JOIN cms_hospital_measures sir
       ON sir.facility_id = h.facility_id AND sir.measure_id = 'HAI_1_SIR'
LEFT JOIN fda_events_by_hospital fda
       ON fda.facility_id = h.facility_id AND fda.device_category = 'hip_knee'
LEFT JOIN inpatient_by_hospital ipps
       ON ipps.facility_id = h.facility_id;
```

---

## Execution checklist

- [x] Phase 1: 13 CMS Provider Data datasets ingesting
- [ ] Phase 2: data-api/v1 client + 9 DMEPOS/Inpatient/Physician datasets
- [ ] Phase 2b: Open Payments via DKAN with `openpaymentsdata.cms.gov` base URL
- [ ] Phase 3: FDA openFDA ingestion (MAUDE + GUDID + recalls)
- [ ] Phase 4: Build 3 bridge tables + materialized view
- [ ] Phase 5: Expose REST APIs on the Go backend (`rp360-backend-golang`) reading from this Postgres warehouse

---

## Data linkage summary diagram

```
FDA openFDA                           CMS data.cms.gov
─────────────────                     ──────────────────
MAUDE events                          Provider Data (Phase 1)
  ├─ product_code ──┐                   ├─ Hospitals (CCN)
  ├─ brand_name     │                   ├─ Measures (ERR, SIR, PSI)
  ├─ UDI ───┐       │                   └─ ASC (NPI ↔ CCN)
  ├─ fac_name/ZIP ──┼── Bridge 2 ─────► data-api/v1 (Phase 2)
  └─ NPI ───────────┼── Bridge 3 ─────►   ├─ DMEPOS (NPI + HCPCS)
                    │                     └─ Inpatient (CCN + MS-DRG)
GUDID ──────────────┘
  (UDI → product_code)    Bridge 1
                          (product_code ↔ CMS measure)
```
