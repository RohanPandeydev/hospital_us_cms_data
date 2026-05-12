# Query Tutorial — Building From One Lookup to a Full Cross-Source Join

A step-by-step walkthrough that builds one SQL query at a time, ending with a fully-joined HCPCS → hospital → quality dashboard. Every query is verified against the live warehouse.

> **Running example:** `33208` — single-chamber pacemaker insertion (a real CPT code with rich data across all tables).

---

## Step 1 — Look up one code

**Question:** What is `C1721`?
**Table:** `hcpcs_master` (Medicare code dictionary)

```sql
SELECT hcpcs_code, short_desc, long_desc, is_device, code_family, betos_code
FROM hcpcs_master FINAL
WHERE hcpcs_code = 'C1721';
```

**Output:**
```
('C1721', 'Aicd, dual chamber', 'Cardioverter-defibrillator, dual chamber (implantable)', 1, 'C', 'D1A')
```

**Lesson:** `hcpcs_master` is the start of every HCPCS query.
Use `FINAL` to deduplicate rows because the table is `ReplacingMergeTree`.

---

## Step 2 — How does Medicare pay for it?

**Table:** `opps_addendum_b`. Join key: `hcpcs_code`.

```sql
SELECT effective_quarter, status_indicator, apc_code, payment_rate
FROM opps_addendum_b
WHERE hcpcs_code = 'C1721'
ORDER BY effective_quarter DESC;
```

**Output:**
```
2026Q1  SI=N  apc=None  pay=None
2025Q2  SI=N  apc=None  pay=None
2025Q1  SI=N  apc=None  pay=None
```

**Lesson:** the Status Indicator (SI) tells the whole story:
- `J1`/`J2`/`T`/`S` — separately paid (has APC + payment rate)
- `N` — packaged into another code's payment
- `Y` — paid through DME (suppliers, not hospitals, bill it)
- `E1` — not covered by Medicare

---

## Step 3 — Who actually bills it?

`C1721` is SI=N (no one bills it standalone). Switch to **`33208`** (the actual surgical procedure).

**Table:** `cms_provider_summary`.
**Important:** this table has 11 different `dataset_id` slices. Filter to a specific one.

```sql
SELECT provider_name, provider_state, npi, sum(total_services) AS total_services
FROM cms_provider_summary
WHERE hcpcs_code = '33208'
  AND provider_name != ''
  AND dataset_id = 'medicare_physician_by_provider_service'
GROUP BY provider_name, provider_state, npi
ORDER BY total_services DESC NULLS LAST
LIMIT 5;
```

**Output:**
```
Wichita Asc Lp     KS  NPI=1295119584  svcs=402
Eckart             FL  NPI=1366479404  svcs=332
Kinder             IL  NPI=1346204518  svcs=324
Brammer            VA  NPI=1083822522  svcs=307
Pima Heart Asc Llc AZ  NPI=1184078503  svcs=301
```

**Lesson:** filter by `dataset_id`. Two HCPCS-rich slices:
- `medicare_physician_by_provider_service` (8.4M rows — doctors)
- `medicare_dmepos_by_supplier_service` (5.5M rows — equipment suppliers)

---

## Step 4 — Bridge NPI → hospital CCN

**Table:** `npi_facility_affiliation` (1.6M rows linking physicians to hospitals).

```sql
SELECT npi, last_name, first_name, facility_ccn, facility_type
FROM npi_facility_affiliation FINAL
WHERE npi = '1366479404';
```

**Output:**
```
('1366479404', 'ECKART', 'ROBERT', '100087', 'Hospital')
('1366479404', 'ECKART', 'ROBERT', '100267', 'Hospital')
('1366479404', 'ECKART', 'ROBERT', '100359', 'Hospital')
```

**Lesson:** one physician can be affiliated with multiple hospitals. When ranking hospitals, his services count for all 3.

---

## Step 5 — Combine: which hospitals get the most pacemakers?

Wrap the FINAL tables in CTEs so ClickHouse parses the joins cleanly.

```sql
WITH
  billed AS (
    SELECT npi, sum(total_services) AS svcs
    FROM cms_provider_summary
    WHERE hcpcs_code = '33208'
      AND dataset_id = 'medicare_physician_by_provider_service'
      AND npi != ''
    GROUP BY npi
  ),
  aff  AS (SELECT npi, facility_ccn FROM (SELECT * FROM npi_facility_affiliation FINAL)),
  hosp AS (SELECT facility_id, facility_name, state, overall_rating FROM cms_hospitals FINAL)
SELECT aff.facility_ccn                     AS ccn,
       any(hosp.facility_name)              AS hospital,
       any(hosp.state)                      AS state,
       any(hosp.overall_rating)             AS stars,
       countDistinct(billed.npi)            AS doctors,
       sum(billed.svcs)                     AS pacemakers
FROM billed
INNER JOIN aff  USING npi
LEFT  JOIN hosp ON aff.facility_ccn = hosp.facility_id
GROUP BY ccn
ORDER BY pacemakers DESC NULLS LAST
LIMIT 5;
```

**Output:**
```
CCN 670025  BAYLOR SCOTT & WHITE THE HEART HOSPITAL    TX  ★5  25 MDs  1391 pacemakers
CCN 220110  BRIGHAM AND WOMEN'S HOSPITAL               MA  ★5  22 MDs  1267 pacemakers
CCN 100087  SARASOTA MEMORIAL HOSPITAL                 FL  ★5  18 MDs  1233 pacemakers
CCN 490009  UNIVERSITY OF VIRGINIA MEDICAL CENTER      VA  ★4  12 MDs   982 pacemakers
CCN 220071  MASSACHUSETTS GENERAL HOSPITAL             MA  ★5  23 MDs   978 pacemakers
```

**Lesson — the FINAL/alias trap:**
ClickHouse complains about `FROM cms_hospitals FINAL AS h` in joins. Two fixes:
- **Move FINAL after the alias:** `FROM cms_hospitals AS h FINAL`
- **Or wrap in a CTE:** `WITH hosp AS (SELECT * FROM cms_hospitals FINAL)` and join `hosp` (cleaner for complex queries — recommended).

---

## Step 6 — Add hospital quality (readmission ratio)

Layer in `cms_hospital_measures`.

```sql
WITH
  billed AS (
    SELECT npi, sum(total_services) AS svcs
    FROM cms_provider_summary
    WHERE hcpcs_code = '33208'
      AND dataset_id = 'medicare_physician_by_provider_service'
    GROUP BY npi
  ),
  aff   AS (SELECT npi, facility_ccn FROM (SELECT * FROM npi_facility_affiliation FINAL)),
  hosp  AS (SELECT facility_id, facility_name, state, overall_rating
            FROM cms_hospitals FINAL),
  readm AS (
    SELECT facility_id, score_num AS hf_readmit_ratio
    FROM cms_hospital_measures FINAL
    WHERE measure_id = 'READM-30-HF-HRRP'        -- Heart-failure readmission ERR
  )
SELECT aff.facility_ccn                AS ccn,
       any(hosp.facility_name)         AS hospital,
       any(hosp.state)                 AS state,
       any(hosp.overall_rating)        AS stars,
       sum(billed.svcs)                AS pacemakers,
       any(readm.hf_readmit_ratio)     AS hf_readmit_ratio
FROM billed
INNER JOIN aff   USING npi
LEFT  JOIN hosp  ON aff.facility_ccn = hosp.facility_id
LEFT  JOIN readm ON aff.facility_ccn = readm.facility_id
GROUP BY ccn
ORDER BY pacemakers DESC NULLS LAST
LIMIT 5;
```

**Output:**
```
Brigham and Women's   MA  ★5  1267 pacemakers   HF readmit ratio 0.876  (better than expected)
Sarasota Memorial     FL  ★5  1233 pacemakers   HF readmit ratio 0.986  (about average)
Mass General          MA  ★5   978 pacemakers   HF readmit ratio 0.915  (better than expected)
```

**Lesson — readmission ratios:**
- `< 1.0` = readmits fewer patients than CMS predicts (good)
- `> 1.0` = readmits more than predicted → Medicare penalty
- `= 1.0` = exactly the expected rate

---

## Step 7 — Pull in clinical trials linked to the same code

```sql
WITH iv AS (SELECT * FROM clinical_trial_interventions FINAL),
     t  AS (SELECT * FROM clinical_trials FINAL)
SELECT t.nct_id, t.brief_title, t.lead_sponsor, t.overall_status, t.start_date
FROM iv
INNER JOIN t ON iv.nct_id = t.nct_id
WHERE iv.hcpcs_code = '33208'
ORDER BY t.start_date DESC NULLS LAST
LIMIT 5;
```

**Lesson:** trial→HCPCS mapping was done by Groq AI and only covers device/procedure interventions where it found a candidate match. Most rows in `clinical_trial_interventions` have `hcpcs_code IS NULL`.

---

## Step 8 — Pull in Open Payments (fuzzy match)

Open Payments has **no HCPCS column**. Match on product/manufacturer keywords.

```sql
SELECT manufacturer_name, product_name,
       count() AS payments,
       round(sum(payment_total), 0) AS total_usd
FROM cms_open_payments
WHERE lowerUTF8(product_name) LIKE '%pacemaker%'
GROUP BY manufacturer_name, product_name
ORDER BY total_usd DESC NULLS LAST
LIMIT 5;
```

**Lesson:** label fuzzy matches in your output. Two unrelated products can share a keyword.

---

## Step 9 — Final mega-query: everything in one shot

```sql
WITH
  the_code AS (SELECT '33208' AS c),
  billed AS (
    SELECT npi, sum(total_services) AS svcs, sum(total_payment_amt) AS paid
    FROM cms_provider_summary, the_code
    WHERE hcpcs_code = the_code.c
      AND dataset_id = 'medicare_physician_by_provider_service'
      AND npi != ''
    GROUP BY npi
  ),
  aff   AS (SELECT npi, facility_ccn FROM (SELECT * FROM npi_facility_affiliation FINAL)),
  hosp  AS (SELECT facility_id, facility_name, state, overall_rating FROM cms_hospitals FINAL),
  readm AS (
    SELECT facility_id, score_num AS hf
    FROM cms_hospital_measures FINAL
    WHERE measure_id = 'READM-30-HF-HRRP'
  ),
  opps_info AS (
    SELECT hcpcs_code, status_indicator, apc_code, payment_rate, effective_quarter
    FROM opps_addendum_b
    WHERE effective_quarter = '2026Q1'
  )
SELECT
  (SELECT any(short_desc) FROM hcpcs_master FINAL
                          WHERE hcpcs_code = (SELECT c FROM the_code)) AS code_desc,
  (SELECT any(status_indicator) FROM opps_info
                                WHERE hcpcs_code = (SELECT c FROM the_code)) AS opps_si,
  aff.facility_ccn AS ccn,
  any(hosp.facility_name) AS hospital,
  any(hosp.state)         AS state,
  any(hosp.overall_rating) AS stars,
  countDistinct(billed.npi) AS doctors,
  sum(billed.svcs)         AS services,
  any(readm.hf)            AS hf_readmit_ratio
FROM billed
INNER JOIN aff   USING npi
LEFT  JOIN hosp  ON aff.facility_ccn = hosp.facility_id
LEFT  JOIN readm ON aff.facility_ccn = readm.facility_id
GROUP BY aff.facility_ccn
ORDER BY services DESC NULLS LAST
LIMIT 10;
```

This single query answers:
1. What is HCPCS X? (code_desc)
2. How does Medicare pay for it? (opps_si)
3. Which hospitals do it most? (ranked by services)
4. How are they rated? (stars, hf_readmit_ratio)

---

## Cheat sheet

**Always remember:**

| Rule | Why |
|---|---|
| Add `FINAL` on ReplacingMergeTree tables | Deduplicates older versions of rows |
| `cms_provider_summary` is plain MergeTree — no FINAL needed, but **always filter `dataset_id`** | Otherwise you mix physician + DMEPOS + inpatient + outpatient slices |
| Wrap FINAL tables in CTEs before joining | Avoids `FROM x FINAL AS alias` syntax errors |
| Inpatient/outpatient PUFs have **no HCPCS column** — keyed by DRG/APC | Bridge via `opps_addendum_b.apc_code` to `raw['APC_Cd']` |
| Open Payments has no HCPCS link — use product/manufacturer fuzzy match | Label results as approximate |
| CCN format: 6 chars, leading zeros matter | `050441` ≠ `50441` |

**Common measure IDs:**

| Pattern | Meaning |
|---|---|
| `READM_30_*` | Raw 30-day readmission rate (%) |
| `READM-30-*-HRRP` | Excess Readmission Ratio (>1.0 = Medicare penalty) |
| `MORT_30_*` | 30-day mortality rate |
| `PSI_*` | Patient safety indicators |
| `HAI_*_SIR` | Infection ratio (>1.0 = worse than expected) |
| `EDAC_30_*` | Excess days in acute care |
| `Hybrid_HWR` | Hospital-wide readmission |

Run any of these queries with:
```python
from src import db
rows = db._raw_client().query("...your SQL...").result_rows
for r in rows: print(r)
```

Or use `clickhouse-client` from the command line — see `DATA_GUIDE.md § 5` for connection details.
