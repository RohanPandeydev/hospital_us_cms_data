#!/usr/bin/env bash
# Run the remaining 6 ingests + Groq mapper.
# Designed to be launched in the background; appends to logs/finish-*.log.
set -uo pipefail
cd "$(dirname "$0")/.."

source .venv/bin/activate 2>/dev/null || true
PY="${PYTHON:-python}"

LOG="logs/finish-$(date +%Y%m%d-%H%M%S).log"
mkdir -p logs
exec > >(tee -a "$LOG") 2>&1

hdr() { printf "\n▸ %s\n" "$*"; }

# Only the 6 datasets that are still empty in cms_provider_summary.
# The other 5 (dmepos_by_supplier_service, physician_by_provider_service,
# provider_of_services, betos_classification, hospital_cost_report) already
# have data and re-ingesting them would duplicate rows on plain MergeTree.
REMAINING=(
  medicare_inpatient_by_provider
  medicare_inpatient_by_provider_service
  medicare_outpatient_by_provider_service
  medicare_dmepos_by_supplier
  medicare_dmepos_by_referring_provider
  medicare_physician_by_provider
)

hdr "$(date +%H:%M:%S)  ingest phase — ${#REMAINING[@]} datasets"
for ds in "${REMAINING[@]}"; do
  csv="downloads/cms_api/${ds}.csv"
  if [ ! -f "$csv" ]; then
    echo "  ✗ $ds: missing $csv — skipping"
    continue
  fi
  hdr "$(date +%H:%M:%S)  ingest --dataset $ds  ($(ls -lh "$csv" | awk '{print $5}'))"
  "$PY" run.py ingest --dataset "$ds" --csv-path "$csv" \
    || echo "  ! $ds failed (continuing)"
done

hdr "$(date +%H:%M:%S)  Groq trial→HCPCS mapper"
"$PY" run.py map-trials-to-hcpcs --batch-size 10 \
  || echo "  ! mapper failed"

hdr "$(date +%H:%M:%S)  DONE — log: $LOG"

# Final state snapshot
"$PY" - <<'EOF'
from src import db
with db.connect() as c:
    print('\n=== final state ===')
    for ds in ['medicare_inpatient_by_provider','medicare_inpatient_by_provider_service',
                'medicare_outpatient_by_provider_service','medicare_dmepos_by_supplier',
                'medicare_dmepos_by_referring_provider','medicare_physician_by_provider']:
        n = c.query(f"SELECT count() FROM cms_provider_summary WHERE dataset_id='{ds}'").result_rows[0][0]
        mark = '✓' if n > 0 else '✗'
        print(f'  {mark} {ds:<42} {n:>12,}')
    n_mapped = c.query("SELECT count() FROM clinical_trial_interventions WHERE hcpcs_code IS NOT NULL").result_rows[0][0]
    n_total = c.query("SELECT count() FROM clinical_trial_interventions").result_rows[0][0]
    print(f'\n  trials mapped to HCPCS: {n_mapped:,}/{n_total:,}')
EOF
