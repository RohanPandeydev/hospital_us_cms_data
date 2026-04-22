# CMS Hospital Data Warehouse (local)

Ingests CMS Provider Data hospital datasets into a local Postgres database.

See **[PLAN.md](PLAN.md)** for the full roadmap (Phases 1–4, device linkage).

## Setup (one time)

```bash
cd hospital_us_cms_data
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# optional: edit .env if your Postgres creds differ
cp .env.example .env
```

## Run everything (one command)

```bash
python run.py ingest     # auto-creates DB + tables, fetches every dataset
python run.py status     # run history
```

Re-running is safe and idempotent — already-ingested rows upsert with no duplicates.

## Other commands

```bash
python run.py list                                # show available datasets
python run.py ingest --dataset xubh-q36u          # ingest just one
python run.py ingest --dataset xubh-q36u --limit 50   # smoke test
```

## Datasets

| ID | Kind | Name |
|----|------|------|
| xubh-q36u | hospital_master | Hospital General Information (master, ~5,400 rows) |
| ynj2-r877 | facility_measure | Complications and Deaths - Hospital (PSI) |
| 632h-zaca | facility_measure | Unplanned Hospital Visits |
| 9n3s-kdb3 | facility_measure | HRRP Readmissions |
| 77hc-ibv8 | facility_measure | Healthcare Associated Infections (HAI / SIR) |
| muwa-iene | facility_measure | PSI-90 Composite |
| dgck-syfz | facility_measure | HCAHPS Patient Survey |
| bs2r-24vh | state_measure | Complications and Deaths - State |
| qqw3-t4ie | national_measure | Complications and Deaths - National |

Add/remove datasets in `src/datasets.py`.

## Schema

- `cms_hospitals` — master facility table (PK: `facility_id`)
- `cms_hospital_measures` — unified per-facility measures (PK: dataset_id + facility_id + measure_id)
- `cms_state_measures` — state benchmarks
- `cms_national_measures` — national benchmarks
- `cms_ingestion_log` — per-run audit trail

Full raw API row is preserved in a `JSONB` column on every table, so you can always recover any field that wasn't lifted to a typed column.
