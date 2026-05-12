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
