# Developer Onboarding — How the Tables Connect

A 5-minute mental model. Read this before writing your first query.

---

## The 4 keys that connect everything

The warehouse looks complicated (11 tables) but only **4 columns** stitch them together. Memorize these.

| Key | What it represents | Example value |
|---|---|---|
| `hcpcs_code` | A **procedure / device / supply** that Medicare pays for | `33208`, `C9601`, `E0601` |
| `npi` | A **person or supplier** (an individual NPI) | `1366479404` (Dr. Eckart) |
| `facility_id` / `facility_ccn` / `ccn` | A **hospital** (Medicare's 6-char CCN) | `220110` (Brigham & Women's) |
| `nct_id` | A **clinical trial** on ClinicalTrials.gov | `NCT04957771` |

Everything else is a fact or dimension hanging off one of these 4 keys.

---

## Visual map

```
                  ┌──────────────────┐
                  │   hcpcs_master   │  ← what is this code?
                  └────────┬─────────┘
                           │ hcpcs_code
        ┌──────────────────┼──────────────────────┬────────────────────────┐
        │                  │                      │                        │
┌───────▼──────┐ ┌─────────▼────────┐ ┌───────────▼───────────┐  ┌─────────▼──────────────────┐
│opps_addendum_b│ │ stark_dhs_codes  │ │ cms_provider_summary  │  │ clinical_trial_interventions│
│how it's paid │ │ DHS designation  │ │ who bills it          │  │ which trials test it       │
└──────────────┘ └──────────────────┘ └───────┬───────┬───────┘  └───────┬────────────────────┘
                                              │       │                  │ nct_id
                                              │ npi   │ ccn              │
                                              │       │             ┌────▼─────────────┐
                                              │       │             │ clinical_trials  │
                                              │       │             └──────────────────┘
                                ┌─────────────▼──┐ ┌──▼────────────────┐
                                │ npi_facility_  │ │ cms_hospitals     │
                                │ affiliation    │ │ name/state/rating │
                                │ npi → ccn      │ └──┬────────────────┘
                                └─────────┬──────┘    │ facility_id
                                          │ ccn       │
                                          └───────────┤
                                                      │
                                          ┌───────────▼────────────┐
                                          │ cms_hospital_measures  │
                                          │ readmission/quality    │
                                          └────────────────────────┘

cms_open_payments — no FK; fuzzy match by product_name / manufacturer_name
```

---

## Decide which table to query, by question

| Your question | Tables you need |
|---|---|
| "What is HCPCS code X?" | `hcpcs_master` |
| "How does Medicare pay for X?" | `opps_addendum_b` |
| "Is X a Stark DHS code?" | `stark_dhs_codes` |
| "Who bills X?" | `cms_provider_summary` |
| "Which hospitals do X?" | `cms_provider_summary` + `npi_facility_affiliation` + `cms_hospitals` |
| "What's hospital Y's readmission rate?" | `cms_hospitals` + `cms_hospital_measures` |
| "What does hospital Y do most?" | `cms_provider_summary` filtered to dataset = inpatient/outpatient + CCN |
| "Which trials study X?" | `clinical_trial_interventions` + `clinical_trials` |
| "Which manufacturers pay doctors for X?" | `cms_open_payments` (fuzzy match on product name) |

Pattern: **start at the key, walk to the fact.**

---

## The one gotcha that bites everyone

`cms_provider_summary` has **11 different shapes stacked into one table.** Always filter by `dataset_id`:

```sql
-- Has HCPCS + NPI (good for HCPCS-centric queries):
WHERE dataset_id = 'medicare_physician_by_provider_service'   -- 8.4M rows
WHERE dataset_id = 'medicare_dmepos_by_supplier_service'      -- 5.5M rows

-- Has CCN + DRG (good for hospital-centric queries, NO HCPCS):
WHERE dataset_id = 'medicare_inpatient_by_provider_service'   -- 146K rows

-- Has CCN + APC in raw JSON (NO HCPCS column):
WHERE dataset_id = 'medicare_outpatient_by_provider_service'  -- 117K rows
```

If you forget the filter, you'll join physicians to hospitals to inpatient stays and the row counts will multiply weirdly.

---

## ClickHouse-specific quirks

1. **`FINAL` modifier** — these tables deduplicate at query time:
   `hcpcs_master`, `cms_hospitals`, `cms_hospital_measures`, `clinical_trials`, `clinical_trial_interventions`, `npi_facility_affiliation`. Always add `FINAL`.

2. **Don't write `FROM x FINAL AS alias` in joins** — ClickHouse rejects it.
   Either swap the order (`FROM x AS alias FINAL`) or wrap in a CTE (the cleaner pattern used everywhere in `ui/app.py`):
   ```sql
   WITH hosp AS (SELECT * FROM cms_hospitals FINAL)
   SELECT ... FROM ... LEFT JOIN hosp ON ...
   ```

3. **CCN format is 6 chars with leading zeros** — `050441`, not `50441`. Stored as String, so type-cast matters.

4. **No real HCPCS column on outpatient/inpatient PUFs** — they're keyed by APC/DRG. APC code lives in `cms_provider_summary.raw['APC_Cd']` for outpatient; bridge to HCPCS via `opps_addendum_b.apc_code`.

5. **Open Payments has no HCPCS link** — only fuzzy product-name matching is possible.

---

## How to discover a table's schema yourself

```sql
DESCRIBE TABLE hospital_us_cms_data.cms_hospital_measures;
SHOW CREATE TABLE hospital_us_cms_data.cms_provider_summary;

-- See what's actually in there:
SELECT * FROM cms_provider_summary LIMIT 3 FORMAT Vertical;

-- Count distinct values of a column:
SELECT dataset_id, count() FROM cms_provider_summary GROUP BY dataset_id ORDER BY 2 DESC;

-- Sample of HCPCS codes:
SELECT hcpcs_code, short_desc FROM hcpcs_master FINAL WHERE is_device = 1 LIMIT 10;
```

---

## Connection string

```
host:     warehouse.dev.rp360.io
port:     443 (HTTPS)
user:     (from .env CLICKHOUSE_USER)
password: (from .env CLICKHOUSE_PASSWORD)
database: hospital_us_cms_data
```

**Python** (already wired):
```python
from src import db
client = db._raw_client()
rows = client.query("SELECT count() FROM hcpcs_master FINAL").result_rows
```

**clickhouse-client CLI:**
```bash
clickhouse-client --host warehouse.dev.rp360.io --port 443 --secure \
                  --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" \
                  --database hospital_us_cms_data \
                  --query "DESCRIBE TABLE cms_hospital_measures"
```

**Any BI tool** (Metabase, Superset, Hex, dbt, DBeaver): use the ClickHouse driver, point at the host above, schema `hospital_us_cms_data`. All 11 tables show up.

---

## Three reference docs in this repo

| File | Purpose |
|---|---|
| `DEV_ONBOARDING.md` (this file) | 5-min mental model |
| `DATA_GUIDE.md` | Full schema reference + 15 copy-paste queries |
| `QUERY_TUTORIAL.md` | Step-by-step build-up from one lookup to a 9-table join |

**Recommended reading order:** this file → `QUERY_TUTORIAL.md` → `DATA_GUIDE.md` as needed.

---

## Smoke test (run this first, after connecting)

```sql
SELECT 'tables alive' AS check,
       (SELECT count() FROM hcpcs_master FINAL)             AS hcpcs_codes,
       (SELECT count() FROM cms_hospitals FINAL)            AS hospitals,
       (SELECT count() FROM cms_provider_summary)           AS billing_rows,
       (SELECT count() FROM npi_facility_affiliation FINAL) AS npi_to_ccn,
       (SELECT count() FROM clinical_trials FINAL)          AS trials,
       (SELECT count() FROM opps_addendum_b)                AS opps_rows;
```

Should return (approximate, as of last refresh):
- `hcpcs_codes` ≈ **9,068** (the post-`FINAL`-dedup count)
- `hospitals` ≈ **5,426**
- `billing_rows` ≈ **15,982,726** (no FINAL — this table is MergeTree)
- `npi_to_ccn` ≈ **1,638,956**
- `trials` ≈ **23,451**
- `opps_rows` ≈ **56,161**

If any are 0 or the query fails, your `.env` credentials are wrong or you're on the wrong database.

> **Note:** without `FINAL`, count() returns the raw pre-dedup row count (e.g. `SELECT count() FROM hcpcs_master` returns ~27K — older versions of each code that haven't been background-merged away yet). Use `FINAL` to get the logical count of unique entities.
