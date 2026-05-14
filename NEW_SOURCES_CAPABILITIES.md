# What the 12 New Sources Unlock

A working catalogue of the new analytical capabilities now possible against
the `hospital_us_cms_data` ClickHouse warehouse after the 7.6M-row ingestion.
Every query below is real and runnable against the live database.

---

## TL;DR — 5 new analytical layers

| Layer | Powered by | Lift |
|---|---|---|
| 🔴 **Compliance red flags** | `oig_leie_exclusions` | Flag any NPI banned from federal healthcare programs |
| 💰 **Full reimbursement picture** | `mpfs_rates` + existing `opps_addendum_b` | Office-setting + hospital-setting $ per HCPCS |
| 🏥 **Full facility universe** | `pos_facilities` | ASCs + SNFs + dialysis + hospice — 6× facility expansion |
| 👤 **Provider eligibility filter** | `medicare_opt_out`, `order_referring_npi`, `medicare_ffs_enrollment`, `medicare_revalidation` | Disqualify untargetable / lapsed NPIs |
| 🎯 **Clinical-category targeting** | `betos_crosswalk`, `npi_taxonomy_crosswalk` | Filter HCPCS by clinical lens; filter NPIs by specialty |

---

## 🔴 Compliance Intelligence

### 1. Any NPI in our pipeline currently OIG-excluded?

```sql
WITH leie AS (SELECT * FROM oig_leie_exclusions FINAL WHERE npi != '')
SELECT op.physician_npi, op.physician_name,
       leie.exclusion_type, leie.exclusion_date, leie.state,
       sum(op.payment_total) AS total_industry_$
FROM cms_open_payments op
INNER JOIN leie ON op.physician_npi = leie.npi
GROUP BY op.physician_npi, op.physician_name,
         leie.exclusion_type, leie.exclusion_date, leie.state
ORDER BY total_industry_$ DESC;
```

Already returns **105 NPIs** with industry-payment history AND active OIG exclusion. Each one is a compliance landmine for a manufacturer's sales pipeline.

### 2. Bulk-screen a list of NPIs against the LEIE

```sql
WITH targets AS (
  SELECT npi FROM npi_facility_affiliation FINAL WHERE facility_ccn = '330214'
),
leie AS (SELECT npi, exclusion_type, exclusion_date FROM oig_leie_exclusions FINAL)
SELECT t.npi, leie.exclusion_type, leie.exclusion_date,
       multiIf(leie.npi != '', 'EXCLUDED', 'clean') AS status
FROM targets t
LEFT JOIN leie ON t.npi = leie.npi;
```

Use case: before a rep visits a hospital, batch-check every NPI in the affiliation list. Returns a red/green badge per provider.

### 3. Providers due for revalidation (lapse risk)

```sql
SELECT npi, last_name, org_name, revalidation_due_date
FROM medicare_revalidation FINAL
WHERE revalidation_due_date BETWEEN today() AND today() + 90
ORDER BY revalidation_due_date ASC;
```

Lapsed revalidation = billing privileges suspended = no Medicare reimbursement = no procedure volume. Useful early-warning signal.

---

## 💰 Full Reimbursement Picture

### 4. Office vs hospital reimbursement for a device CPT

```sql
SELECT m.hcpcs_code, m.short_descriptor,
       round(m.non_facility_payment, 2) AS office_setting_$,
       round(m.facility_payment, 2)     AS hospital_setting_$,
       round(m.non_facility_payment - m.facility_payment, 2) AS site_differential_$,
       m.work_rvu,
       m.status_code
FROM mpfs_rates FINAL m
WHERE m.hcpcs_code IN ('33340','61885','61886','63685')   -- Watchman, DBS, SCS
  AND m.status_code = 'A'                                  -- active codes only
  AND m.modifier = '';
```

Before MPFS, we only had `opps_addendum_b` (hospital outpatient setting). Now we can see when a procedure is **more profitable in an ASC than in a hospital** — directly informs where reps should push for adoption.

### 5. Codes where site-of-service swing is largest

```sql
SELECT hcpcs_code, short_descriptor,
       round(non_facility_payment - facility_payment, 2) AS swing_$
FROM mpfs_rates FINAL
WHERE status_code = 'A' AND modifier = ''
  AND non_facility_payment > 0 AND facility_payment > 0
ORDER BY swing_$ DESC LIMIT 20;
```

A large positive swing means the physician makes more in their office than at the hospital — a real-world signal that adoption is moving out of hospitals into ASCs/offices.

### 6. Per-HCPCS dollars + uncompensated burden of a hospital

```sql
SELECT h.facility_id, h.facility_name,
       hc.total_beds,
       round(hc.total_charges/1e6, 1)   AS charges_M,
       round(hc.uncompensated_care_cost/1e6, 2) AS uncomp_M,
       round(hc.uncompensated_care_cost / nullIf(hc.total_costs, 0) * 100, 2) AS uncomp_pct
FROM cms_hospitals FINAL h
LEFT JOIN hcris_hospital_cost_reports FINAL hc ON h.facility_id = hc.ccn
WHERE hc.uncompensated_care_cost IS NOT NULL
ORDER BY uncomp_pct DESC LIMIT 20;
```

Hospitals with high uncompensated-care % are financially stressed → slower to adopt expensive new tech → deprioritize for early-launch device targeting.

---

## 🏥 Full Facility Universe

### 7. ASC count per state (Watchman-eligible facility universe)

```sql
SELECT state, count() AS asc_count
FROM pos_facilities FINAL
WHERE facility_type = 'Ambulatory Surgical Center'
  AND termination_date IS NULL
GROUP BY state
ORDER BY asc_count DESC;
```

For each device + setting, we can now address ~6× more facilities than the hospitals-only `cms_hospitals` table allowed.

### 8. ASC + hospital combined catchment for BSc in MA

```sql
SELECT facility_type,
       count() AS facility_count,
       countIf(termination_date IS NULL) AS active_count
FROM pos_facilities FINAL
WHERE state = 'MA'
GROUP BY facility_type
ORDER BY active_count DESC;
```

### 9. Find all ASCs near a specific zip code (drive-time catchment proxy)

```sql
SELECT facility_name, city, zip_code, bed_count
FROM pos_facilities FINAL
WHERE facility_type = 'Ambulatory Surgical Center'
  AND zip_code LIKE '021%'   -- Boston-area ZIPs
  AND termination_date IS NULL;
```

---

## 👤 Provider Eligibility & Filtering

### 10. Drop opted-out providers from any NPI list

```sql
WITH op_targets AS (
    SELECT DISTINCT physician_npi AS npi
    FROM cms_open_payments
    WHERE manufacturer_name LIKE '%Boston Scientific%'
),
opt_out AS (SELECT npi FROM medicare_opt_out FINAL)
SELECT t.npi
FROM op_targets t
WHERE t.npi NOT IN (SELECT npi FROM opt_out);
```

Manufacturers cannot bill Medicare through opted-out providers — removing them is a free targeting precision boost.

### 11. NPIs eligible to order DME (relevant for accessory/supplies)

```sql
SELECT count() AS dme_eligible_npis
FROM order_referring_npi FINAL
WHERE dme_eligible = 1;
```

### 12. Pull only billing-active NPIs from any list

```sql
WITH op_targets AS (
    SELECT DISTINCT physician_npi FROM cms_open_payments
    WHERE manufacturer_name LIKE '%Boston Scientific%'
)
SELECT op_targets.physician_npi,
       ffs.provider_type_desc,
       ffs.state_cd,
       ffs.org_name
FROM op_targets
INNER JOIN medicare_ffs_enrollment FINAL ffs
       ON op_targets.physician_npi = ffs.npi;
```

If an NPI isn't in `medicare_ffs_enrollment` they're not currently enrolled to bill Medicare FFS — useless as a targeting prospect.

---

## 🎯 Clinical-Category Targeting

### 13. All HCPCS in a clinical family (e.g., cardiology implants)

```sql
SELECT hcpcs_code, betos_description, rbcs_family
FROM betos_crosswalk FINAL
WHERE rbcs_category LIKE '%Cardio%'
   OR rbcs_family LIKE '%implant%'
ORDER BY hcpcs_code;
```

Cleanly filter HCPCS by clinical lens instead of guessing which codes are device-relevant from text descriptions.

### 14. Find every NPI in a clinical specialty

```sql
WITH cardio_specialties AS (
    SELECT medicare_specialty_code
    FROM npi_taxonomy_crosswalk FINAL
    WHERE provider_taxonomy_description LIKE '%Cardiology%'
       OR provider_taxonomy_description LIKE '%Cardiac Electrophysiology%'
)
SELECT count(DISTINCT ffs.npi)
FROM medicare_ffs_enrollment FINAL ffs
WHERE ffs.provider_type_cd IN (SELECT medicare_specialty_code FROM cardio_specialties);
```

---

## 🔗 Cross-Layer Power Queries

### 15. "Show me clean, enrolled, cardiology NPIs at MA ASCs"

```sql
WITH
  cardio_codes AS (
    SELECT medicare_specialty_code
    FROM npi_taxonomy_crosswalk FINAL
    WHERE provider_taxonomy_description ILIKE '%cardio%'
  ),
  cardio_enrolled AS (
    SELECT DISTINCT ffs.npi
    FROM medicare_ffs_enrollment FINAL ffs
    WHERE ffs.provider_type_cd IN (SELECT medicare_specialty_code FROM cardio_codes)
      AND ffs.state_cd = 'MA'
  ),
  excluded AS (SELECT npi FROM oig_leie_exclusions FINAL WHERE npi != ''),
  opted_out AS (SELECT npi FROM medicare_opt_out FINAL),
  ma_ascs AS (
    SELECT ccn FROM pos_facilities FINAL
    WHERE state = 'MA' AND facility_type = 'Ambulatory Surgical Center'
      AND termination_date IS NULL
  )
SELECT DISTINCT aff.npi
FROM npi_facility_affiliation FINAL aff
INNER JOIN cardio_enrolled e ON aff.npi = e.npi
INNER JOIN ma_ascs ON aff.facility_ccn = ma_ascs.ccn
WHERE aff.npi NOT IN (SELECT npi FROM excluded)
  AND aff.npi NOT IN (SELECT npi FROM opted_out);
```

This is the **whole point** of the new pipeline. One query → a vetted, targetable list of physicians for a Boston Scientific cardiology sales push, with compliance + eligibility + setting + specialty all filtered.

### 16. Full HCPCS profile — every dollar, every limit, every category

```sql
SELECT h.hcpcs_code, h.short_desc,
       opps.payment_rate                          AS hospital_$,
       mpfs.non_facility_payment                  AS office_$,
       mpfs.facility_payment                      AS facility_$,
       mpfs.work_rvu, mpfs.global_days,
       opps.status_indicator,
       betos.rbcs_category, betos.rbcs_family,
       stark.dhs_category                         AS stark_dhs
FROM hcpcs_master FINAL h
LEFT JOIN opps_addendum_b FINAL opps ON h.hcpcs_code = opps.hcpcs_code
LEFT JOIN mpfs_rates FINAL mpfs ON h.hcpcs_code = mpfs.hcpcs_code AND mpfs.modifier=''
LEFT JOIN betos_crosswalk FINAL betos ON h.hcpcs_code = betos.hcpcs_code
LEFT JOIN stark_dhs_codes FINAL stark ON h.hcpcs_code = stark.hcpcs_code
WHERE h.hcpcs_code IN ('33340','61885','C1820');
```

---

## What questions you can now answer

1. **Compliance:** "Has this NPI ever been excluded?"
2. **Reimbursement:** "What's the Medicare $ for this device procedure in an office vs. hospital?"
3. **Targeting:** "Which clean, billing-eligible, specialty-matched NPIs are at the kind of facility where this procedure happens?"
4. **Catchment:** "How many ASCs are in this state? How are their beds + capital spend trending?"
5. **Risk:** "Which hospitals are financially stressed (high uncompensated burden + low capital)?"
6. **Lifecycle:** "Which providers have an upcoming revalidation deadline that could disrupt their billing in 90 days?"

---

## Where to extend next

Things we **don't** have that would round out the picture:

- **NPPES NPI registry** (8 GB monthly file) — full 7M-NPI master directory; we have FFS enrollment (2.6M) + Order&Referring (2M) which covers most billing-active NPIs. NPPES adds the rest.
- **Hospital Price Transparency MRFs** — every hospital's chargemaster + negotiated rates. Massive (TBs total). High value but heavy-lift ingest.
- **MA Contract Enrollment by Plan/Contract** — supplements Monthly Enrollment with per-contract granularity. Live on cms.gov as monthly ZIPs.
- **Drug Pricing 340B** — only relevant for biologics / drug-eluting devices.
