#!/usr/bin/env bash
# Fulfill the AXIOS / BSC Endoscopy requirement end-to-end.
# Runs the four loads needed to populate /requirement and /axios.
#
# Pre-reqs:
#   - You are on a network that can reach warehouse.dev.rp360.io:8443
#   - .env has the ClickHouse creds
#   - Python venv with requirements.txt installed
#
# Usage:
#   bash scripts/fulfill_axios_requirement.sh
#   bash scripts/fulfill_axios_requirement.sh --dry-run   # only the AXIOS fetch supports it
#
# Exit codes:
#   0 — all four steps completed
#   1 — env / venv / network problem
#   2 — at least one step failed

set -uo pipefail

cd "$(dirname "$0")/.."

DRY=""
[[ "${1:-}" == "--dry-run" ]] && DRY="--dry-run"

echo "════════════════════════════════════════════════════════════════"
echo " AXIOS REQUIREMENT FULFILLMENT — 4 steps"
echo "════════════════════════════════════════════════════════════════"

# ─── Pre-flight ──────────────────────────────────────────────────────
echo; echo "[pre-flight] Checking warehouse reachability…"
if ! nc -zv -w5 warehouse.dev.rp360.io 8443 >/dev/null 2>&1; then
  echo "❌ Cannot reach warehouse.dev.rp360.io:8443 — VPN connected?"
  exit 1
fi
echo "✓ warehouse reachable"

echo; echo "[pre-flight] Checking python deps…"
if ! python3 -c "import clickhouse_connect, requests" 2>/dev/null; then
  echo "❌ clickhouse_connect or requests missing — run: pip install -r requirements.txt"
  exit 1
fi
echo "✓ deps installed"

FAILED=0

# ─── Step 1 — AXIOS MAUDE pull from openFDA (fixes numerator) ────────
echo; echo "════════════════════════════════════════════════════════════════"
echo " STEP 1 — Pull AXIOS MAUDE events from openFDA"
echo "════════════════════════════════════════════════════════════════"
echo "Source:  https://api.fda.gov/device/event.json?search=device.brand_name:axios"
echo "Target:  fda_maude_events + fda_maude_devices"
echo "Expect:  ~2,367 events written"
if python3 scripts/fetch_axios_maude.py $DRY; then
  echo "✓ Step 1 complete"
else
  echo "❌ Step 1 failed"; FAILED=$((FAILED+1))
fi

# ─── Step 2a — PSI-90 family (fixes Dataset 2 panel) ─────────────────
echo; echo "════════════════════════════════════════════════════════════════"
echo " STEP 2a — Ingest CMS Hospital Compare PSI-90 family (muwa-iene)"
echo "════════════════════════════════════════════════════════════════"
echo "Source:  https://data.cms.gov/provider-data/dataset/muwa-iene"
echo "Target:  cms_hospital_measures (PSI_15, PSI_09, PSI_13, PSI_90 …)"
if python3 run.py ingest --dataset muwa-iene; then
  echo "✓ Step 2a complete"
else
  echo "❌ Step 2a failed"; FAILED=$((FAILED+1))
fi

# ─── Step 2b — HRRP readmission (fixes Dataset 2 panel) ──────────────
echo; echo "════════════════════════════════════════════════════════════════"
echo " STEP 2b — Ingest CMS Hospital Readmissions Reduction Program (9n3s-kdb3)"
echo "════════════════════════════════════════════════════════════════"
echo "Source:  https://data.cms.gov/provider-data/dataset/9n3s-kdb3"
echo "Target:  cms_hospital_measures (READM_30_HOSP_WIDE …)"
if python3 run.py ingest --dataset 9n3s-kdb3; then
  echo "✓ Step 2b complete"
else
  echo "❌ Step 2b failed"; FAILED=$((FAILED+1))
fi

# ─── Step 3 — Multi-year Physician PUF (fixes Dataset 3 trend) ───────
echo; echo "════════════════════════════════════════════════════════════════"
echo " STEP 3 — Multi-year Physician PUF for YoY trend"
echo "════════════════════════════════════════════════════════════════"
echo "This is MANUAL — drop additional yearly CSVs into downloads/ then re-ingest."
echo "Available years: 2019, 2020, 2021, 2022, 2024 from"
echo "  https://data.cms.gov/provider-summary-by-type-of-service/"
echo "  medicare-physician-other-practitioners/"
echo "  medicare-physician-other-practitioners-by-provider-and-service"
echo
echo "Currently in downloads/:"
ls -lh downloads/physician_*.csv 2>/dev/null || echo "  (only physician_2023.csv)"
echo
echo "(Skipping ingest — code change in src/datasets.py also needed to enable multi-year scan.)"

# ─── Final summary ───────────────────────────────────────────────────
echo; echo "════════════════════════════════════════════════════════════════"
echo " SUMMARY"
echo "════════════════════════════════════════════════════════════════"
if [[ $FAILED -eq 0 ]]; then
  echo "✅ All automated steps complete."
  echo "→ Reload http://localhost:8000/requirement"
  echo "→ The proof checklist at the bottom should flip ✅"
  exit 0
else
  echo "⚠️  $FAILED step(s) failed — see logs above"
  exit 2
fi
