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
-- FDA MAUDE (adverse events + device details)
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fda_maude_events (
    report_number       String,
    event_type          Nullable(String),
    date_received       Nullable(Date),
    date_of_event       Nullable(Date),
    event_location      Nullable(String),
    report_source_code  Nullable(String),
    mdr_report_key      Nullable(String),
    manufacturer_name   Nullable(String),
    facility_name       Nullable(String),
    facility_state      Nullable(String),
    facility_zip        Nullable(String),
    patient_outcomes    Nullable(String),
    device_problems     Nullable(String),
    mdr_text            Nullable(String),
    raw                 String,
    fetched_at          DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY report_number;

-- Per-event device rows. Keyed by (report_number, seq) so re-ingest of the
-- same event with the same seq overwrites. If a re-ingest has fewer device
-- rows than before, the extras are cleared by the DELETE WHERE report_number
-- IN (...) mutation that ingest.py fires before inserting.
CREATE TABLE IF NOT EXISTS fda_maude_devices (
    report_number       String,
    seq                 Int32,
    product_code        Nullable(String),
    brand_name          Nullable(String),
    generic_name        Nullable(String),
    manufacturer        Nullable(String),
    model_number        Nullable(String),
    catalog_number      Nullable(String),
    lot_number          Nullable(String),
    udi_di              Nullable(String),
    udi_public          Nullable(String),
    device_age          Nullable(String),
    device_availability Nullable(String),
    raw                 String,
    fetched_at          DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY (report_number, seq);

CREATE TABLE IF NOT EXISTS fda_gudid_devices (
    primary_di          String,
    product_code        Nullable(String),
    product_codes_all   Nullable(String),
    brand_name          Nullable(String),
    company_name        Nullable(String),
    device_description  Nullable(String),
    gmdn_pt_name        Nullable(String),
    catalog_number      Nullable(String),
    version_model       Nullable(String),
    is_kit              Nullable(UInt8),
    is_combination      Nullable(UInt8),
    public_version_date Nullable(Date),
    raw                 String,
    fetched_at          DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY primary_di;

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

-- ---------------------------------------------------------------
-- Ingestion audit log — insert-once per run (finish_ingest_log)
-- ---------------------------------------------------------------

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
