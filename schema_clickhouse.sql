-- CMS Hospital Data — ClickHouse warehouse schema.
-- Strategy: ReplacingMergeTree(fetched_at) for upsert semantics. Reads that
-- need the latest version per key use SELECT ... FINAL. Dedup happens during
-- background merges; FINAL does it at query time.
-- Idempotent: every CREATE TABLE uses IF NOT EXISTS.

-- ---------------------------------------------------------------
-- CMS provider-data: hospitals + measures
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cms_hospitals (
    facility_id         String,
    facility_name       Nullable(String),
    address             Nullable(String),
    city                Nullable(String),
    state               Nullable(String),
    zip_code            Nullable(String),
    county_name         Nullable(String),
    telephone           Nullable(String),
    hospital_type       Nullable(String),
    hospital_ownership  Nullable(String),
    emergency_services  Nullable(String),
    birthing_friendly   Nullable(String),
    overall_rating      Nullable(String),
    raw                 String,
    fetched_at          DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY facility_id;

CREATE TABLE IF NOT EXISTS cms_hospital_measures (
    dataset_id            String,
    facility_id           String,
    measure_id            String,
    measure_name          Nullable(String),
    score                 Nullable(String),
    score_num             Nullable(Float64),
    lower_estimate        Nullable(String),
    lower_estimate_num    Nullable(Float64),
    higher_estimate       Nullable(String),
    higher_estimate_num   Nullable(Float64),
    compared_to_national  Nullable(String),
    denominator           Nullable(String),
    denominator_num       Nullable(Float64),
    start_date            Nullable(String),
    start_date_parsed     Nullable(Date),
    end_date              Nullable(String),
    end_date_parsed       Nullable(Date),
    footnote              Nullable(String),
    raw                   String,
    fetched_at            DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (dataset_id, facility_id, measure_id);

CREATE TABLE IF NOT EXISTS cms_state_measures (
    dataset_id            String,
    state                 String,
    measure_id            String,
    measure_name          Nullable(String),
    score                 Nullable(String),
    compared_to_national  Nullable(String),
    raw                   String,
    fetched_at            DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (dataset_id, state, measure_id);

CREATE TABLE IF NOT EXISTS cms_national_measures (
    dataset_id            String,
    measure_id            String,
    measure_name          Nullable(String),
    score                 Nullable(String),
    raw                   String,
    fetched_at            DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (dataset_id, measure_id);

CREATE TABLE IF NOT EXISTS cms_footnote_crosswalk (
    footnote        String,
    footnote_text   Nullable(String),
    raw             String,
    fetched_at      DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY footnote;

CREATE TABLE IF NOT EXISTS cms_wide_facility_snapshots (
    dataset_id     String,
    facility_id    String,
    npi            Nullable(String),
    facility_name  Nullable(String),
    state          Nullable(String),
    zip_code       Nullable(String),
    year           String,
    raw            String,
    fetched_at     DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (dataset_id, facility_id, year);

-- ---------------------------------------------------------------
-- FDA MAUDE + GUDID — REMOVED. The three tables (fda_maude_events,
-- fda_maude_devices, fda_gudid_devices) were dropped because the
-- project no longer uses openFDA data. Their DDL is kept here as
-- comments for reference / quick restore.
--
-- To re-enable: uncomment these CREATE TABLE blocks, uncomment the
-- two entries in src/datasets.py, and run `python run.py init`.
-- ---------------------------------------------------------------

-- ---------------------------------------------------------------
-- CMS data-api: Medicare utilization, DMEPOS, Open Payments
-- ---------------------------------------------------------------

-- Append-only — no dedup needed. Deduping this table for an 80M-row dataset
-- would wreck query performance, and upstream rows are unique by construction.
CREATE TABLE IF NOT EXISTS cms_provider_summary (
    dataset_id           String,
    year                 Nullable(Int32),
    ccn                  Nullable(String),
    npi                  Nullable(String),
    referring_npi        Nullable(String),
    hcpcs_code           Nullable(String),
    drg_code             Nullable(String),
    drg_description      Nullable(String),
    provider_state       Nullable(String),
    provider_city        Nullable(String),
    provider_name        Nullable(String),
    total_services       Nullable(Float64),
    total_beneficiaries  Nullable(Float64),
    total_payment_amt    Nullable(Float64),
    raw                  String,
    fetched_at           DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (dataset_id, ifNull(provider_state, ''), ifNull(ccn, ''));

CREATE TABLE IF NOT EXISTS cms_open_payments (
    dataset_id              String,
    year                    Nullable(Int32),
    record_id               Nullable(String),
    physician_npi           Nullable(String),
    physician_name          Nullable(String),
    physician_specialty     Nullable(String),
    teaching_hospital_ccn   Nullable(String),
    teaching_hospital_name  Nullable(String),
    manufacturer_name       Nullable(String),
    product_name            Nullable(String),
    product_category        Nullable(String),
    nature_of_payment       Nullable(String),
    payment_total           Nullable(Float64),
    payment_date            Nullable(Date),
    raw                     String,
    fetched_at              DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (dataset_id, ifNull(manufacturer_name, ''));

-- ---------------------------------------------------------------
-- HCPCS master + manual bridges
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS hcpcs_master (
    hcpcs_code        String,
    short_desc        Nullable(String),
    long_desc         Nullable(String),
    betos_code        Nullable(String),
    pricing_indicator Nullable(String),
    coverage_code     Nullable(String),
    asc_payment_grp   Nullable(String),
    mog_payment_grp   Nullable(String),
    type_of_service   Nullable(String),
    action_code       Nullable(String),
    code_family       Nullable(String),
    is_device         Nullable(UInt8),
    effective_qtr     Nullable(String),
    raw_line          Nullable(String),
    fetched_at        DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY hcpcs_code;

CREATE TABLE IF NOT EXISTS bridge_product_code_to_measure (
    product_code     String,
    cms_measure_id   String,
    device_category  Nullable(String),
    confidence       Nullable(String),
    fetched_at       DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (product_code, cms_measure_id);

CREATE TABLE IF NOT EXISTS bridge_hcpcs_to_product_code (
    hcpcs_code       String,
    product_code     String,
    device_category  Nullable(String),
    match_method     String,
    confidence       String,
    source_notes     Nullable(String),
    fetched_at       DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (hcpcs_code, product_code);

-- MAUDE-event → CMS-CCN bridge.
-- MAUDE doesn't carry CCN; only `facility_name`, `facility_state`, and
-- (sometimes) `facility_zip`. This table holds the inferred CCN per report
-- using a multi-pass match: exact normalized name + state, then state +
-- ngram similarity. Built by scripts/build_maude_ccn_bridge.py — re-runnable
-- and idempotent (ReplacingMergeTree on report_number).
--
-- Joining this to fda_maude_devices gives the all-in-one row the
-- requirement asks for: manufacturer + device + CCN.
CREATE TABLE IF NOT EXISTS bridge_maude_to_ccn (
    report_number       String,
    facility_id         String,           -- CCN
    match_method        String,           -- 'exact_name_state' | 'ngram_state'
    confidence          String,           -- 'high' | 'medium' | 'low'
    facility_name_maude Nullable(String),
    facility_name_ccn   Nullable(String),
    state               Nullable(String),
    score               Nullable(Float64),
    fetched_at          DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY report_number;

-- vw_device_event_with_ccn was the all-in-one (MAUDE event × device × CCN)
-- view. Dropped together with the FDA tables. The DDL is kept commented
-- for reference; restore alongside the fda_maude_* CREATE TABLE blocks
-- above if the project re-adds openFDA.

-- ---------------------------------------------------------------
-- Device → parent-procedure crosswalk.
-- C-codes (and similar packaged HCPCS) are physical-device codes that
-- are bundled into another code's payment under OPPS (SI=N). They never
-- show up in cms_provider_summary because nobody bills them. This table
-- maps each device code to the CPT/HCPCS procedure that DOES get billed
-- when that device is used — e.g. C1778 neurostim lead → CPT 63685
-- neurostim generator insertion. Many-to-many.
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS device_to_procedure (
    device_code            String,           -- HCPCS device code (usually C-prefix, SI=N)
    procedure_code         String,           -- The CPT/HCPCS code that bills the procedure
    description            String,           -- What the procedure does
    device_category        Nullable(String), -- AICD | pacemaker | DBS | SCS | DES | LAA closure | LAMS | etc.
    fda_class              Nullable(String), -- 'II' or 'III' (FDA risk classification)
    manufacturer_product   Nullable(String), -- e.g. 'BSc Watchman', 'BSc Promus / Synergy', 'BSc FARAPULSE'
    notes                  Nullable(String),
    fetched_at             DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (device_code, procedure_code);

-- ---------------------------------------------------------------
-- Ingestion audit log — insert-once per run (finish_ingest_log)
-- ---------------------------------------------------------------

-- ---------------------------------------------------------------
-- State-mandated adverse-event registries (hospital-named events).
-- Currently MA Department of Public Health "Serious Reportable Events"
-- (NQF SRE list) — the only public US source we've verified that carries
-- both a hospital identity and device-relevant event categories. MAUDE
-- doesn't carry a hospital identifier; this table is the bridge.
--
-- Ingest source: mass.gov XLSX, one workbook per (year × facility_type).
-- Variants: acute / non_acute / asc. CCN match only resolves for
-- acute-care hospitals (the others aren't in cms_hospitals).
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS state_adverse_events (
    state                String,           -- 'MA' for now; designed to accept MN/NJ later
    report_year          Int32,            -- calendar year covered by the report
    facility_type        String,           -- 'acute' | 'non_acute' | 'asc'
    hospital_name        String,           -- raw name as published by the state
    state_license_id     Nullable(String), -- e.g. MA's 4-digit licensee number from "(2168)"
    ccn_match            Nullable(String), -- CMS CCN if name+state match resolved
    ccn_match_confidence Nullable(String), -- 'exact' | 'fuzzy' | 'none'
    event_category       String,           -- group header, e.g. 'Product or Device Events'
    event_type           String,           -- column header, e.g. 'Device misuse or malfunction'
    is_device_related    UInt8,            -- 1 for device/device-adjacent SREs, 0 otherwise
    event_count          Int32,            -- number of events reported by this facility
    source_url           String,           -- direct URL of the XLSX we ingested
    source_doc_label     Nullable(String), -- human label, e.g. 'CY 2022 acute-care'
    fetched_at           DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (state, report_year, facility_type, hospital_name, event_type);

-- ---------------------------------------------------------------
-- OPPS Addendum B — HCPCS → APC crosswalk + payment rates.
-- Source: cms.gov/files/zip/{month}-{year}-opps-addendum-b.zip
-- Published quarterly (Jan/Apr/Jul/Oct). Joining this with
-- medicare_outpatient_by_provider_service (which is APC-keyed) lets you
-- apportion APC volumes back to individual HCPCS codes.
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS opps_addendum_b (
    hcpcs_code               String,
    effective_quarter        String,        -- e.g. '2026Q1'
    short_descriptor         Nullable(String),
    status_indicator         Nullable(String),  -- 'S','T','J1','J2','Q1', etc.
    apc_code                 Nullable(String),
    relative_weight          Nullable(Float64),
    payment_rate             Nullable(Float64),
    national_copayment       Nullable(Float64),
    minimum_copayment        Nullable(Float64),
    pass_through_expiry_year Nullable(String),
    raw                      String,
    fetched_at               DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (hcpcs_code, effective_quarter);

CREATE TABLE IF NOT EXISTS cms_ingestion_log (
    run_id           String,                  -- uuid generated in Python
    dataset_id       String,
    dataset_name     Nullable(String),
    started_at       DateTime64(3),
    finished_at      Nullable(DateTime64(3)),
    rows_fetched     Int64 DEFAULT 0,
    rows_upserted    Int64 DEFAULT 0,
    status           String,
    error            Nullable(String)
)
ENGINE = MergeTree
ORDER BY (dataset_id, started_at);

-- ---------------------------------------------------------------
-- Stark Law DHS (Designated Health Services) CPT/HCPCS list.
-- Source: cms.gov/medicare/regulations-guidance/physician-self-referral
--         /list-cpt-hcpcs-codes (AMA-licensed ZIP, one per effective year).
-- One row per (code × dhs_category × effective_year). A code can belong to
-- more than one DHS category (e.g. CT codes appear in both "Radiology" and
-- "Inpatient/Outpatient Hospital Services"), so the natural key is composite.
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stark_dhs_codes (
    hcpcs_code        String,
    dhs_category      String,          -- e.g. 'CLINICAL LABORATORY SERVICES'
    effective_year    Int32,
    short_description Nullable(String),
    source_doc        String,          -- file name inside the ZIP
    source_url        Nullable(String),
    raw_line          Nullable(String),
    fetched_at        DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (hcpcs_code, dhs_category, effective_year);

-- ---------------------------------------------------------------
-- ClinicalTrials.gov v2 — study + intervention rows.
-- Source: clinicaltrials.gov/api/v2/studies (JSON v2, paginated by pageToken).
-- The base tables are managed by the older risk_db ingester (which writes
-- into ClickHouse too); we add `hcpcs_code` / `hcpcs_confidence` columns so
-- the Groq trial→HCPCS mapper can backfill mappings without a second table.
-- The ALTER ... IF NOT EXISTS guards make this idempotent.
-- ---------------------------------------------------------------

ALTER TABLE clinical_trial_interventions
    ADD COLUMN IF NOT EXISTS hcpcs_code Nullable(String),
    ADD COLUMN IF NOT EXISTS hcpcs_confidence Nullable(String);

-- ---------------------------------------------------------------
-- NEW SOURCES (NEW_SOURCES.md) — 12 tables
-- ---------------------------------------------------------------

-- P0-1: OIG LEIE — providers/entities excluded from federal healthcare programs
-- Source: oig.hhs.gov/exclusions/downloadables/UPDATED.csv
CREATE TABLE IF NOT EXISTS oig_leie_exclusions (
    leie_id           UInt64,                 -- hash(lastname,firstname,busname,npi,excldate)
    last_name         Nullable(String),
    first_name        Nullable(String),
    middle_name       Nullable(String),
    business_name     Nullable(String),
    general           Nullable(String),       -- general info / occupation
    specialty         Nullable(String),
    upin              Nullable(String),
    npi               Nullable(String),
    dob               Nullable(String),       -- ISO 'YYYY-MM-DD' string (parse at query time)
    address           Nullable(String),
    city              Nullable(String),
    state             Nullable(String),
    zip               Nullable(String),
    exclusion_type    Nullable(String),       -- statute citation
    exclusion_date    Nullable(String),       -- ISO 'YYYY-MM-DD'
    reinstate_date    Nullable(String),       -- ISO 'YYYY-MM-DD'
    waiver_date       Nullable(String),       -- ISO 'YYYY-MM-DD'
    waiver_state      Nullable(String),
    source_url        Nullable(String),
    raw               String,
    fetched_at        DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY leie_id;

-- P0-2: Medicare Physician Fee Schedule — per-CPT/HCPCS payment rates
-- Source: cms.gov/medicare/payment/fee-schedules/physician/pfs-relative-value-files (quarterly ZIP)
CREATE TABLE IF NOT EXISTS mpfs_rates (
    hcpcs_code               String,
    modifier                 String,                 -- '' if none
    effective_year           UInt16,
    effective_quarter        UInt8,                  -- 1..4
    short_descriptor         Nullable(String),
    status_code              Nullable(String),       -- A/R/N/etc.
    pe_facility_rvu          Nullable(Float64),      -- practice expense (facility)
    pe_non_facility_rvu      Nullable(Float64),      -- practice expense (non-facility / office)
    work_rvu                 Nullable(Float64),
    malpractice_rvu          Nullable(Float64),
    total_facility_rvu       Nullable(Float64),
    total_non_facility_rvu   Nullable(Float64),
    facility_payment         Nullable(Float64),      -- $ at office setting (non-facility)
    non_facility_payment     Nullable(Float64),
    conversion_factor        Nullable(Float64),
    global_days              Nullable(String),
    bilateral_indicator      Nullable(String),
    raw                      String,
    fetched_at               DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (hcpcs_code, modifier, effective_year, effective_quarter);

-- P0-3: Opt-Out Affidavits — providers who left Medicare
-- Source: data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/opt-out-affidavits
CREATE TABLE IF NOT EXISTS medicare_opt_out (
    npi                  String,
    optout_effective     String DEFAULT '',     -- 'YYYY-MM-DD' or '' (sort-key tiebreaker)
    first_name           Nullable(String),
    last_name            Nullable(String),
    specialty            Nullable(String),
    optout_effective_date Nullable(String),    -- ISO 'YYYY-MM-DD'
    optout_end_date      Nullable(String),     -- ISO 'YYYY-MM-DD'
    last_updated         Nullable(String),     -- ISO 'YYYY-MM-DD'
    first_name_alias     Nullable(String),
    address_line1        Nullable(String),
    address_line2        Nullable(String),
    city                 Nullable(String),
    state               Nullable(String),
    zip                  Nullable(String),
    source_url           Nullable(String),
    raw                  String,
    fetched_at           DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (npi, optout_effective);

-- P0-4: POS file — all Medicare-certified facilities (hospitals, ASCs, SNFs, dialysis, etc.)
-- Source: cms.gov/data-research/statistics-trends-and-reports/provider-services-current-files
CREATE TABLE IF NOT EXISTS pos_facilities (
    ccn                  String,                -- provider/CCN
    facility_name        Nullable(String),
    facility_type        Nullable(String),      -- decoded from PRVDR_CTGRY_CD
    provider_category_cd Nullable(String),      -- raw code
    provider_subcategory_cd Nullable(String),
    address              Nullable(String),
    city                 Nullable(String),
    state                Nullable(String),
    zip_code             Nullable(String),
    county_name          Nullable(String),
    phone                Nullable(String),
    bed_count            Nullable(Int32),
    certification_date   Nullable(Date),
    termination_date     Nullable(Date),
    termination_code     Nullable(String),
    medicaid_only        Nullable(UInt8),
    chain_owner          Nullable(String),
    fiscal_year_end      Nullable(String),
    cbsa_code            Nullable(String),
    snapshot_quarter     String,                 -- e.g. '2025Q1'
    source_url           Nullable(String),
    raw                  String,
    fetched_at           DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (ccn, snapshot_quarter);

-- P1-5: Order & Referring NPI — NPIs eligible to order/refer Medicare items
-- Source: data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/order-and-referring
CREATE TABLE IF NOT EXISTS order_referring_npi (
    npi               String,
    last_name         Nullable(String),
    first_name        Nullable(String),
    -- Eligibility flags for each service type (Y/N in source)
    partb_eligible    Nullable(UInt8),
    dme_eligible      Nullable(UInt8),
    hha_eligible      Nullable(UInt8),
    pmd_eligible      Nullable(UInt8),   -- Power Mobility Devices
    hospice_eligible  Nullable(UInt8),
    source_url        Nullable(String),
    raw               String,
    fetched_at        DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY npi;

-- P1-6: Provider/Supplier Taxonomy Crosswalk — NUCC → CMS specialty
-- Source: data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/medicare-provider-and-supplier-taxonomy-crosswalk
CREATE TABLE IF NOT EXISTS npi_taxonomy_crosswalk (
    medicare_specialty_code   String,
    medicare_provider_supplier_type Nullable(String),
    provider_taxonomy_code    String,           -- NUCC taxonomy
    provider_taxonomy_description Nullable(String),
    source_url                Nullable(String),
    raw                       String,
    fetched_at                DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (medicare_specialty_code, provider_taxonomy_code);

-- P1-7: Medicare FFS Public Provider Enrollment
-- Source: data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/medicare-fee-for-service-public-provider-enrollment
CREATE TABLE IF NOT EXISTS medicare_ffs_enrollment (
    npi               String,
    pecos_asct_cntl_id Nullable(String),
    enrollment_id     Nullable(String),
    provider_type_cd  String DEFAULT '',     -- non-nullable for sort key
    provider_type_desc Nullable(String),
    state_cd          Nullable(String),
    first_name        Nullable(String),
    last_name         Nullable(String),
    org_name          Nullable(String),
    gndr_sw           Nullable(String),
    source_url        Nullable(String),
    raw               String,
    fetched_at        DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (npi, provider_type_cd);

-- P2-8: HCRIS — Hospital cost reports (Forms 2552-10)
-- Source: cms.gov/data-research/statistics-trends-and-reports/cost-reports/hospital-2010-form
CREATE TABLE IF NOT EXISTS hcris_hospital_cost_reports (
    rpt_rec_num       UInt64,           -- HCRIS report record number (natural key)
    ccn               String,
    fy_bgn_dt         Nullable(Date),
    fy_end_dt         Nullable(Date),
    proc_dt           Nullable(Date),
    initl_rpt_sw      Nullable(String),
    last_rpt_sw       Nullable(String),
    trnsmtl_num       Nullable(String),
    fi_num            Nullable(String),
    adr_vndr_cd       Nullable(String),
    fy_year           Nullable(UInt16),
    -- summary fields parsed from numeric table (S-3, S-10, A, G)
    total_beds        Nullable(Int32),
    total_discharges  Nullable(Int64),
    medicare_discharges Nullable(Int64),
    medicaid_discharges Nullable(Int64),
    total_charges     Nullable(Float64),
    total_costs       Nullable(Float64),
    medical_supplies_cost Nullable(Float64),    -- Wkst A line for med supplies (device proxy)
    capital_expenditure Nullable(Float64),
    uncompensated_care_cost Nullable(Float64),  -- Wkst S-10
    source_url        Nullable(String),
    raw               String,
    fetched_at        DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (rpt_rec_num);

-- P2-9: Medicare Revalidation List
-- Source: data.cms.gov/tools/medicare-revalidation-list
CREATE TABLE IF NOT EXISTS medicare_revalidation (
    enrollment_id        String,
    npi                  Nullable(String),
    first_name           Nullable(String),
    last_name            Nullable(String),
    org_name             Nullable(String),
    revalidation_due_date Nullable(Date),
    revalidation_status  Nullable(String),
    adjusted_revalidation_due_date Nullable(Date),
    source_url           Nullable(String),
    raw                  String,
    fetched_at           DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY enrollment_id;

-- P2-10: MA & Part D monthly enrollment by state
-- Source: cms.gov/.../medicare-advantagepart-d-contract-and-enrollment-data/monthly-enrollment-state
CREATE TABLE IF NOT EXISTS medicare_ma_partd_enrollment (
    snapshot_month       String,             -- 'YYYY-MM'
    contract_id          String,
    plan_id              String,
    state                Nullable(String),
    county               Nullable(String),
    fips_state_county    String DEFAULT '',     -- non-nullable for sort key
    enrollment           Nullable(Int64),
    source_url           Nullable(String),
    raw                  String,
    fetched_at           DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (snapshot_month, contract_id, plan_id, fips_state_county);

-- P2-11: MA Star Ratings (Part C and D performance)
-- Source: cms.gov/medicare/health-drug-plans/part-c-d-performance-data
CREATE TABLE IF NOT EXISTS medicare_star_ratings (
    rating_year      UInt16,
    contract_id      String,
    measure_id       String,
    measure_name     Nullable(String),
    star_rating      Nullable(Float32),
    raw_score        Nullable(String),
    source_url       Nullable(String),
    raw              String,
    fetched_at       DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (rating_year, contract_id, measure_id);

-- P2-12: BETOS crosswalk — HCPCS → clinical category
-- Source: data.cms.gov/.../betos-classification-system
CREATE TABLE IF NOT EXISTS betos_crosswalk (
    hcpcs_code         String,
    betos_code         Nullable(String),
    betos_description  Nullable(String),
    rbcs_category      Nullable(String),       -- restructured BETOS classification system category
    rbcs_subcategory   Nullable(String),
    rbcs_family        Nullable(String),
    source_url         Nullable(String),
    raw                String,
    fetched_at         DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY hcpcs_code;

-- ---------------------------------------------------------------
-- Hospital-Acquired Conditions (HAC) — CMS Deficit Reduction Act
-- Source: data.cms.gov 'Deficit Reduction Act Hospital-Acquired Condition Measures'
-- Per-hospital rates for: Foreign Object Retained After Surgery, Air Embolism,
-- Blood Incompatibility, Falls and Trauma. The "Foreign Object" measure is the
-- CMS-level proxy for device-left-behind events.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hospital_hac_measures (
    ccn              String,
    measure_name     String,
    rate             Nullable(Float64),
    footnote         Nullable(String),
    start_quarter    Nullable(String),
    end_quarter      Nullable(String),
    source_url       Nullable(String),
    raw              String,
    fetched_at       DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (ccn, measure_name);

-- ---------------------------------------------------------------
-- Hospital Quality Summary — per-CCN count of measures "Better/Worse than national"
-- Source: data.cms.gov 'Hospital General Information' (CMS Provider Data Catalog)
-- The "Count of X Measures Worse" columns are the CMS-level proxy for
-- "how many quality issues did this hospital have vs peers" — closest to
-- a complaint-volume signal without scraping QCOR per hospital.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hospital_quality_summary (
    ccn                              String,
    hospital_overall_rating          Nullable(Int8),
    mort_total                       Nullable(Int8),
    mort_facility_count              Nullable(Int8),
    mort_better                      Nullable(Int8),
    mort_no_different                Nullable(Int8),
    mort_worse                       Nullable(Int8),
    safety_total                     Nullable(Int8),
    safety_facility_count            Nullable(Int8),
    safety_better                    Nullable(Int8),
    safety_no_different              Nullable(Int8),
    safety_worse                     Nullable(Int8),     -- device-infection / complication signal
    readm_total                      Nullable(Int8),
    readm_facility_count             Nullable(Int8),
    readm_better                     Nullable(Int8),
    readm_no_different               Nullable(Int8),
    readm_worse                      Nullable(Int8),
    pt_exp_total                     Nullable(Int8),
    pt_exp_facility_count            Nullable(Int8),
    te_total                         Nullable(Int8),
    te_facility_count                Nullable(Int8),
    source_url                       Nullable(String),
    raw                              String,
    fetched_at                       DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY ccn;

-- ---------------------------------------------------------------
-- QCOR Hospital Deficiencies (Form 2567 citations) — TinyFish-scraped per-CCN
-- Source: qcor.cms.gov per-hospital inspection reports.
-- Only ingested for BSc-relevant hospital subset (top 20-50 stent-volume CCNs).
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hospital_qcor_deficiencies (
    deficiency_id      UInt64,             -- hash(ccn, survey_date, tag)
    ccn                String,
    survey_date        Nullable(String),   -- ISO date string
    tag_number         Nullable(String),   -- 'A-0123' etc.
    tag_description    Nullable(String),   -- short tag label
    deficiency_text    Nullable(String),   -- the citation narrative
    severity_scope     Nullable(String),   -- e.g. 'D', 'F', 'L', 'Immediate Jeopardy'
    correction_date    Nullable(String),
    source_url         Nullable(String),
    scraped_at         DateTime64(3) DEFAULT now64(3),
    raw                String
)
ENGINE = ReplacingMergeTree(scraped_at)
ORDER BY deficiency_id;

-- ---------------------------------------------------------------
-- Nursing Home Health Deficiencies — actual Form 2567-style citation text
-- Source: data.cms.gov/provider-data 'Health Deficiencies' (apr 2026 release)
-- Per-survey-date deficiency citations with severity codes, tag numbers,
-- and the actual deficiency narrative. 418K rows covering ~15K SNFs.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS snf_health_deficiencies (
    deficiency_id        UInt64,           -- hash(ccn, survey_date, tag, location)
    ccn                  String,
    provider_name        Nullable(String),
    state                Nullable(String),
    survey_date          Nullable(String),  -- ISO 'YYYY-MM-DD'
    survey_type          Nullable(String),
    deficiency_prefix    Nullable(String),
    deficiency_category  Nullable(String),
    deficiency_tag       Nullable(String),
    deficiency_text      Nullable(String),  -- actual citation narrative
    scope_severity       Nullable(String),  -- 'D'..'L', 'J/K/L' = Immediate Jeopardy
    correction_date      Nullable(String),
    inspection_cycle     Nullable(UInt8),
    is_standard          Nullable(UInt8),
    is_complaint         Nullable(UInt8),
    is_infection_control Nullable(UInt8),
    source_url           Nullable(String),
    raw                  String,
    fetched_at           DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY deficiency_id;

-- Citation Code Look-up table — F-Tag / E-Tag descriptions
CREATE TABLE IF NOT EXISTS snf_citation_codes (
    tag_prefix          String,
    tag_number          String,
    tag_combined        String,            -- 'F-0656'
    tag_description     Nullable(String),
    tag_category        Nullable(String),
    source_url          Nullable(String),
    fetched_at          DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (tag_prefix, tag_number);

-- ---------------------------------------------------------------
-- Hospital Value-Based Purchasing (HVBP) — Safety domain
-- Source: data.cms.gov Provider Data Catalog dataset 'dgmq-aat3' (FY2026).
-- Per-CCN performance on 6 HAI measures + SEP-1 sepsis + combined SSI.
-- Each measure has: achievement_threshold, benchmark, baseline_rate,
-- performance_rate, achievement_points, improvement_points, measure_score.
-- These scores translate to actual Medicare payment adjustments —
-- a hospital with low scores is being financially penalized for unsafe care.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hospital_vbp_safety (
    ccn                              String,
    fiscal_year                      String DEFAULT '',
    facility_name                    Nullable(String),
    address                          Nullable(String),
    city                             Nullable(String),
    state                            Nullable(String),
    zip_code                         Nullable(String),
    county                           Nullable(String),
    -- HAI-1 CLABSI
    hai1_achievement_threshold       Nullable(Float64),
    hai1_benchmark                   Nullable(Float64),
    hai1_baseline_rate               Nullable(Float64),
    hai1_performance_rate            Nullable(Float64),
    hai1_achievement_points          Nullable(String),
    hai1_improvement_points          Nullable(String),
    hai1_measure_score               Nullable(String),
    -- HAI-2 CAUTI
    hai2_achievement_threshold       Nullable(Float64),
    hai2_benchmark                   Nullable(Float64),
    hai2_baseline_rate               Nullable(Float64),
    hai2_performance_rate            Nullable(Float64),
    hai2_achievement_points          Nullable(String),
    hai2_improvement_points          Nullable(String),
    hai2_measure_score               Nullable(String),
    combined_ssi_measure_score       Nullable(String),
    -- HAI-3 SSI Colon
    hai3_achievement_threshold       Nullable(Float64),
    hai3_benchmark                   Nullable(Float64),
    hai3_baseline_rate               Nullable(Float64),
    hai3_performance_rate            Nullable(Float64),
    hai3_achievement_points          Nullable(String),
    hai3_improvement_points          Nullable(String),
    hai3_measure_score               Nullable(String),
    -- HAI-4 SSI Abdominal Hysterectomy
    hai4_achievement_threshold       Nullable(Float64),
    hai4_benchmark                   Nullable(Float64),
    hai4_baseline_rate               Nullable(Float64),
    hai4_performance_rate            Nullable(Float64),
    hai4_achievement_points          Nullable(String),
    hai4_improvement_points          Nullable(String),
    hai4_measure_score               Nullable(String),
    -- HAI-5 MRSA
    hai5_achievement_threshold       Nullable(Float64),
    hai5_benchmark                   Nullable(Float64),
    hai5_baseline_rate               Nullable(Float64),
    hai5_performance_rate            Nullable(Float64),
    hai5_achievement_points          Nullable(String),
    hai5_improvement_points          Nullable(String),
    hai5_measure_score               Nullable(String),
    -- HAI-6 C. diff
    hai6_achievement_threshold       Nullable(Float64),
    hai6_benchmark                   Nullable(Float64),
    hai6_baseline_rate               Nullable(Float64),
    hai6_performance_rate            Nullable(Float64),
    hai6_achievement_points          Nullable(String),
    hai6_improvement_points          Nullable(String),
    hai6_measure_score               Nullable(String),
    -- SEP-1 Sepsis bundle compliance
    sep1_achievement_threshold       Nullable(Float64),
    sep1_benchmark                   Nullable(Float64),
    sep1_baseline_rate               Nullable(Float64),
    sep1_performance_rate            Nullable(Float64),
    sep1_achievement_points          Nullable(String),
    sep1_improvement_points          Nullable(String),
    sep1_measure_score               Nullable(String),
    source_url                       Nullable(String),
    raw                              String,
    fetched_at                       DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (ccn, fiscal_year);

-- ---------------------------------------------------------------
-- BSc AXIOS Stent — Probabilistic Hospital Attribution
-- ---------------------------------------------------------------
-- Method: P(hospital | MAUDE event) = hospital's share of national
-- AXIOS-procedure billing volume (HCPCS 43274/43275/43266/43253/43240).
-- Estimated events = P × 1,071 (actual BSc AXIOS MAUDE events as of session).
-- Estimated deaths = P × 50 (actual deaths in those 1,071 events).
-- Joined safety signals: SEP-1 sepsis bundle, CLABSI, HF readmit.
-- Source MAUDE: default.flattened_adverse_event filtered to AXIOS brand.
-- 827 hospitals; sanity-check sum(est_total_events) ≈ 1,071.
CREATE TABLE IF NOT EXISTS bsc_axios_attribution (
    ccn                            String,
    facility_name                  Nullable(String),
    state                          Nullable(String),
    city                           Nullable(String),
    axios_proc_services            Float64,
    physician_count                UInt32,
    pct_national_volume            Float64,
    est_total_events               Float64,
    est_deaths                     Float64,
    est_injuries                   Float64,
    est_malfunctions               Float64,
    sep1_measure_score             Nullable(String),
    hai1_measure_score             Nullable(String),
    sep1_performance_rate          Nullable(Float64),
    readm_hf_score                 Nullable(Float64),
    method                         String DEFAULT 'volume_weighted_v1',
    source_axios_events            UInt32 DEFAULT 1071,
    hcpcs_used                     String DEFAULT '43274,43275,43266,43253,43240',
    notes                          Nullable(String),
    fetched_at                     DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY ccn;

-- ---------------------------------------------------------------
-- BSc Non-Coronary Stent — Combined Attribution
-- ---------------------------------------------------------------
-- Same method as AXIOS, broader: covers all 5 C-codes (C1874/C1875/
-- C1876/C2617/C2625) by mapping each to its parent CPT, then summing
-- national procedure volume across CPTs 37236/37238/43240/43253/49327.
-- Source MAUDE total: 8,201 BSc non-coronary stent events (277 deaths)
-- matching WALLFLEX / WALLSTENT / POLARIS / PERCUFLEX / ADVANIX /
-- CONTOUR / AGILE / EXPRESS BIL brands.
-- 273 hospitals.
CREATE TABLE IF NOT EXISTS bsc_noncoronary_stent_attribution (
    ccn                            String,
    facility_name                  Nullable(String),
    state                          Nullable(String),
    city                           Nullable(String),
    device_codes                   String DEFAULT 'C1874,C1875,C1876,C2617,C2625',
    parent_cpts                    String DEFAULT '37236,37238,43240,43253,49327',
    proc_services                  Float64,
    physician_count                UInt32,
    pct_national_volume            Float64,
    est_total_events               Float64,
    est_deaths                     Float64,
    est_injuries                   Float64,
    est_malfunctions               Float64,
    sep1_measure_score             Nullable(String),
    hai1_measure_score             Nullable(String),
    sep1_performance_rate          Nullable(Float64),
    readm_hf_score                 Nullable(Float64),
    readm_ami_score                Nullable(Float64),
    method                         String DEFAULT 'volume_weighted_v1',
    source_maude_events            UInt32 DEFAULT 8201,
    source_maude_deaths            UInt32 DEFAULT 277,
    brand_pattern_used             String DEFAULT 'WALLFLEX,WALLSTENT,POLARIS,PERCUFLEX,ADVANIX,CONTOUR,AGILE,EXPRESS BIL',
    fetched_at                     DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY ccn;

-- ---------------------------------------------------------------
-- BSc Device-Code-Level Attribution (one row per device_code × CCN)
-- ---------------------------------------------------------------
-- Per-device-code probabilistic attribution. Each C-code maps to
-- its specific BSc product and parent CPT(s):
--   C1874 → Eluvia DES (CPTs 37236/37238) — 475 events, 4 deaths
--   C1876 → Innova (CPT 37236) — 756 events, 7 deaths
--   C2617 → Vici Venous Stent (CPTs 37236/37238) — 1 event
--   C2625 → AXIOS/Hot AXIOS (CPTs 43240/43253/49327) — 1019 events, 49 deaths
-- 621 rows total. Same volume-weighted Bayesian prior as the others.
CREATE TABLE IF NOT EXISTS bsc_device_code_attribution (
    device_code              String,
    bsc_product              String,
    parent_cpts              String,
    ccn                      String,
    facility_name            Nullable(String),
    state                    Nullable(String),
    city                     Nullable(String),
    proc_services            Float64,
    physician_count          UInt32,
    pct_national_volume      Float64,
    est_total_events         Float64,
    est_deaths               Float64,
    est_injuries             Float64,
    est_malfunctions         Float64,
    sep1_measure_score       Nullable(String),
    hai1_measure_score       Nullable(String),
    readm_hf_score           Nullable(Float64),
    readm_ami_score          Nullable(Float64),
    maude_events             UInt32,
    maude_deaths             UInt32,
    fetched_at               DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (device_code, ccn);
