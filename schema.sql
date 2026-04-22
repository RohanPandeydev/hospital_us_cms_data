-- CMS Hospital Data — local warehouse schema
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS cms_hospitals (
    facility_id           TEXT PRIMARY KEY,
    facility_name         TEXT,
    address               TEXT,
    city                  TEXT,
    state                 TEXT,
    zip_code              TEXT,
    county_name           TEXT,
    telephone             TEXT,
    hospital_type         TEXT,
    hospital_ownership    TEXT,
    emergency_services    TEXT,
    birthing_friendly     TEXT,
    overall_rating        TEXT,
    raw                   JSONB NOT NULL,
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cms_hospitals_state   ON cms_hospitals(state);
CREATE INDEX IF NOT EXISTS idx_cms_hospitals_zip     ON cms_hospitals(zip_code);
CREATE INDEX IF NOT EXISTS idx_cms_hospitals_type    ON cms_hospitals(hospital_type);
CREATE INDEX IF NOT EXISTS idx_cms_hospitals_rating  ON cms_hospitals(overall_rating);

-- Unified facility-level measures (flexible — stores any CMS facility measure dataset)
CREATE TABLE IF NOT EXISTS cms_hospital_measures (
    id                     BIGSERIAL PRIMARY KEY,
    dataset_id             TEXT NOT NULL,
    facility_id            TEXT NOT NULL,
    measure_id             TEXT,
    measure_name           TEXT,
    score                  TEXT,
    score_num              NUMERIC,
    lower_estimate         TEXT,
    lower_estimate_num     NUMERIC,
    higher_estimate        TEXT,
    higher_estimate_num    NUMERIC,
    compared_to_national   TEXT,
    denominator            TEXT,
    denominator_num        NUMERIC,
    start_date             TEXT,
    start_date_parsed      DATE,
    end_date               TEXT,
    end_date_parsed        DATE,
    footnote               TEXT,
    raw                    JSONB NOT NULL,
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, facility_id, measure_id)
);

-- Additive migration (safe re-run for existing tables)
ALTER TABLE cms_hospital_measures
    ADD COLUMN IF NOT EXISTS score_num           NUMERIC,
    ADD COLUMN IF NOT EXISTS lower_estimate_num  NUMERIC,
    ADD COLUMN IF NOT EXISTS higher_estimate_num NUMERIC,
    ADD COLUMN IF NOT EXISTS denominator_num     NUMERIC,
    ADD COLUMN IF NOT EXISTS start_date_parsed   DATE,
    ADD COLUMN IF NOT EXISTS end_date_parsed     DATE,
    ADD COLUMN IF NOT EXISTS footnote            TEXT;

CREATE INDEX IF NOT EXISTS idx_hm_facility  ON cms_hospital_measures(facility_id);
CREATE INDEX IF NOT EXISTS idx_hm_measure   ON cms_hospital_measures(measure_id);
CREATE INDEX IF NOT EXISTS idx_hm_dataset   ON cms_hospital_measures(dataset_id);

-- State-level benchmarks
CREATE TABLE IF NOT EXISTS cms_state_measures (
    id                     BIGSERIAL PRIMARY KEY,
    dataset_id             TEXT NOT NULL,
    state                  TEXT,
    measure_id             TEXT,
    measure_name           TEXT,
    score                  TEXT,
    compared_to_national   TEXT,
    raw                    JSONB NOT NULL,
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, state, measure_id)
);

-- National-level benchmarks
CREATE TABLE IF NOT EXISTS cms_national_measures (
    id                     BIGSERIAL PRIMARY KEY,
    dataset_id             TEXT NOT NULL,
    measure_id             TEXT,
    measure_name           TEXT,
    score                  TEXT,
    raw                    JSONB NOT NULL,
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, measure_id)
);

-- Footnote crosswalk (reference table — decodes footnote codes 1..32)
CREATE TABLE IF NOT EXISTS cms_footnote_crosswalk (
    footnote        TEXT PRIMARY KEY,
    footnote_text   TEXT,
    raw             JSONB NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Wide-format per-facility datasets (one row = one facility, many measures as columns).
-- Used for datasets like tqkv-mgxq (CJR Joint Replacement), 4jcv-atw7 (ASC Quality).
-- Keeps the full row in JSONB so any column is queryable without schema churn.
CREATE TABLE IF NOT EXISTS cms_wide_facility_snapshots (
    id             BIGSERIAL PRIMARY KEY,
    dataset_id     TEXT NOT NULL,
    facility_id    TEXT,
    npi            TEXT,
    facility_name  TEXT,
    state          TEXT,
    zip_code       TEXT,
    year           TEXT,
    raw            JSONB NOT NULL,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, facility_id, year)
);

CREATE INDEX IF NOT EXISTS idx_wide_facility  ON cms_wide_facility_snapshots(facility_id);
CREATE INDEX IF NOT EXISTS idx_wide_npi       ON cms_wide_facility_snapshots(npi);
CREATE INDEX IF NOT EXISTS idx_wide_dataset   ON cms_wide_facility_snapshots(dataset_id);

-- ==================================================================
-- FDA MAUDE (Phase 3) — device adverse events from openFDA
-- ==================================================================

CREATE TABLE IF NOT EXISTS fda_maude_events (
    report_number       TEXT PRIMARY KEY,
    event_type          TEXT,
    date_received       DATE,
    date_of_event       DATE,
    event_location      TEXT,
    report_source_code  TEXT,
    mdr_report_key      TEXT,
    manufacturer_name   TEXT,
    facility_name       TEXT,
    facility_state      TEXT,
    facility_zip        TEXT,
    patient_outcomes    TEXT,
    device_problems     TEXT,
    mdr_text            TEXT,
    raw                 JSONB NOT NULL,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_maude_event_type  ON fda_maude_events(event_type);
CREATE INDEX IF NOT EXISTS idx_maude_date        ON fda_maude_events(date_received);
CREATE INDEX IF NOT EXISTS idx_maude_fac_zip     ON fda_maude_events(facility_zip);
CREATE INDEX IF NOT EXISTS idx_maude_fac_state   ON fda_maude_events(facility_state);
CREATE INDEX IF NOT EXISTS idx_maude_source_code ON fda_maude_events(report_source_code);

CREATE TABLE IF NOT EXISTS fda_maude_devices (
    id                  BIGSERIAL PRIMARY KEY,
    report_number       TEXT NOT NULL REFERENCES fda_maude_events(report_number) ON DELETE CASCADE,
    seq                 INT,
    product_code        TEXT,
    brand_name          TEXT,
    generic_name        TEXT,
    manufacturer        TEXT,
    model_number        TEXT,
    catalog_number      TEXT,
    lot_number          TEXT,
    udi_di              TEXT,
    udi_public          TEXT,
    device_age          TEXT,
    device_availability TEXT,
    raw                 JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_maude_dev_pc    ON fda_maude_devices(product_code);
CREATE INDEX IF NOT EXISTS idx_maude_dev_brand ON fda_maude_devices(brand_name);
CREATE INDEX IF NOT EXISTS idx_maude_dev_mfr   ON fda_maude_devices(manufacturer);
CREATE INDEX IF NOT EXISTS idx_maude_dev_udi   ON fda_maude_devices(udi_di);
CREATE INDEX IF NOT EXISTS idx_maude_dev_rpt   ON fda_maude_devices(report_number);

-- FDA GUDID — Unique Device Identifier master. Source: openFDA /device/udi.
-- The device_description + brand_name + product_codes make this the bridge
-- we can text-match HCPCS long_desc against.
CREATE TABLE IF NOT EXISTS fda_gudid_devices (
    primary_di          TEXT PRIMARY KEY,
    product_code        TEXT,               -- FDA CDRH 3-letter classification (primary)
    product_codes_all   TEXT,               -- comma-joined when multiple
    brand_name          TEXT,
    company_name        TEXT,
    device_description  TEXT,
    gmdn_pt_name        TEXT,               -- Global Medical Device Nomenclature preferred term
    catalog_number      TEXT,               -- manufacturer catalog #
    version_model       TEXT,
    is_kit              BOOLEAN,
    is_combination      BOOLEAN,
    public_version_date DATE,
    raw                 JSONB NOT NULL,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gudid_pc     ON fda_gudid_devices(product_code);
CREATE INDEX IF NOT EXISTS idx_gudid_brand  ON fda_gudid_devices(brand_name);
CREATE INDEX IF NOT EXISTS idx_gudid_co     ON fda_gudid_devices(company_name);
-- Full-text index for fuzzy description matching against HCPCS long_desc
CREATE INDEX IF NOT EXISTS idx_gudid_desc_tsv
    ON fda_gudid_devices USING GIN (to_tsvector('english', coalesce(device_description, '')));

-- ==================================================================
-- CMS data-api/v1 datasets — Medicare utilization, DMEPOS, payments
-- Generic schema because these datasets have very different columns;
-- the raw JSONB preserves everything, with key joins surfaced as columns.
-- ==================================================================

CREATE TABLE IF NOT EXISTS cms_provider_summary (
    id                BIGSERIAL PRIMARY KEY,
    dataset_id        TEXT NOT NULL,
    year              INT,
    ccn               TEXT,      -- facility_id when applicable (hospital)
    npi               TEXT,      -- provider NPI when applicable
    referring_npi     TEXT,
    hcpcs_code        TEXT,      -- outpatient, DMEPOS, physician services
    drg_code          TEXT,      -- inpatient MS-DRG
    drg_description   TEXT,
    provider_state    TEXT,
    provider_city     TEXT,
    provider_name     TEXT,
    total_services    NUMERIC,
    total_beneficiaries NUMERIC,
    total_payment_amt NUMERIC,
    raw               JSONB NOT NULL,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cps_dataset ON cms_provider_summary(dataset_id);
CREATE INDEX IF NOT EXISTS idx_cps_ccn     ON cms_provider_summary(ccn);
CREATE INDEX IF NOT EXISTS idx_cps_npi     ON cms_provider_summary(npi);
CREATE INDEX IF NOT EXISTS idx_cps_drg     ON cms_provider_summary(drg_code);
CREATE INDEX IF NOT EXISTS idx_cps_hcpcs   ON cms_provider_summary(hcpcs_code);
CREATE INDEX IF NOT EXISTS idx_cps_year    ON cms_provider_summary(year);

-- Open Payments (manufacturer → provider financial ties)
CREATE TABLE IF NOT EXISTS cms_open_payments (
    id                        BIGSERIAL PRIMARY KEY,
    dataset_id                TEXT NOT NULL,
    year                      INT,
    record_id                 TEXT,
    physician_npi             TEXT,
    physician_name            TEXT,
    physician_specialty       TEXT,
    teaching_hospital_ccn     TEXT,
    teaching_hospital_name    TEXT,
    manufacturer_name         TEXT,
    product_name              TEXT,
    product_category          TEXT,
    nature_of_payment         TEXT,
    payment_total             NUMERIC,
    payment_date              DATE,
    raw                       JSONB NOT NULL,
    fetched_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_op_year           ON cms_open_payments(year);
CREATE INDEX IF NOT EXISTS idx_op_physician_npi  ON cms_open_payments(physician_npi);
CREATE INDEX IF NOT EXISTS idx_op_hospital_ccn   ON cms_open_payments(teaching_hospital_ccn);
CREATE INDEX IF NOT EXISTS idx_op_manufacturer   ON cms_open_payments(manufacturer_name);
CREATE INDEX IF NOT EXISTS idx_op_product        ON cms_open_payments(product_name);

-- HCPCS master dictionary (free CMS download, quarterly)
-- Links every procedure/device code used in Medicare to its description + BETOS category.
-- CPT (Level I) descriptions are AMA-copyrighted; we load them for research/internal use.
CREATE TABLE IF NOT EXISTS hcpcs_master (
    hcpcs_code       TEXT PRIMARY KEY,
    short_desc       TEXT,
    long_desc        TEXT,
    betos_code       TEXT,       -- 3-char clinical category code (e.g., P1G, D1A)
    pricing_indicator TEXT,      -- payment rule
    coverage_code    TEXT,       -- Medicare coverage status
    asc_payment_grp  TEXT,       -- ambulatory surgical center group
    mog_payment_grp  TEXT,
    type_of_service  TEXT,
    action_code      TEXT,       -- Added/Changed/Deleted this quarter
    code_family      TEXT,       -- A/B/C/D/E/G/H/J/K/L/M/P/Q/R/S/T/U/V — category prefix
    is_device        BOOLEAN,    -- derived: true for C/E/K/L codes
    effective_qtr    TEXT,       -- e.g. 'APR2026'
    raw_line         TEXT,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_hcpcs_family  ON hcpcs_master(code_family);
CREATE INDEX IF NOT EXISTS idx_hcpcs_device  ON hcpcs_master(is_device);
CREATE INDEX IF NOT EXISTS idx_hcpcs_betos   ON hcpcs_master(betos_code);

-- Bridge 1: FDA product_code ↔ CMS measure (static crosswalk, seeded manually)
CREATE TABLE IF NOT EXISTS bridge_product_code_to_measure (
    product_code     TEXT,
    cms_measure_id   TEXT,
    device_category  TEXT,
    confidence       TEXT,
    PRIMARY KEY (product_code, cms_measure_id)
);

-- Bridge 1b: HCPCS ↔ FDA product_code.
-- No authoritative public crosswalk exists; seeded manually for high-volume device
-- families, then extensible via GUDID description matching and brand fuzzy-match.
CREATE TABLE IF NOT EXISTS bridge_hcpcs_to_product_code (
    hcpcs_code       TEXT NOT NULL,
    product_code     TEXT NOT NULL,
    device_category  TEXT,
    match_method     TEXT NOT NULL,   -- 'manual_seed' | 'gudid_description' | 'brand_fuzzy'
    confidence       TEXT NOT NULL,   -- 'high' | 'medium' | 'low'
    source_notes     TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (hcpcs_code, product_code)
);

CREATE INDEX IF NOT EXISTS idx_br_h2p_hcpcs    ON bridge_hcpcs_to_product_code(hcpcs_code);
CREATE INDEX IF NOT EXISTS idx_br_h2p_pc       ON bridge_hcpcs_to_product_code(product_code);
CREATE INDEX IF NOT EXISTS idx_br_h2p_category ON bridge_hcpcs_to_product_code(device_category);

-- ==================================================================
-- Materialized view: hcpcs_device_profile
-- One row per (hcpcs_code, product_code) with human-readable labels
-- and an aggregated MAUDE event count. Hospitals × HCPCS utilization
-- joins layer on top of this once cms_provider_summary is populated.
-- ==================================================================
DROP MATERIALIZED VIEW IF EXISTS hcpcs_device_profile CASCADE;
CREATE MATERIALIZED VIEW hcpcs_device_profile AS
SELECT
    h.hcpcs_code,
    h.short_desc                             AS hcpcs_short_desc,
    h.long_desc                              AS hcpcs_long_desc,
    h.code_family,
    h.is_device,
    h.betos_code,
    b.product_code,
    b.device_category,
    b.match_method,
    b.confidence                             AS bridge_confidence,
    COUNT(DISTINCT d.report_number)          AS maude_event_count,
    COUNT(DISTINCT d.manufacturer) FILTER (WHERE d.manufacturer IS NOT NULL)
                                             AS maude_manufacturer_count
FROM hcpcs_master h
JOIN bridge_hcpcs_to_product_code b USING (hcpcs_code)
LEFT JOIN fda_maude_devices d ON d.product_code = b.product_code
GROUP BY
    h.hcpcs_code, h.short_desc, h.long_desc, h.code_family,
    h.is_device, h.betos_code,
    b.product_code, b.device_category, b.match_method, b.confidence;

CREATE UNIQUE INDEX IF NOT EXISTS idx_hdp_pk
    ON hcpcs_device_profile(hcpcs_code, product_code);
CREATE INDEX IF NOT EXISTS idx_hdp_category
    ON hcpcs_device_profile(device_category);
CREATE INDEX IF NOT EXISTS idx_hdp_events
    ON hcpcs_device_profile(maude_event_count DESC);

-- ==================================================================
-- Materialized view: hospital_hcpcs_enriched
-- The end-game join: for every hospital × HCPCS billed (via
-- cms_provider_summary), attach facility metadata, HCPCS description,
-- mapped FDA product_code, and MAUDE event count for that product.
-- Refreshes: REFRESH MATERIALIZED VIEW hospital_hcpcs_enriched;
-- Until cms_provider_summary is populated this view is empty but
-- valid — it will light up the moment the utilization ingest lands.
-- ==================================================================
DROP MATERIALIZED VIEW IF EXISTS hospital_hcpcs_enriched CASCADE;
CREATE MATERIALIZED VIEW hospital_hcpcs_enriched AS
SELECT
    s.ccn                                    AS facility_id,
    f.facility_name,
    f.state,
    f.city,
    f.hospital_type,
    f.overall_rating,
    s.year,
    s.hcpcs_code,
    h.short_desc                             AS hcpcs_desc,
    h.code_family,
    h.is_device,
    b.product_code,
    b.device_category,
    b.confidence                             AS bridge_confidence,
    s.total_services,
    s.total_beneficiaries,
    s.total_payment_amt,
    p.maude_event_count
FROM cms_provider_summary s
LEFT JOIN cms_hospitals f ON f.facility_id = s.ccn
LEFT JOIN hcpcs_master h USING (hcpcs_code)
LEFT JOIN bridge_hcpcs_to_product_code b USING (hcpcs_code)
LEFT JOIN (
    SELECT product_code, COUNT(DISTINCT report_number) AS maude_event_count
      FROM fda_maude_devices
     GROUP BY product_code
) p ON p.product_code = b.product_code
WHERE s.ccn IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_hhe_facility   ON hospital_hcpcs_enriched(facility_id);
CREATE INDEX IF NOT EXISTS idx_hhe_hcpcs      ON hospital_hcpcs_enriched(hcpcs_code);
CREATE INDEX IF NOT EXISTS idx_hhe_product    ON hospital_hcpcs_enriched(product_code);
CREATE INDEX IF NOT EXISTS idx_hhe_state      ON hospital_hcpcs_enriched(state);
CREATE INDEX IF NOT EXISTS idx_hhe_category   ON hospital_hcpcs_enriched(device_category);

-- Ingestion audit log
CREATE TABLE IF NOT EXISTS cms_ingestion_log (
    id               BIGSERIAL PRIMARY KEY,
    dataset_id       TEXT NOT NULL,
    dataset_name     TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    rows_fetched     INTEGER DEFAULT 0,
    rows_upserted    INTEGER DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'running',
    error            TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_dataset ON cms_ingestion_log(dataset_id);
CREATE INDEX IF NOT EXISTS idx_log_status  ON cms_ingestion_log(status);
