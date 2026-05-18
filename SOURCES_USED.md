# Data Sources Used — BSc AXIOS Attribution

> **Scope:** Every external source we fetched data from during this work. USA-only. Free/public unless noted.
> **Last updated:** 2026-05-18

---

## 1. Federal — FDA (openFDA public API)

All endpoints free, no auth, JSON.

| # | Source | URL | What it gives | Output file |
|---|---|---|---|---|
| 1 | **FDA MAUDE — Adverse Event Reports** | `https://api.fda.gov/device/event.json` | Device-reported adverse events; 25M total, 1,071 BSc AXIOS-filtered | warehouse: `default.flattened_adverse_event` |
| 2 | **FDA UDI / GUDID device catalog** | `https://api.fda.gov/device/udi.json` | Unique Device Identifier records; 4.9M devices | warehouse: `default.raw_udi` |
| 3 | **FDA 510(k) clearances** | `https://api.fda.gov/device/510k.json` | Premarket notifications; 180K clearances | warehouse: `default.raw_510k` |
| 4 | **FDA PMA (Premarket Approval)** | `https://api.fda.gov/device/pma.json` | Class III device approvals; 56K PMAs | warehouse: `default.raw_pma` |
| 5 | **FDA Device Recalls** | `https://api.fda.gov/device/recall.json` | Recall metadata; 58K records | warehouse: `default.raw_recall` |
| 6 | **FDA Device Enforcement** | `https://api.fda.gov/device/enforcement.json` | Enforcement actions; 77K records | warehouse: `default.raw_enforcement` |
| 7 | **FDA Warning Letters** | `https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/warning-letters` | Compliance/enforcement letters; 171K | warehouse: `default.raw_warning_letters` |
| 8 | **FDA Facility Inspections + 483 citations** | `https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/inspection-references` | Inspection findings; 533K | warehouse: `default.raw_inspections_citations` |

---

## 2. Federal — CMS (data.cms.gov)

### 2a. CMS Provider Data Catalog (DCAT-1.1)

| # | Source | URL | What it gives | Output |
|---|---|---|---|---|
| 9 | **CMS catalog index** | `https://data.cms.gov/data.json` | 234-dataset DCAT-1.1 catalog | `backend/cms_data/catalog.json` (fda-dashboard repo) |
| 10 | **Hospital General Information** | `https://data.cms.gov/provider-data/dataset/xubh-q36u` | 5,852 hospital roster | warehouse: `cms_hospitals` |
| 11 | **Complications and Deaths — Hospital** | `https://data.cms.gov/provider-data/dataset/ynj2-r877` | 95,840 rows PSI/mortality | warehouse: `cms_hospital_measures` |
| 12 | **CMS Medicare PSI-90 Composite** | `https://data.cms.gov/provider-data/dataset/muwa-iene` | 52,361 rows | warehouse: `cms_hospital_measures` |
| 13 | **Unplanned Hospital Visits — Hospital** | `https://data.cms.gov/provider-data/dataset/632h-zaca` | 67,088 readmit/EDAC rows | warehouse: `cms_hospital_measures` |
| 14 | **Patient-Reported Outcomes — Hospital** | `https://data.cms.gov/provider-data/dataset/mxtu-43qs` | 4,628 THA/TKA PROM rows | warehouse: `cms_hospital_measures` |
| 15 | **Hospital VBP — Safety (FY2026)** | `https://data.cms.gov/provider-data/dataset/dgmq-aat3` | 2,455 hospitals SEP-1+HAI scores | warehouse: `hospital_vbp_safety` |
| 16 | **Hospital-Acquired Conditions (HAC)** | `https://data.cms.gov/provider-data/dataset/yq43-i98g` | 12,120 HAC rate rows | warehouse: `hospital_hac_measures` |
| 17 | **Healthcare-Associated Infections (HAI)** | `https://data.cms.gov/provider-data/dataset/77hc-ibv8` | 5,262 CLABSI/CAUTI/SSI rows | warehouse: `cms_hospital_measures` |
| 18 | **Nursing Home Health Deficiencies** | `https://data.cms.gov/provider-data/dataset/r5ix-sfxw` | 418,148 Form-2567 citations | warehouse: `snf_health_deficiencies` |
| 19 | **Citation Code Look-up (F-tags)** | data.cms.gov provider-data | 643 tag descriptions | warehouse: `snf_citation_codes` |

### 2b. CMS Medicare PUFs (data.cms.gov/data-api)

| # | Source | URL | What it gives | Output |
|---|---|---|---|---|
| 20 | **Medicare Physician & Other Practitioners PUF** | `https://data.cms.gov/data-api/v1/dataset/.../medicare-physician-other-practitioners-by-provider-and-service` | 8.4M rows per-NPI per-HCPCS | warehouse: `cms_provider_summary` |
| 21 | **Medicare Inpatient Hospitals PUF** | `https://data.cms.gov/data-api/v1/dataset/medicare-inpatient-hospitals-by-provider-and-service` | 145.9K rows per-CCN per-DRG | warehouse: `cms_provider_summary` |
| 22 | **Medicare Outpatient Hospitals PUF** | `https://data.cms.gov/data-api/v1/dataset/medicare-outpatient-hospitals-by-provider-and-service` | 116.8K rows per-CCN per-APC | warehouse: `cms_provider_summary` |
| 23 | **Medicare DMEPOS PUF** | `https://data.cms.gov/data-api/v1/dataset/medicare-durable-medical-equipment-devices-supplies` | 5.5M supplier-service rows | warehouse: `cms_provider_summary` |
| 24 | **CMS Open Payments (Sunshine Act)** | `https://openpaymentsdata.cms.gov/api/1/datastore/query` | 2.0M manufacturer-physician payments | warehouse: `cms_open_payments` |

### 2c. CMS other (cms.gov ZIPs + DAC)

| # | Source | URL | What it gives | Output |
|---|---|---|---|---|
| 25 | **OPPS Addendum B (quarterly)** | `https://www.cms.gov/files/zip/january-2026-opps-addendum-b.zip` | 47,528 HCPCS payment rules | warehouse: `opps_addendum_b` |
| 26 | **CMS DAC — Facility Affiliation** | `https://data.cms.gov/provider-data/dataset/mj5m-pzi6` | 1.6M NPI→CCN links | warehouse: `npi_facility_affiliation` |
| 27 | **HCPCS Level II Code Set (quarterly)** | `https://www.cms.gov/files/zip/april-2026-alpha-numeric-hcpcs-file.zip` | 18,136 codes | warehouse: `hcpcs_master` |
| 28 | **Medicare PFS PPRRVU (RBRVS)** | `https://www.cms.gov/files/zip/rvu26d.zip` | Per-CPT RVUs | warehouse: `mpfs_rates` |
| 29 | **CMS BETOS classification** | `https://data.cms.gov/data-api/v1/dataset/restructured-betos-classification-system` | BETOS/RBCS crosswalk | warehouse: `betos_crosswalk` |
| 30 | **HCRIS Hospital Cost Reports** | `https://data.cms.gov/sites/default/files/...HOSPITAL_CCN_FY{year}.csv` | Cost report filings | warehouse: `hcris_hospital_cost_reports` |
| 31 | **CMS Provider of Services (POS) file** | `https://data.cms.gov/data.json` (DCAT) | ~6K facilities universe | warehouse: `pos_facilities` |
| 32 | **CMS Medicare Opt-Out** | `https://data.cms.gov/data.json` | Opt-out NPIs | warehouse: `medicare_opt_out` |
| 33 | **CMS Order & Referring** | `https://data.cms.gov/data.json` | DMEPOS-eligible NPIs | warehouse: `order_referring_npi` |
| 34 | **CMS FFS Enrollment** | `https://data.cms.gov/data.json` | Beneficiary enrollment | warehouse: `medicare_ffs_enrollment` |
| 35 | **Medicare MA/Part D Enrollment** | `https://www.cms.gov/files/zip/medicare-monthly-enrollment-snapshot.zip` | MA plan enrollment | warehouse: `medicare_ma_partd_enrollment` |
| 36 | **CMS Revalidation Due List** | `https://data.cms.gov/data.json` | Revalidation schedule | warehouse: `medicare_revalidation` |
| 37 | **NPI Taxonomy Crosswalk** | NUCC / `https://data.cms.gov/data.json` | Specialty crosswalk | warehouse: `npi_taxonomy_crosswalk` |
| 38 | **CMS Care Compare Star Ratings** | `https://www.medicare.gov/care-compare/` | 1-5 star ratings per hospital | warehouse: `medicare_star_ratings` |

---

## 3. Federal — Other (USA-focused)

| # | Source | URL | What it gives | Output |
|---|---|---|---|---|
| 39 | **USAspending.gov** | `https://api.usaspending.gov/api/v2/search/spending_by_award/` | BSc federal contracts (VA/DoD) 2015-2025 | `bsc_va_purchases.json` |
| 40 | **ClinicalTrials.gov v2 API** | `https://clinicaltrials.gov/api/v2/studies` | BSc US trials, AXIOS / spinal cord stim | warehouse: `clinical_trials` + `scrape/clinicaltrials_axios_us.json` |
| 41 | **OIG LEIE Excluded Providers** | `https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv` | Excluded NPIs/entities | warehouse: `oig_leie_exclusions` |
| 42 | **PubMed E-utils (NCBI)** | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` `efetch.fcgi` | 60+ AXIOS case reports w/ US affiliations | `scrape/pubmed_axios_us.json` |
| 43 | **NIH RePORTER API** | `https://api.reporter.nih.gov/v2/projects/search` | NIH-funded AXIOS research at US institutions | `scrape/nih_reporter_axios_us.json` |
| 44 | **PCORI funded research** | `https://www.pcori.org/api/research/results` | PCORI grants matching AXIOS keywords | `scrape/pcori_axios_us.json` |
| 45 | **CourtListener API v4 (Free Law Project)** | `https://www.courtlistener.com/api/rest/v4/search/` | BSc federal lawsuits, AXIOS-specific | `scrape/courtlistener_bsc.json` + `_axios.json` |
| 46 | **SEC EDGAR submissions** | `https://data.sec.gov/submissions/CIK0000885725.json` | BSc 10-K / 10-Q / 8-K filings | `scrape/sec_edgar_bsc.json` |
| 47 | **AHRQ HCUPnet** | `https://datatools.ahrq.gov/api/v1/hcupnet/` | National hospital aggregate queries | indexed (no data pulled yet) |
| 48 | **AGA Quality Initiatives page** | `https://www.gastro.org/practice-guidance/quality` | GIQuIC registry info + quality links | `scrape/aga_quality_us.json` |

---

## 4. State open-data portals (USA discharge / quality data)

### 4a. New York (deterministic per-discharge)

| # | Source | URL | What it gives | Output |
|---|---|---|---|---|
| 49 | **NY SPARCS Hospital Inpatient Discharges 2023** | `https://health.data.ny.gov/resource/46xm-urtu.json` | 30-field per-discharge with `permanent_facility_id`, DRG, deaths, LOS, charges | `scrape/states/ny_sparcs_axios_relevant_2023.json` (500 rows) + warehouse: `ny_sparcs_axios_by_hospital` (108 hospitals) |
| 50 | **NY SPARCS ED Treat-and-Release** | `https://health.data.ny.gov/api/views.json?q=sparcs` | NY ED visits by hospital | `scrape/states/ny_emergency_department_treat_and_release_encounters_.json` |
| 51 | **NY data.ny.gov catalog** | `https://health.data.ny.gov/api/views.json` | SPARCS dataset discovery index | indexed |

### 4b. California (deterministic per-hospital aggregates)

| # | Source | URL | What it gives | Output |
|---|---|---|---|---|
| 52 | **CA HCAI Inpatient Mortality Indicators 2016-2023** | `https://data.chhs.ca.gov/dataset/05fee607-cea9-4bf1-8b53-20ca584748a3/resource/af88090e-b6f5-4f65-a7ea-d613e6569d96/download/2016-2023-imi-results-long-view.csv` | 60,498 rows × 11 fields. 554 CA hospitals, per-hospital cases + deaths for pancreatic + GI conditions over 8 years | `scrape/states/ca_inpatient_mortality_2016_2023_FULL.csv` (6.7 MB) + warehouse: `ca_axios_mortality_by_hospital` (554 hospitals) |
| 53 | **CA CHHS Open Data CKAN catalog** | `https://data.chhs.ca.gov/api/3/action/package_search?q=hospital%20discharge` | 37 hospital-related CHHS datasets | indexed |
| 54 | **CA HCAI Hospital Inpatient — Characteristics** | (CKAN resource) | Schema captured | `scrape/states/ca_hospital_inpatient_-_characteristics_by_patient_co.json` |
| 55 | **CA HCAI Number of Selected Inpatient Medical Procedures** | (CKAN resource) | Schema captured | `scrape/states/ca_number_of_selected_inpatient_medical_procedures_in.json` |
| 56 | **CA HCAI Fourth Quarter Summary Hospital Utilization** | (CKAN resource) | Schema captured | `scrape/states/ca_fourth_quarter_summary_hospital_utilization_discha.json` |

### 4c. Other states (indexed — pulls queued)

| # | Source | URL | Status |
|---|---|---|---|
| 57 | **TX DSHS PUDF download index** | `https://www.dshs.texas.gov/THCIC/Hospitals/Download.shtm` | 144 PUDF files cataloged; full access needs form | `scrape/states/tx_pudf_download_index.json` |
| 58 | **WA data.wa.gov catalog (CHARS)** | `https://data.wa.gov/api/views.json` | 50 hospital-discharge datasets indexed | `scrape/states/wa_chars_catalog.json` |
| 59 | **FL Health Finder** | `https://www.floridahealthfinder.gov/` | Hospital quality data; downloads require browsing | `scrape/states/fl_healthfinder_meta.json` |
| 60 | **PA PHC4 Hospital Performance** | `https://www.phc4.org/` | Hospital reports (PDF / web) | `scrape/states/pa_phc4_meta.json` |
| 61 | **MA CHIA Case Mix Database** | `https://www.chiamass.gov/hospital-cost-report-database/` | Hospital cost reports | `scrape/states/ma_chia_downloads.json` |
| 62 | **CO data.colorado.gov** | `https://data.colorado.gov/api/views.json` | Hospital discharge datasets | `scrape/states/co_hospital_datasets.json` |
| 63 | **NJ DOH Hospital Performance** | `https://www.nj.gov/health/healthcarequality/hospital-performance/` | NJ quality reports | `scrape/states/nj_dol_hospital_quality.json` |

### 4d. State medical-error reporting (publicly published)

| # | Source | URL | What it gives |
|---|---|---|---|
| 64 | **MA Serious Reportable Events** | `https://www.mass.gov/lists/serious-reportable-event-reports` | Hospital-attributed NQF "never events" | scraped via TinyFish |
| 65 | **PA Patient Safety Authority** | `http://patientsafety.pa.gov/` | PA hospital adverse-event reports | indexed |
| 66 | **NY DOH Patient Safety** | `https://www.health.ny.gov/` | NY hospital incident data | indexed |

---

## 5. TinyFish browser-automation runs (USA-only, stealth profile)

`POST https://agent.tinyfish.ai/v1/automation/run-async` with `browser_profile=stealth, proxy_config={country_code:US}`

| # | Target URL | Purpose | Result |
|---|---|---|---|
| 67 | `https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/.../warning-letters` | BSc FDA warning letters | `scrape/tinyfish/fda_bsc_warning_letters.json` |
| 68 | `https://news.bostonscientific.com/news` | BSc corporate news | 404 (page moved) |
| 69 | `https://projects.propublica.org/nursing-homes/` | ProPublica inspections for BSc mentions | `scrape/tinyfish/axios_propublica_inspections.json` |
| 70 | `https://www.courtlistener.com/` | BSc federal cases (TF backup) | `scrape/tinyfish/courtlistener_bsc.json` |
| 71 | `https://www.hospitalsafetygrade.org/` | Leapfrog letter grades top-10 AXIOS hospitals | `scrape/tinyfish/leapfrog_safety_grades.json` |
| 72 | `https://www.qualitycheck.org/` | Joint Commission accreditation status | (queued — TF credits exhausted) |
| 73 | `https://health.usnews.com/best-hospitals/rankings/gastroenterology-and-gi-surgery` | US News top GI hospitals | `scrape/tinyfish/usnews_top_gastro.json` |
| 74 | `https://hcai.ca.gov/data/healthcare-quality/` | CA HAI per-hospital data | `scrape/tinyfish/ca_hcai_hospital_safety.json` |
| 75 | `https://www.health.ny.gov/statistics/sparcs/` | NY SPARCS portal navigation | `scrape/tinyfish/ny_doh_hospital_data.json` |
| 76 | `https://www.bostonscientific.com/en-US/medical-specialties.html` | BSc complete US product portfolio | (queued) |
| 77 | `https://projects.propublica.org/docdollars/` | ProPublica Dollars for Docs BSc top recipients | (queued) |
| 78 | `https://datatools.ahrq.gov/hcupnet/` | AHRQ HCUPnet national procedure volumes | `scrape/tinyfish/ahrq_hcupnet_endoscopy_volumes.json` |

---

## 6. Hospital-side direct HTTP (USA, no auth)

| # | Source | URL | Output |
|---|---|---|---|
| 79 | Cox Medical Centers Quality | `https://www.coxhealth.com/about/measuring-our-quality/` | timed out |
| 80 | HonorHealth Quality Care | `https://www.honorhealth.com/about-us/quality-care` | scraped |
| 81 | Barnes-Jewish Quality | `https://www.barnesjewish.org/About-Us/Quality` | scraped |
| 82 | Northwestern Awards | `https://www.nm.org/about-us/awards-recognition` | scraped |
| 83 | Indiana University Health Quality | `https://iuhealth.org/about-iu-health/quality-and-safety` | timed out |
| 84 | Cedars-Sinai Quality | `https://www.cedars-sinai.org/about/quality.html` | scraped |
| 85 | Loma Linda Quality | `https://lluh.org/quality-and-safety` | scraped |
| 86 | Mayo Clinic AZ Quality | `https://www.mayoclinic.org/about-mayo-clinic/quality` | scraped |

All → `scrape/hospital_quality_pages_us.json`

---

## 7. Mass-tort / litigation tracking

| # | Source | URL | Output |
|---|---|---|---|
| 87 | Morgan & Morgan Medical Device | `https://www.forthepeople.com/medical-malpractice/` | scraped |
| 88 | Lieff Cabraser Medical Device | `https://www.lieffcabraser.com/medical-device/` | scraped |
| 89 | Levin Papantonio Practice Areas | `https://www.levinpapantonio.com/practice-areas/` | scraped |
| 90 | Simmons Hanly Conroy Medical Device | `https://www.simmonsfirm.com/medical-device/` | scraped |
| 91 | Public Justice | `https://www.publicjustice.net/our-work/` | scraped |

All → `scrape/law_firm_bsc_trackers_us.json`

---

## 8. MCP discovery tools used during research

| Service | Endpoint | Purpose | Status during work |
|---|---|---|---|
| **mcpbundles cms-medicare MCP** | `https://mcp.mcpbundles.com/bundle/cms-medicare` | Discover data.cms.gov dataset IDs (`xubh-q36u`, `632h-zaca`, `dgmq-aat3`, etc.) | Connected at session start; later disconnected |
| **TinyFish browser automation** | `https://agent.tinyfish.ai/v1/automation/run-async` | Browser-based scraping for CAPTCHA-protected pages | Used 15 jobs; credits exhausted |

---

## Counts summary

| Category | Sources |
|---|---:|
| FDA openFDA | 8 |
| CMS Provider Data Catalog | 11 |
| CMS Medicare PUFs | 5 |
| CMS other (ZIPs/DAC/HCRIS) | 14 |
| Other federal (USA-spending, ClinicalTrials, OIG, PubMed, NIH, PCORI, CourtListener, SEC, AHRQ, AGA) | 10 |
| State portals (NY, CA, TX, WA, FL, PA, MA, CO, NJ) | 15 |
| State medical-error reporting (MA, PA, NY) | 3 |
| TinyFish browser scrapes | 12 |
| Hospital direct HTTP | 8 |
| Mass-tort law firms | 5 |
| MCP discovery tools | 2 |
| **TOTAL** | **93** distinct external sources |

---

## How to read this

Each row above = **one external URL we hit** to either ingest into our ClickHouse
warehouse (productionised) or save as JSON in `scrape/` (raw, ready-to-ingest).

USA filter applied everywhere:
- API queries use `location=US` / `country=United States` filters where supported
- Author affiliations from PubMed scrubbed for non-US records
- State portals are inherently per-state
- Federal sources (FDA, CMS) are US-only by definition

---

## §9 Additional free sources fetched (expansion pass)

| # | Source | URL | Output |
|---|---|---|---|
| 92 | **CMS HCAHPS Patient Experience Survey** (per-hospital, all US) | `https://data.cms.gov/provider-data/sites/default/files/resources/...HCAHPS-Hospital.csv` | `scrape/expanded/hcahps_hospital.csv.gz` (2.1 MB) + `hcahps_by_hospital_summary.json` (4,792 hospitals) |
| 93 | **PubMed E-utils — BSc full device family** | NCBI E-utils | `scrape/expanded/pubmed_bsc_full_family_us.json` (PRECISION, OBTRYX, INNOVA, ELUVIA, WATCHMAN, TAXUS, SYNERGY, Promus, Emblem S-ICD, Hot AXIOS, Vici) |
| 94 | **PubMed — per top-10 academic hospital** | NCBI E-utils | `scrape/expanded/pubmed_bsc_per_hospital.json` (BSc publication counts per Mayo / Cleveland / Cedars / Mass General / Hopkins / UCLA / UCSF / Stanford / NYU / Mt Sinai) |
| 95 | **BSc corporate news releases** | `news.bostonscientific.com/news-releases` | `scrape/expanded/bsc_page_news-releases.html` |
| 96 | **ProPublica Surgeon Scorecard** | `projects.propublica.org/surgeons/` | `scrape/expanded/propublica_surgeon_scorecard.html` |
| 97 | **MedRxiv preprints** | `medrxiv.org/search/...` | `scrape/expanded/medrxiv_bsc_preprints.html` |
| 98 | **CDC NHSN hospital infection/admission data** | `data.cdc.gov/api/views.json` | `scrape/expanded/cdc_*.json` (3 hospital datasets indexed + 200-row samples) |
| 99 | **HRSA Data Warehouse downloads** | `data.hrsa.gov/data/download` | `scrape/expanded/hrsa_data_warehouse.json` (30 files) |
