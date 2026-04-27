-- =============================================================
-- Unified Healthcare Device Risk Intelligence — Postgres schema
-- Target DB: cms_hospitals  (user: rohankumarpandey)
-- =============================================================
-- Extends the existing warehouse with:
--   * fda_recalls                          — FDA device recall enforcement actions
--   * clinical_trials                      — ClinicalTrials.gov v2 studies
--   * clinical_trial_interventions         — trial × intervention rows
--   * hospital_device_risk_intelligence    — unified JSON output, one row per (CCN × device_category)
--   * risk_ingestion_log                   — per-run audit trail
-- Every *_raw JSONB column preserves the upstream payload.

-- ---------- FDA 510(k) clearances (authoritative K#↔product_code↔applicant) ----------
-- The master source for the "primary match = K_NUMBER" path in the device
-- matcher. Every 510(k) carries a single product_code and a single applicant
-- (manufacturer), so once we resolve a device's K# we inherit its product
-- code directly — no keyword inference needed.
CREATE TABLE IF NOT EXISTS fda_510k (
    k_number             TEXT PRIMARY KEY,
    applicant            TEXT,        -- manufacturer legal name
    device_name          TEXT,
    product_code         TEXT,        -- FDA CDRH product code
    decision_date        DATE,
    decision_description TEXT,
    clearance_type       TEXT,
    statement_or_summary TEXT,
    country_code         TEXT,
    postal_code          TEXT,
    state                TEXT,
    raw                  JSONB NOT NULL,
    fetched_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_510k_pc        ON fda_510k (product_code);
CREATE INDEX IF NOT EXISTS idx_510k_applicant ON fda_510k (applicant);
CREATE INDEX IF NOT EXISTS idx_510k_applicant_lower ON fda_510k (lower(applicant));
CREATE INDEX IF NOT EXISTS idx_510k_dev_tsv ON fda_510k USING gin (
  to_tsvector('simple', coalesce(device_name, '') || ' ' || coalesce(applicant, ''))
);

-- ---------- FDA recalls (Class I / II / III enforcement actions) ----------
CREATE TABLE IF NOT EXISTS fda_recalls (
    recall_number        TEXT PRIMARY KEY,
    event_id             TEXT,
    product_code         TEXT,
    firm_name            TEXT,
    brand_name           TEXT,
    product_description  TEXT,
    reason_for_recall    TEXT,
    recall_class         TEXT,        -- "Class I" | "Class II" | "Class III"
    status               TEXT,
    recall_initiation_date  DATE,
    center_classification_date DATE,
    termination_date     DATE,
    country              TEXT,
    state                TEXT,
    city                 TEXT,
    distribution_pattern TEXT,
    product_quantity     TEXT,
    voluntary_mandated   TEXT,
    device_category      TEXT,       -- inferred by keyword-match on product_description
    raw                  JSONB NOT NULL,
    fetched_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fda_recalls_pc    ON fda_recalls (product_code);
CREATE INDEX IF NOT EXISTS idx_fda_recalls_class ON fda_recalls (recall_class);
CREATE INDEX IF NOT EXISTS idx_fda_recalls_firm  ON fda_recalls (firm_name);
CREATE INDEX IF NOT EXISTS idx_fda_recalls_cat   ON fda_recalls (device_category);

-- Device-match provenance: how the recall row was linked to a device family.
-- These columns are written by src/risk_device_matcher.py.
ALTER TABLE fda_recalls ADD COLUMN IF NOT EXISTS matched_k_number      TEXT;
ALTER TABLE fda_recalls ADD COLUMN IF NOT EXISTS matched_product_code  TEXT;
ALTER TABLE fda_recalls ADD COLUMN IF NOT EXISTS match_method          TEXT;
    -- one of: k_number | product_code | manufacturer | regex | llm | null
ALTER TABLE fda_recalls ADD COLUMN IF NOT EXISTS match_confidence      TEXT;
    -- one of: high | medium | low
CREATE INDEX IF NOT EXISTS idx_fda_recalls_k        ON fda_recalls (matched_k_number);
CREATE INDEX IF NOT EXISTS idx_fda_recalls_matched_pc ON fda_recalls (matched_product_code);
CREATE INDEX IF NOT EXISTS idx_fda_recalls_method   ON fda_recalls (match_method);

-- ---------- Clinical evidence (ClinicalTrials.gov) ----------
CREATE TABLE IF NOT EXISTS clinical_trials (
    nct_id              TEXT PRIMARY KEY,
    brief_title         TEXT,
    official_title      TEXT,
    overall_status      TEXT,
    phase               TEXT,
    study_type          TEXT,
    condition           TEXT,
    enrollment_count    INTEGER,
    has_results         BOOLEAN DEFAULT FALSE,
    why_stopped         TEXT,
    start_date          DATE,
    completion_date     DATE,
    last_update_date    DATE,
    lead_sponsor        TEXT,
    device_category     TEXT,        -- set by ingest based on search-term bucket
    raw                 JSONB NOT NULL,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ctg_device_cat ON clinical_trials (device_category);
CREATE INDEX IF NOT EXISTS idx_ctg_status     ON clinical_trials (overall_status);

CREATE TABLE IF NOT EXISTS clinical_trial_interventions (
    nct_id              TEXT NOT NULL,
    intervention_name   TEXT NOT NULL,
    intervention_type   TEXT,
    description         TEXT,
    PRIMARY KEY (nct_id, intervention_name),
    FOREIGN KEY (nct_id) REFERENCES clinical_trials(nct_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ctg_int_name ON clinical_trial_interventions (intervention_name);

-- ---------- Unified Risk Intelligence output (THE final JSON) ----------
-- One row per (CCN × device_category). `payload` is the full blueprint JSON.
-- Flattened score columns let us index / sort / filter without JSON casts.
CREATE TABLE IF NOT EXISTS hospital_device_risk_intelligence (
    ccn                 TEXT NOT NULL,
    device_category     TEXT NOT NULL,
    product_code        TEXT,
    hospital_name       TEXT,
    state               TEXT,
    quality_score       NUMERIC,         -- CMS overall_rating coerced to numeric
    total_events        INTEGER DEFAULT 0,
    death_count         INTEGER DEFAULT 0,
    injury_count        INTEGER DEFAULT 0,
    malfunction_count   INTEGER DEFAULT 0,
    has_recall          BOOLEAN DEFAULT FALSE,
    worst_recall_class  TEXT,
    trial_count         INTEGER DEFAULT 0,
    manufacturer_exposure TEXT,          -- 'none' | 'low' | 'medium' | 'high'
    maude_score         NUMERIC,         -- 0-10 normalized adverse-event score
    recall_override     BOOLEAN DEFAULT FALSE,
    final_score         NUMERIC,         -- 0-10 composite risk
    hospital_device_link_confidence TEXT,   -- 'weak' | 'moderate' | 'strong'
    device_risk_confidence          TEXT,
    linkage_type        TEXT DEFAULT 'indirect',
    payload             JSONB NOT NULL,  -- FULL unified risk-intelligence JSON
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ccn, device_category)
);
CREATE INDEX IF NOT EXISTS idx_hdri_state        ON hospital_device_risk_intelligence (state);
CREATE INDEX IF NOT EXISTS idx_hdri_final_score  ON hospital_device_risk_intelligence (final_score DESC);
CREATE INDEX IF NOT EXISTS idx_hdri_device_cat   ON hospital_device_risk_intelligence (device_category);
CREATE INDEX IF NOT EXISTS idx_hdri_product_code ON hospital_device_risk_intelligence (product_code);
CREATE INDEX IF NOT EXISTS idx_hdri_recall       ON hospital_device_risk_intelligence (has_recall) WHERE has_recall;
CREATE INDEX IF NOT EXISTS idx_hdri_payload_gin  ON hospital_device_risk_intelligence USING gin (payload);

-- =============================================================
-- HOSPITAL ↔ DEVICE DIRECT LINKAGE (senior data-engineer layer)
-- =============================================================
-- Three new tables create a real, verifiable hospital-to-device linkage,
-- working around MAUDE's facility-name redaction by triangulating through
-- Medicare claims data (DRG discharges) and code-crosswalks (HCPCS/CPT/
-- UDI/product_code).

-- Medicare Inpatient PUF — each row = (CCN, DRG) with annual discharge volume
-- Source: data.cms.gov Medicare Inpatient Hospitals by Provider and Service
-- When a hospital bills DRG 469 (major joint replacement), that's 1 patient
-- exposed to a hip/knee implant. This is direct, auditable, CCN-level exposure.
CREATE TABLE IF NOT EXISTS cms_hospital_drg_volume (
    ccn                 TEXT NOT NULL,
    drg_code            TEXT NOT NULL,
    drg_description     TEXT,
    year                INTEGER NOT NULL,
    total_discharges    INTEGER,
    avg_submitted_charge  NUMERIC,
    avg_total_payment     NUMERIC,
    avg_medicare_payment  NUMERIC,
    PRIMARY KEY (ccn, drg_code, year)
);
CREATE INDEX IF NOT EXISTS idx_drg_vol_drg  ON cms_hospital_drg_volume (drg_code);
CREATE INDEX IF NOT EXISTS idx_drg_vol_ccn  ON cms_hospital_drg_volume (ccn);

-- DRG → device_category mapping.
-- A DRG can map to multiple device categories (e.g. coronary stent DRG 246
-- covers both bare-metal and drug-eluting stents); weight allocates the
-- discharge count across categories for fair attribution.
CREATE TABLE IF NOT EXISTS drg_to_device_category (
    drg_code            TEXT NOT NULL,
    device_category     TEXT NOT NULL,
    weight              NUMERIC NOT NULL DEFAULT 1.0,
    rationale           TEXT,
    PRIMARY KEY (drg_code, device_category)
);

-- FDA Device Classification (product_code master)
-- Source: https://api.fda.gov/device/classification.json
-- Adds device_class (I/II/III), review_panel, medical_specialty, intended_use
-- for every FDA-registered product code — fills regulatory gaps the 510(k)
-- catalog doesn't cover.
CREATE TABLE IF NOT EXISTS fda_device_classification (
    product_code        TEXT PRIMARY KEY,
    device_name         TEXT,
    device_class        TEXT,
    regulation_number   TEXT,
    medical_specialty   TEXT,
    medical_specialty_description TEXT,
    review_panel        TEXT,
    submission_type_id  TEXT,
    definition          TEXT,
    life_sustain_support_flag TEXT,
    implant_flag        TEXT,
    third_party_flag    TEXT,
    gmp_exempt_flag     TEXT,
    raw                 JSONB NOT NULL,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fda_cls_class    ON fda_device_classification (device_class);
CREATE INDEX IF NOT EXISTS idx_fda_cls_specialty ON fda_device_classification (medical_specialty);
CREATE INDEX IF NOT EXISTS idx_fda_cls_implant   ON fda_device_classification (implant_flag);

-- Unified code crosswalk: HCPCS ↔ CPT ↔ product_code ↔ UDI ↔ brand ↔ manufacturer
-- Derived from: bridge_hcpcs_to_product_code + fda_gudid_devices + fda_510k +
-- drg_to_device_category. Materialized so UI lookups are O(1).
CREATE TABLE IF NOT EXISTS device_code_crosswalk (
    id                  BIGSERIAL PRIMARY KEY,
    device_category     TEXT NOT NULL,
    code_type           TEXT NOT NULL,  -- 'hcpcs' | 'cpt' | 'drg' | 'product_code' | 'udi_di' | 'k_number' | 'gmdn' | 'brand' | 'manufacturer'
    code_value          TEXT NOT NULL,
    display_name        TEXT,
    source              TEXT,           -- 'bridge' | 'gudid' | '510k' | 'drg_map' | 'fda_classification'
    confidence          TEXT,           -- 'high' | 'medium' | 'low'
    linked_brand        TEXT,
    linked_manufacturer TEXT,
    linked_product_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_crosswalk_cat   ON device_code_crosswalk (device_category);
CREATE INDEX IF NOT EXISTS idx_crosswalk_type  ON device_code_crosswalk (code_type);
CREATE INDEX IF NOT EXISTS idx_crosswalk_val   ON device_code_crosswalk (code_value);
CREATE INDEX IF NOT EXISTS idx_crosswalk_cat_type ON device_code_crosswalk (device_category, code_type);

-- Medicare Physician & Other Practitioners — NPI-level HCPCS volume.
-- Used to cross-link physicians to hospitals (via Open Payments
-- teaching_hospital_ccn + NPPES).
CREATE TABLE IF NOT EXISTS cms_physician_hcpcs_volume (
    npi                 TEXT NOT NULL,
    hcpcs_code          TEXT NOT NULL,
    year                INTEGER NOT NULL,
    provider_type       TEXT,
    place_of_service    TEXT,
    tot_benes           INTEGER,
    tot_services        INTEGER,
    avg_mdcr_pymt_amt   NUMERIC,
    PRIMARY KEY (npi, hcpcs_code, place_of_service, year)
);
CREATE INDEX IF NOT EXISTS idx_phy_hcpcs_npi    ON cms_physician_hcpcs_volume (npi);
CREATE INDEX IF NOT EXISTS idx_phy_hcpcs_code   ON cms_physician_hcpcs_volume (hcpcs_code);

-- View: device-adjacent CMS hospital quality measures → device_category
-- Connects Hospital Compare measures (PSI_90, COMP_HIP_KNEE, MORT_30_CABG...)
-- to the device categories affected. Fills in the "complication_rate" field.
CREATE OR REPLACE VIEW hospital_device_complications AS
    SELECT hm.facility_id AS ccn, hm.measure_id, hm.measure_name,
           hm.score, hm.score_num, hm.compared_to_national,
           CASE
             WHEN hm.measure_id IN ('COMP_HIP_KNEE','READM_30_HIP_KNEE','READM-30-HIP-KNEE-HRRP')
                  THEN 'hip_knee_implant'
             WHEN hm.measure_id IN ('MORT_30_CABG','READM_30_CABG','READM-30-CABG-HRRP')
                  THEN 'cabg_conduit'
             WHEN hm.measure_id IN ('PSI_12')  -- perioperative PE/DVT
                  THEN 'ivc_filter'
             WHEN hm.measure_id IN ('PSI_13')  -- postop sepsis
                  THEN 'central_venous_catheter'
             WHEN hm.measure_id IN ('PSI_90')  -- composite patient safety
                  THEN 'multi_device'
             ELSE NULL
           END AS device_category_link
      FROM cms_hospital_measures hm
     WHERE hm.measure_id IN (
         'COMP_HIP_KNEE','READM_30_HIP_KNEE','READM-30-HIP-KNEE-HRRP',
         'MORT_30_CABG','READM_30_CABG','READM-30-CABG-HRRP',
         'PSI_12','PSI_13','PSI_90'
       )
       AND hm.score IS NOT NULL;

-- ---------- Audit ----------
CREATE TABLE IF NOT EXISTS risk_ingestion_log (
    id             BIGSERIAL PRIMARY KEY,
    run_id         UUID NOT NULL,
    source         TEXT NOT NULL,          -- 'recalls' | 'clinical_trials' | 'open_payments' | 'assemble'
    started_at     TIMESTAMPTZ NOT NULL,
    finished_at    TIMESTAMPTZ,
    rows_fetched   INTEGER,
    rows_upserted  INTEGER,
    status         TEXT,                   -- 'ok' | 'error'
    error          TEXT,
    params         JSONB
);
CREATE INDEX IF NOT EXISTS idx_risk_log_source ON risk_ingestion_log (source);
CREATE INDEX IF NOT EXISTS idx_risk_log_run    ON risk_ingestion_log (run_id);
