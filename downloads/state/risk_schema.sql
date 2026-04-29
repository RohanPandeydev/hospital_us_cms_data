
-- =============================================================
-- NY SPARCS Potentially Preventable Complications (public hospital adverse-event proxy)
-- Keyed by PFI (NY state's 3-4 digit Permanent Facility Identifier) rather than
-- regex/fuzzy on names — PFI↔CCN crosswalk comes from an NY DOH reference table.
-- =============================================================
CREATE TABLE IF NOT EXISTS sparcs_ppc_rate (
    discharge_year INT,
    pfi            TEXT,
    hospital_name  TEXT,
    ppc_group_num  INT,
    ppc_group_name TEXT,
    observed_rate  NUMERIC,  -- per 10,000 at-risk discharges
    adjusted_rate  NUMERIC,
    significance   TEXT,     -- Lower / Similar / Higher than expected
    ppc_version    TEXT,
    ccn            TEXT,     -- resolved via ny_pfi_to_ccn crosswalk
    fetched_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (discharge_year, pfi, ppc_group_num)
);
CREATE INDEX IF NOT EXISTS idx_sparcs_ccn ON sparcs_ppc_rate (ccn);
CREATE INDEX IF NOT EXISTS idx_sparcs_year_sig ON sparcs_ppc_rate (discharge_year, significance);

-- Official NY DOH crosswalk: PFI <-> Medicare CCN
-- Seeded from NY DOH Health Data NY dataset (jzqw-k7xg — "Health Facility General
-- Information") which exposes both `pfi` and the 6-digit `medicare_provider_id`.
CREATE TABLE IF NOT EXISTS ny_pfi_to_ccn (
    pfi  TEXT PRIMARY KEY,
    ccn  TEXT,
    fetched_at TIMESTAMPTZ DEFAULT now()
);
