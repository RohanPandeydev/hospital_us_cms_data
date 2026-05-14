# New Data Sources to Ingest

12 new public data sources to add to the `hospital_us_cms_data` ClickHouse warehouse.

Pipeline plan per source: **Groq** for entity matching / HCPCS classification, **TinyFish** for any non-bulk-download scraping, **ClickHouse** for storage.

After ingestion, table count goes from **17 → 29**.

---

## 🔥 P0 — Build first (4 sources)

### 1. OIG LEIE — Provider Exclusions Database
Federal list of providers/entities excluded from Medicare/Medicaid/all federal healthcare programs. Critical compliance red-flag layer.

- **Landing page:** https://oig.hhs.gov/exclusions/exclusions_list.asp
- **Bulk file (monthly):** https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv
- **Supplemental (reinstatements):** https://oig.hhs.gov/exclusions/downloadables/REIN.csv
- **Data dictionary:** https://oig.hhs.gov/exclusions/downloadables/LEIEDataFileFormat.pdf
- **Search UI:** https://exclusions.oig.hhs.gov/
- **Verification tool:** https://exclusions.oig.hhs.gov/verify.aspx

**Target table:** `oig_leie_exclusions`
**Volume:** ~80K rows
**Ingestion:** direct CSV download (no scraping). Groq to normalize provider names → NPI matches.

---

### 2. Medicare Physician Fee Schedule (PFS) — RVU & Payment File
Per-CPT/HCPCS reimbursement rates for the **office / non-facility setting**. Complements our `opps_addendum_b` which only covers outpatient hospital.

- **Search UI:** https://www.cms.gov/medicare/physician-fee-schedule/search
- **Overview / data dictionary:** https://www.cms.gov/medicare/physician-fee-schedule/search/overview
- **Bulk RVU files landing page:** https://www.cms.gov/medicare/payment/fee-schedules/physician/pfs-relative-value-files
- **Quarterly ZIPs** (example for 2025 Q1): https://www.cms.gov/files/zip/rvu25a-updated-03/06/2025.zip
- **MFS API (data.cms.gov):** https://data.cms.gov/provider-data/dataset/medicare-physician-fee-schedule

**Target table:** `mpfs_rates`
**Volume:** ~10K CPT/HCPCS codes × 4 quarters/year
**Ingestion:** quarterly ZIP from CMS → unzip → parse `PPRRVU{yy}{q}.csv`. Groq for code category enrichment.

---

### 3. Opt-Out Affidavits
Providers who formally opted out of Medicare. Device manufacturers cannot bill Medicare via these providers — disqualifies them as targets.

- **Landing page:** https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/opt-out-affidavits/data
- **Lookup tool:** https://data.cms.gov/tools/provider-opt-out-affidavits-look-up-tool
- **Bulk CSV (most recent):** https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/opt-out-affidavits/data (latest distribution under `accessURL`)

**Target table:** `medicare_opt_out`
**Volume:** ~50K rows
**Ingestion:** direct CSV via `data.cms.gov` DCAT-1.1 catalog.

---

### 4. Provider of Services (POS) File
Quarterly snapshot of **every Medicare-certified facility** — not just hospitals. Adds ASCs, SNFs, dialysis, hospice, home health. **6x expansion of our facility universe.**

- **CMS landing page:** https://www.cms.gov/data-research/statistics-trends-and-reports/provider-services-current-files
- **Quarterly ZIPs:** https://www.cms.gov/files/zip/pos-other-march-2025.zip (and equivalents)
- **Public CSV via DAC:** https://data.cms.gov/provider-data/dataset/provider-of-services-file (search)
- **Data dictionary:** https://www.cms.gov/Research-Statistics-Data-and-Systems/Downloadable-Public-Use-Files/Provider-of-Services

**Target table:** `pos_facilities`
**Volume:** ~45K facilities (ASCs ~6K, SNFs ~15K, dialysis ~8K, hospitals ~5K, hospice ~5K, home health ~10K, FQHC ~1.5K)
**Ingestion:** quarterly ZIP → parse fixed-width or CSV → load. TinyFish to discover the latest quarterly ZIP URL if CMS changes the path.

---

## 🟡 P1 — Next sprint (3 sources)

### 5. Order & Referring NPI File
NPIs eligible to order/refer Medicare-billed items. Lite NPPES substitute (~1M rows vs 7M).

- **Landing page:** https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/order-and-referring/data
- **Bulk CSV:** distributed via `data.cms.gov` DCAT-1.1 catalog under the dataset above

**Target table:** `order_referring_npi`
**Volume:** ~1M rows
**Ingestion:** direct CSV.

---

### 6. Medicare Provider & Supplier Taxonomy Crosswalk
NUCC taxonomy code → CMS specialty mapping. Lets us filter NPIs by clinical specialty.

- **Landing page:** https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/medicare-provider-and-supplier-taxonomy-crosswalk/data
- **NUCC source:** https://taxonomy.nucc.org/

**Target table:** `npi_taxonomy_crosswalk`
**Volume:** ~900 rows
**Ingestion:** direct CSV. Groq to enrich with plain-English category labels.

---

### 7. Medicare Fee-For-Service Public Provider Enrollment
Full FFS enrollment file. Definitive "who is billing-eligible" list with active dates.

- **Landing page:** https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/medicare-fee-for-service-public-provider-enrollment/data

**Target table:** `medicare_ffs_enrollment`
**Volume:** ~2M rows
**Ingestion:** direct CSV.

---

## 🟢 P2 — Nice-to-have (5 sources)

### 8. HCRIS — Hospital Cost Reports
Annual cost reports filed by every Medicare-certified hospital. Worksheet S-10 (uncompensated care), Worksheet A (departmental costs incl. supplies), Worksheet G (capital).

- **Landing page:** https://www.cms.gov/data-research/statistics-trends-and-reports/cost-reports
- **Annual bulk download:** https://www.cms.gov/data-research/statistics-trends-and-reports/cost-reports/hospital-2010-form
- **Direct ZIP (latest fiscal year):** https://www.cms.gov/files/zip/hosp10-reports.zip
- **Form 2552-10 specs:** https://www.cms.gov/regulations-and-guidance/guidance/manuals/paper-based-manuals-items/cms021935

**Target table:** `hcris_hospital_cost_reports`
**Volume:** ~6K hospitals × multiple fiscal years
**Ingestion:** ZIP unpacks to flat ALPHA + NMRC files (CSV format). Groq for worksheet-line description enrichment.

---

### 9. Medicare Revalidation List
Providers due for revalidation (compliance signal).

- **Landing page:** https://data.cms.gov/tools/medicare-revalidation-list
- **API endpoint:** distributed via `data.cms.gov` DCAT-1.1 catalog

**Target table:** `medicare_revalidation`
**Volume:** ~100K rows
**Ingestion:** direct CSV.

---

### 10. Monthly MA & Part D Enrollment by State
Medicare Advantage and Part D contract enrollment by state. Hospital catchment market sizing.

- **Landing page:** https://www.cms.gov/data-research/statistics-trends-and-reports/medicare-advantagepart-d-contract-and-enrollment-data/monthly-enrollment-state
- **Plan Crosswalks:** https://www.cms.gov/data-research/statistics-trends-and-reports/medicare-advantagepart-d-contract-and-enrollment-data/plan-crosswalks

**Target table:** `medicare_ma_partd_enrollment`
**Volume:** ~50 states × ~12 months × ~6K contracts = ~3.6M rows
**Ingestion:** monthly ZIPs → CSV.

---

### 11. Part C/D Performance Data — MA Plan Star Ratings
MA plan star ratings. Useful for hospital network analysis.

- **Landing page:** https://www.cms.gov/medicare/health-drug-plans/part-c-d-performance-data
- **Star Ratings annual zip:** https://www.cms.gov/medicare/health-drug-plans/part-c-d-performance-data/data

**Target table:** `medicare_star_ratings`
**Volume:** ~6K plans × multiple years
**Ingestion:** annual ZIP → CSV.

---

### 12. BETOS Crosswalk
HCPCS → clinical category mapping (e.g., `P5C` = "Cardiovascular procedures — implants").

- **Landing page:** https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners/betos-classification-system
- **Bulk CSV:** distributed via `data.cms.gov` DCAT-1.1 catalog under above dataset

**Target table:** `betos_crosswalk`
**Volume:** ~10K rows
**Ingestion:** direct CSV. Quick win — pure HCPCS enrichment.

---

## Pipeline integration

Each source becomes a new ingester in `src/`:

```
src/
├── oig_leie_ingest.py
├── mpfs_ingest.py
├── opt_out_ingest.py
├── pos_ingest.py
├── order_referring_ingest.py
├── taxonomy_crosswalk_ingest.py
├── ffs_enrollment_ingest.py
├── hcris_ingest.py
├── revalidation_ingest.py
├── ma_partd_enrollment_ingest.py
├── star_ratings_ingest.py
└── betos_ingest.py
```

All wired into `run_pipeline.sh` as a new phase 8 (`--only new_sources`).

## Stack per source

| Tool | Role |
|---|---|
| **TinyFish** | Discover latest quarterly ZIP URLs on CMS landing pages (paths change each release) |
| **Groq** | Entity normalization (LEIE name → NPI), taxonomy → plain-English, HCPCS → BETOS category check, cost-report worksheet-line semantics |
| **ClickHouse** | All storage — `ReplacingMergeTree(fetched_at)` engine, idempotent ingest, `FINAL` reads |

## Cross-join opportunities (the analytical lift)

After all 12 are loaded, these queries become possible:

```sql
-- Compliance red flag: any NPI in our warehouse with an active OIG exclusion
SELECT op.physician_npi, op.physician_name, leie.exclusion_type, leie.exclusion_date
FROM cms_open_payments op
INNER JOIN oig_leie_exclusions leie ON op.physician_npi = leie.npi
WHERE leie.reinstate_date IS NULL;

-- Full revenue picture per HCPCS: hospital + office-setting reimbursement
SELECT h.hcpcs_code, h.short_descriptor,
       opps.payment_rate AS hospital_setting_rate,
       pfs.non_facility_payment AS office_setting_rate,
       (pfs.non_facility_payment - opps.payment_rate) AS site_differential
FROM hcpcs_master h
LEFT JOIN opps_addendum_b opps ON h.hcpcs_code = opps.hcpcs_code
LEFT JOIN mpfs_rates pfs       ON h.hcpcs_code = pfs.hcpcs_code;

-- Full facility universe (hospitals + ASCs + dialysis + SNF)
SELECT facility_type, count() FROM pos_facilities GROUP BY facility_type;

-- Boston Scientific device targets by hospital + ASC, filtered by specialty
SELECT pos.facility_name, pos.facility_type, count(DISTINCT n.npi) AS cardio_mds
FROM pos_facilities pos
INNER JOIN npi_facility_affiliation n ON pos.ccn = n.facility_ccn
INNER JOIN npi_taxonomy_crosswalk t ON n.taxonomy = t.taxonomy_code
WHERE t.medicare_specialty LIKE '%cardiology%'
GROUP BY pos.facility_name, pos.facility_type
ORDER BY cardio_mds DESC;
```
