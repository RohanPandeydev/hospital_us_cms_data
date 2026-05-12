# HCPCS Warehouse — Data Guide

A handbook for the 11 tables in the ClickHouse warehouse: what each one is, how to join them, and copy-paste-ready queries for the most common questions.

> **Database:** `hospital_us_cms_data`
> **Engine:** ClickHouse Cloud (`warehouse.dev.rp360.io:443`)
> **Connection:** see `.env` (`CLICKHOUSE_HOST`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`)

---

## 1. Table inventory

| # | Table | Rows | Grain (1 row =) | Key column(s) |
|---|---|---|---|---|
| 1 | `hcpcs_master` | 27,204 | one Medicare billing code | `hcpcs_code` |
| 2 | `cms_provider_summary` | 15,982,726 | one provider × one HCPCS/DRG × one year | `dataset_id, npi/ccn, hcpcs_code/drg_code` |
| 3 | `cms_open_payments` | 2,042,538 | one industry-to-physician payment record | `id` (record_id) |
| 4 | `npi_facility_affiliation` | 1,638,956 | one (physician NPI × hospital CCN) link | `npi, facility_ccn` |
| 5 | `cms_hospital_measures` | 60,001 | one (hospital × measure) quality score | `facility_id, measure_id` |
| 6 | `opps_addendum_b` | 56,161 | one (HCPCS × quarter) OPPS payment rule | `hcpcs_code, effective_quarter` |
| 7 | `clinical_trials` | 35,806 | one ClinicalTrials.gov study | `nct_id` |
| 8 | `clinical_trial_interventions` | 26,136 | one (trial × intervention) row | `nct_id, intervention_name` |
| 9 | `cms_hospitals` | 5,852 | one Medicare-certified hospital | `facility_id` (= CCN) |
| 10 | `stark_dhs_codes` | 5,237 | one (HCPCS × Stark category × year) row | `hcpcs_code, dhs_category, effective_year` |
| 11 | `cms_ingestion_log` | 108 | one ingestion run record | `run_id` |

---

## 2. Join map

```
hcpcs_master ──(hcpcs_code)──── opps_addendum_b
     │                                  │
     ├──(hcpcs_code)──── cms_provider_summary ──(npi)── npi_facility_affiliation ──(facility_ccn = facility_id)── cms_hospitals
     │                                                                                                                    │
     ├──(hcpcs_code)──── stark_dhs_codes                                                            (facility_id) ─ cms_hospital_measures
     │
     └──(hcpcs_code)──── clinical_trial_interventions ──(nct_id)── clinical_trials

cms_open_payments — fuzzy joins only (no FK to hcpcs_master);
                    matches via product_name / manufacturer_name keywords.
```

**Universal join keys:**
| Key | Type | Used by |
|---|---|---|
| `hcpcs_code` | 5-char string (e.g. `33208`, `C9601`, `E0601`) | hcpcs_master, opps_addendum_b, stark_dhs_codes, clinical_trial_interventions, cms_provider_summary (where populated) |
| `npi` | 10-digit string | cms_provider_summary, npi_facility_affiliation, cms_open_payments (`physician_npi`) |
| `facility_id` / `facility_ccn` / `ccn` | 6-char Medicare hospital ID | cms_hospitals, cms_hospital_measures, npi_facility_affiliation, cms_provider_summary |
| `nct_id` | NCT-prefixed trial ID | clinical_trials, clinical_trial_interventions |
| `apc_code` | OPPS payment group | opps_addendum_b ↔ `cms_provider_summary.raw['APC_Cd']` (outpatient dataset only) |

---

## 3. Per-table reference

### 3.1 `hcpcs_master`
Plain-English purpose: **the dictionary of every Medicare billing code.**

| Column | Meaning |
|---|---|
| `hcpcs_code` | 5-char code (PK) |
| `short_desc` | 28-char description (e.g. `"Aicd, dual chamber"`) |
| `long_desc` | Full description |
| `betos_code` | BETOS classification (D1A = device, M1A = office visit, etc.) |
| `code_family` | First letter of code (`A`, `C`, `E`, `J`, `L`, etc.) |
| `is_device` | `1` if it's a device/equipment code (C, E, K, L codes), else `0` |
| `effective_qtr` | Quarter this master was published from (e.g. `APR2026`) |

### 3.2 `cms_provider_summary`
**Provider-level Medicare billing.** 11 datasets stacked in one table — filter by `dataset_id` to slice.

`dataset_id` values:
- `medicare_physician_by_provider_service` — 8.4M rows. Doctor × HCPCS × Year. **Has HCPCS + NPI, no CCN.**
- `medicare_dmepos_by_supplier_service` — 5.5M rows. DME supplier × HCPCS × Year. **Has HCPCS + NPI, no CCN.**
- `medicare_physician_by_provider` — 1.2M rows. Doctor totals.
- `medicare_dmepos_by_referring_provider` — 383K rows. Referring doctor × HCPCS.
- `medicare_inpatient_by_provider_service` — 146K rows. Hospital × DRG × Year. **Has CCN + DRG, NOT HCPCS.** APC code lives in `raw` JSON.
- `medicare_outpatient_by_provider_service` — 117K rows. Hospital × APC × Year. **Has CCN, NOT HCPCS.** APC in `raw['APC_Cd']`.
- `medicare_dmepos_by_supplier` — 73K rows. Supplier totals.
- `provider_of_services` / `betos_classification` / `hospital_cost_report` / `medicare_inpatient_by_provider` — reference data.

### 3.3 `opps_addendum_b`
**The hospital-outpatient payment rulebook.** Tells you whether a HCPCS code is separately paid (J1/T/S), bundled (N), DME (Y), or excluded (E1).

| Column | Meaning |
|---|---|
| `status_indicator` | `J1`/`T`/`S` = separately paid; `N` = packaged; `Y` = paid via DME fee schedule; `E1` = not covered |
| `apc_code` | Ambulatory Payment Classification (groups HCPCS codes that pay the same rate) |
| `payment_rate` | National unadjusted Medicare payment $ |
| `effective_quarter` | `2025Q1`, `2025Q2`, `2026Q1` currently loaded |

### 3.4 `stark_dhs_codes`
**Stark Law list** — codes Medicare flags for self-referral compliance.

`dhs_category` values: `CLINICAL LABORATORY SERVICES`, `RADIOLOGY AND CERTAIN OTHER IMAGING SERVICES`, `RADIATION THERAPY SERVICES AND SUPPLIES`, `PHYSICAL THERAPY, OCCUPATIONAL THERAPY, AND OUTPATIENT SPEECH-LANGUAGE PATHOLOGY SERVICES`, `PREVENTIVE SCREENING TESTS AND VACCINES`.

### 3.5 `cms_open_payments`
**Industry-to-physician payments.** ~2M payment records (2022–2024). Joinable by `physician_npi` but **not** by HCPCS — match against `product_name` / `manufacturer_name` for fuzzy device-product mapping.

### 3.6 `clinical_trials` + `clinical_trial_interventions`
**ClinicalTrials.gov.** Trials selected by device-family search buckets. `clinical_trial_interventions.hcpcs_code` is populated by **Groq AI classification** for ~2,610 device-relevant interventions.

### 3.7 `cms_hospitals`
**Hospital roster.** Every Medicare-certified hospital. `facility_id` = CCN.

### 3.8 `cms_hospital_measures`
**Quality scorecards.** Each hospital × measure (HRRP readmissions, MORT, PSI, HAI, HCAHPS, etc.).

Useful `measure_id` patterns:
- `READM_30_*` — raw 30-day readmission % (HF, AMI, CABG, PN, COPD, HIP_KNEE)
- `READM-30-*-HRRP` — Excess Readmission Ratio (penalty if > 1.0)
- `MORT_30_*` — 30-day mortality %
- `PSI_*` — patient safety indicators
- `HAI_*` — hospital-acquired infections (SIR ratios)
- `EDAC_30_*` — excess days in acute care per 100 discharges
- `Hybrid_HWR`, `Hybrid_HWM` — hospital-wide readmission / mortality

### 3.9 `npi_facility_affiliation`
**Physician NPI → hospital CCN bridge.** 1.6M rows. One physician can be affiliated with multiple hospitals — `(npi, facility_ccn)` is the unique key.

### 3.10 `cms_ingestion_log`
**Pipeline audit log.** Every `python run.py ingest` writes a row.

---

## 4. Copy-paste queries

> All queries assume database `hospital_us_cms_data`. Note: ClickHouse `FINAL` modifier must come **after** the alias (e.g. `FROM hcpcs_master AS m FINAL`), or wrap in a sub-select (`FROM (SELECT * FROM x FINAL) AS x`).

### Q1. Everything we know about one HCPCS code
```sql
SELECT
  m.short_desc,
  m.long_desc,
  m.is_device,
  (SELECT count() FROM cms_provider_summary       WHERE hcpcs_code = 'C9601') AS billing_records,
  (SELECT count() FROM stark_dhs_codes            WHERE hcpcs_code = 'C9601') AS stark_dhs_designations,
  (SELECT count() FROM opps_addendum_b            WHERE hcpcs_code = 'C9601') AS opps_quarters_published,
  (SELECT countDistinct(nct_id)
     FROM (SELECT * FROM clinical_trial_interventions FINAL)
     WHERE hcpcs_code = 'C9601')                                              AS linked_trials
FROM hcpcs_master AS m FINAL
WHERE m.hcpcs_code = 'C9601';
```

### Q2. Top hospitals billing a specific HCPCS code (via physician affiliations)
```sql
WITH
  '33208' AS the_code,                  -- single-chamber pacemaker
  npi_billed AS (
    SELECT npi, sum(total_services) AS svcs, sum(total_payment_amt) AS paid
    FROM cms_provider_summary
    WHERE hcpcs_code = the_code AND npi != ''
      AND dataset_id IN ('medicare_physician_by_provider_service',
                         'medicare_dmepos_by_supplier_service')
    GROUP BY npi
  ),
  npi_ccn AS (SELECT npi, facility_ccn FROM (SELECT * FROM npi_facility_affiliation FINAL))
SELECT
  aff.facility_ccn AS ccn,
  any(h.facility_name) AS hospital,
  any(h.state)         AS state,
  any(h.overall_rating) AS star_rating,
  countDistinct(npi_billed.npi) AS affiliated_mds,
  sum(npi_billed.svcs) AS total_services,
  sum(npi_billed.paid) AS total_paid
FROM npi_billed
INNER JOIN npi_ccn AS aff USING npi
LEFT JOIN cms_hospitals FINAL AS h ON aff.facility_ccn = h.facility_id
GROUP BY ccn
ORDER BY total_services DESC NULLS LAST
LIMIT 20;
```

### Q3. OPPS payment rate + APC for a code (most-recent quarter)
```sql
SELECT effective_quarter, status_indicator, apc_code, payment_rate, national_copayment
FROM opps_addendum_b
WHERE hcpcs_code = 'C9601'
ORDER BY effective_quarter DESC;
```

### Q4. All HCPCS codes sharing an APC (the "bundle")
```sql
SELECT hcpcs_code, short_descriptor, payment_rate, status_indicator
FROM opps_addendum_b
WHERE apc_code = '5193'
  AND effective_quarter = '2026Q1'
ORDER BY payment_rate DESC, hcpcs_code;
```

### Q5. Top 10 hospitals by 30-day heart-failure readmission ratio (worst-performing)
```sql
SELECT h.facility_name, h.state, h.overall_rating,
       m.score_num AS err_ratio,
       m.compared_to_national
FROM cms_hospitals AS h FINAL
INNER JOIN cms_hospital_measures AS m FINAL
  ON h.facility_id = m.facility_id
WHERE m.measure_id = 'READM-30-HF-HRRP'
  AND m.score_num IS NOT NULL
ORDER BY m.score_num DESC
LIMIT 10;
```

### Q6. Full quality scorecard for one hospital
```sql
SELECT dataset_id, measure_id, measure_name,
       score, score_num, compared_to_national, denominator
FROM cms_hospital_measures FINAL
WHERE facility_id = '050441'           -- Stanford Health Care
ORDER BY dataset_id, measure_id;
```

### Q7. Clinical trials linked to a HCPCS code
```sql
SELECT t.nct_id, t.brief_title, t.lead_sponsor, t.overall_status,
       t.start_date, t.device_category,
       i.intervention_name, i.intervention_type
FROM (SELECT * FROM clinical_trial_interventions FINAL) AS i
INNER JOIN (SELECT * FROM clinical_trials FINAL) AS t
  ON i.nct_id = t.nct_id
WHERE i.hcpcs_code = 'C9601'
ORDER BY t.start_date DESC NULLS LAST;
```

### Q8. Top manufacturers paying physicians (Open Payments)
```sql
SELECT manufacturer_name,
       count() AS payment_count,
       countDistinct(physician_npi) AS unique_mds,
       round(sum(payment_total)/1e6, 2) AS million_usd
FROM cms_open_payments
WHERE manufacturer_name != ''
GROUP BY manufacturer_name
ORDER BY million_usd DESC NULLS LAST
LIMIT 20;
```

### Q9. Boston Scientific spend by product
```sql
SELECT product_name,
       count() AS payments,
       countDistinct(physician_npi) AS unique_mds,
       round(sum(payment_total), 0) AS total_usd
FROM cms_open_payments
WHERE lowerUTF8(manufacturer_name) LIKE '%boston scientific%'
  AND product_name != ''
GROUP BY product_name
ORDER BY total_usd DESC NULLS LAST
LIMIT 25;
```

### Q10. Top physicians billing a code, joined to their hospital affiliations
```sql
WITH '33208' AS the_code
SELECT cps.npi,
       cps.provider_name,
       cps.provider_state,
       cps.total_services,
       cps.total_payment_amt,
       groupArray(aff.facility_ccn) AS hospital_ccns
FROM cms_provider_summary AS cps
LEFT JOIN (SELECT npi, facility_ccn FROM (SELECT * FROM npi_facility_affiliation FINAL)) AS aff
  ON cps.npi = aff.npi
WHERE cps.hcpcs_code = the_code
  AND cps.dataset_id = 'medicare_physician_by_provider_service'
GROUP BY cps.npi, cps.provider_name, cps.provider_state, cps.total_services, cps.total_payment_amt
ORDER BY cps.total_services DESC NULLS LAST
LIMIT 20;
```

### Q11. Top outpatient APCs by hospital (uses the JSON-embedded APC code)
```sql
SELECT JSONExtractString(raw, 'APC_Cd')   AS apc,
       JSONExtractString(raw, 'APC_Desc') AS apc_desc,
       sum(toFloat64OrNull(JSONExtractString(raw, 'CAPC_Srvcs'))) AS services
FROM cms_provider_summary
WHERE dataset_id = 'medicare_outpatient_by_provider_service'
  AND ccn = '050441'                    -- Stanford
GROUP BY apc, apc_desc
HAVING apc != ''
ORDER BY services DESC NULLS LAST
LIMIT 20;
```

### Q12. Stark DHS designations for a code, all years
```sql
SELECT effective_year, dhs_category, short_description
FROM stark_dhs_codes
WHERE hcpcs_code = '76700'
ORDER BY effective_year DESC, dhs_category;
```

### Q13. State-level usage map for a HCPCS code
```sql
SELECT provider_state,
       countDistinct(npi)         AS providers,
       sum(total_services)        AS total_services,
       sum(total_payment_amt)     AS total_paid
FROM cms_provider_summary
WHERE hcpcs_code = 'E0601' AND provider_state != ''
GROUP BY provider_state
ORDER BY total_services DESC NULLS LAST;
```

### Q14. Recent ingestion runs
```sql
SELECT dataset_id, started_at, finished_at,
       rows_fetched, rows_upserted, status
FROM cms_ingestion_log
ORDER BY started_at DESC
LIMIT 20;
```

### Q15. Big cross-source pivot: top 25 device codes by data richness
```sql
WITH
  claims AS (
    SELECT hcpcs_code, count() AS billing_rows
    FROM cms_provider_summary WHERE hcpcs_code != ''
    GROUP BY hcpcs_code
  ),
  trials AS (
    SELECT hcpcs_code, countDistinct(nct_id) AS n_trials
    FROM (SELECT * FROM clinical_trial_interventions FINAL)
    WHERE hcpcs_code IS NOT NULL
    GROUP BY hcpcs_code
  ),
  opps AS (
    SELECT hcpcs_code, max(payment_rate) AS pay_rate
    FROM opps_addendum_b GROUP BY hcpcs_code
  )
SELECT m.hcpcs_code, m.short_desc, m.code_family,
       claims.billing_rows, trials.n_trials, opps.pay_rate
FROM (SELECT * FROM hcpcs_master FINAL) AS m
INNER JOIN claims USING hcpcs_code
INNER JOIN trials USING hcpcs_code
LEFT  JOIN opps   USING hcpcs_code
WHERE m.is_device = 1
ORDER BY trials.n_trials DESC, claims.billing_rows DESC
LIMIT 25;
```

---

## 5. Connecting

### Python (used by the UI)
```python
from src import db
conn = db._raw_client()
rows = conn.query("SELECT count() FROM hcpcs_master").result_rows
print(rows)
```

### ClickHouse HTTP / clickhouse-client
```bash
clickhouse-client --host <CLICKHOUSE_HOST> --port 443 --secure \
                  --user <CLICKHOUSE_USER> --password <CLICKHOUSE_PASSWORD> \
                  --database hospital_us_cms_data \
                  --query "SELECT count() FROM hcpcs_master"
```

### Any BI tool (Metabase, Superset, Hex, etc.)
Use ClickHouse HTTPS driver, point at host/port from `.env`. The 11 tables show up in the `hospital_us_cms_data` schema.

---

## 6. Gotchas

1. **Use `FINAL` on ReplacingMergeTree tables** (`hcpcs_master`, `cms_hospitals`, `cms_hospital_measures`, `clinical_trials`, `clinical_trial_interventions`, `npi_facility_affiliation`) to deduplicate at query time. Without it, you may see prior versions of rows.
2. **`cms_provider_summary` is plain MergeTree** (no FINAL needed) and has 11 different `dataset_id` slices. Always filter by `dataset_id`.
3. **Inpatient/outpatient PUF rows have no HCPCS** — they're keyed by DRG / APC. APC code lives in `raw['APC_Cd']`; join via `opps_addendum_b.apc_code`.
4. **`fetched_at` is `Nullable` on some legacy tables.** Some older rows have `NULL` here even though they're current.
5. **Trial → HCPCS mapping** only covers `intervention_type IN ('DEVICE','PROCEDURE')` interventions where Groq found a candidate. Most rows in `clinical_trial_interventions` have `hcpcs_code IS NULL`.
6. **Open Payments has no HCPCS column** — match via `product_name` / `manufacturer_name` keywords only.
7. **CCN format** — 6 characters, leading zeros matter (e.g. `050441` not `50441`).
