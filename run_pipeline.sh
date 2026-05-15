#!/usr/bin/env bash
# Full HCPCS-centric ingest pipeline. Runs every source group in order:
#   1. Apply schema
#   2. CMS Provider Data        (quality, complications, readmissions, HAI,
#                                HCAHPS, PSI, HRRP, HCRIS, BETOS, POS — all
#                                kinds=facility/state/national/wide_facility)
#   3. Open Payments 2022-2024  (manufacturer → physician/hospital payments)
#   4. FDA MAUDE + GUDID        (device adverse events + UDI master)
#   5. HCPCS master + Stark DHS + ClinicalTrials.gov
#   6. CMS data-api datasets    (Inpatient/Outpatient/DMEPOS/Physician PUF)
#      — data-api is currently Akamai-blocked from this network. We use
#      TinyFish to resolve the bulk CSV URL from each landing page, stream
#      the CSV to disk, and then ingest via --csv-path.
#   7. Groq mapper              (trial intervention text → HCPCS code)
#
# Every ingest writes to ClickHouse via ReplacingMergeTree, so re-running
# this script after a partial failure is safe — rows replay rather than
# duplicate.
#
# Usage:
#   ./run_pipeline.sh                       # full run
#   ./run_pipeline.sh --skip-tinyfish       # skip data-api CSV phase
#   ./run_pipeline.sh --skip-groq           # skip trial→HCPCS mapping
#   ./run_pipeline.sh --only stark,trials   # only run named phases
#   LIMIT=500 ./run_pipeline.sh             # smoke test — caps every ingest
#
# Flags:
#   --skip-init        don't re-apply schema (faster on repeat runs)
#   --skip-provider    skip the CMS provider-data measures phase
#   --skip-op          skip Open Payments
#   --skip-fda         skip MAUDE + GUDID
#   --skip-stark       skip Stark DHS
#   --skip-trials      skip ClinicalTrials.gov
#   --skip-tinyfish    skip CMS data-api CSV resolution + ingest
#   --skip-groq        skip Groq trial→HCPCS mapping
#   --skip-new         skip Phase 8 (12 new sources — see NEW_SOURCES.md)
#   --only <list>      run only listed phases (comma-separated). Valid:
#                      provider,op,fda,stark,trials,hcpcs,tinyfish,groq,new
#
# Env vars:
#   LIMIT              per-dataset row cap (default: unlimited)
#   TF_DOWNLOAD        1=download CSVs after TinyFish discovery (default 1)
#   TRIAL_LIMIT_PER_BUCKET   cap per ClinicalTrials bucket (default: unlimited)
#   GROQ_BATCH_SIZE    items per Groq call in trial→HCPCS mapper (default 10)

set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

# --- venv ---
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
PY="${PYTHON:-python}"

# --- defaults ---
SKIP_INIT=0
SKIP_PROVIDER=0
SKIP_OP=0
SKIP_FDA=0
SKIP_STARK=0
SKIP_TRIALS=0
SKIP_TINYFISH=0
SKIP_GROQ=0
SKIP_NEW=0
ONLY=""
LIMIT="${LIMIT:-}"                            # cap rows per dataset
TF_DOWNLOAD="${TF_DOWNLOAD:-1}"               # 1 = also download the CSV
TRIAL_LIMIT_PER_BUCKET="${TRIAL_LIMIT_PER_BUCKET:-}"
GROQ_BATCH_SIZE="${GROQ_BATCH_SIZE:-10}"

# Manual arg loop so we can consume the next token after `--only`
# (the old `for a in "$@"` form couldn't shift). Supports both
# `--only=tinyfish` (equals form) and `--only tinyfish` (space form).
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-init)      SKIP_INIT=1 ;;
    --skip-provider)  SKIP_PROVIDER=1 ;;
    --skip-op)        SKIP_OP=1 ;;
    --skip-fda)       SKIP_FDA=1 ;;
    --skip-stark)     SKIP_STARK=1 ;;
    --skip-trials)    SKIP_TRIALS=1 ;;
    --skip-tinyfish)  SKIP_TINYFISH=1 ;;
    --skip-groq)      SKIP_GROQ=1 ;;
    --skip-new)       SKIP_NEW=1 ;;
    --only=*)         ONLY="${1#--only=}" ;;
    --only)
      if [ $# -lt 2 ]; then
        echo "ERROR: --only requires a value (e.g. --only tinyfish)"; exit 2
      fi
      ONLY="$2"; shift
      ;;
    -h|--help)        sed -n '2,52p' "$0"; exit 0 ;;
    *) echo "WARN: ignoring unknown arg: $1" >&2 ;;
  esac
  shift
done

# If --only is set, default everything to skipped and selectively re-enable.
in_only() { [[ ",$ONLY," == *",$1,"* ]]; }
if [ -n "$ONLY" ]; then
  SKIP_PROVIDER=1; SKIP_OP=1; SKIP_FDA=1; SKIP_STARK=1
  SKIP_TRIALS=1; SKIP_TINYFISH=1; SKIP_GROQ=1; SKIP_NEW=1
  in_only provider  && SKIP_PROVIDER=0 || true
  in_only op        && SKIP_OP=0 || true
  in_only fda       && SKIP_FDA=0 || true
  in_only stark     && SKIP_STARK=0 || true
  in_only trials    && SKIP_TRIALS=0 || true
  in_only tinyfish  && SKIP_TINYFISH=0 || true
  in_only groq      && SKIP_GROQ=0 || true
  in_only hcpcs     && SKIP_HCPCS=0 || true
  in_only new       && SKIP_NEW=0 || true
fi

# --- logging ---
mkdir -p logs
LOG_FILE="logs/pipeline-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

BLUE='\033[1;34m'; GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RED='\033[1;31m'; RESET='\033[0m'
hdr()  { printf "\n${BLUE}▸ %s${RESET}\n" "$*"; }
ok()   { printf "${GREEN}  ✓ %s${RESET}\n" "$*"; }
warn() { printf "${YELLOW}  ! %s${RESET}\n" "$*"; }
fail() { printf "${RED}  ✗ %s${RESET}\n" "$*"; }

# Common --limit flag, optional.
LIMIT_ARGS=()
if [ -n "$LIMIT" ]; then
  LIMIT_ARGS=(--limit "$LIMIT")
fi

hdr "run_pipeline.sh — log: ${LOG_FILE}"
[ -n "$LIMIT" ]   && warn "row cap: per-dataset --limit ${LIMIT}"
[ -n "$ONLY" ]    && warn "phase filter: only=${ONLY}"

# ============================================================
# 1. Schema (idempotent — CREATE TABLE IF NOT EXISTS)
# ============================================================
if [ "$SKIP_INIT" -eq 0 ]; then
  hdr "1/7  Applying ClickHouse schema (init)"
  "$PY" run.py init
  ok "schema applied"
fi

# ============================================================
# 2. CMS Provider Data — quality, complications, readmissions, HAI,
#    HCAHPS, PSI, HRRP, HACRP, MSPB, VBP, Maternal, HCRIS, POS, BETOS,
#    Footnote crosswalk, Wide-facility snapshots.
#    `python run.py ingest` (no --dataset) walks every DATASET in registry.
# ============================================================
if [ "$SKIP_PROVIDER" -eq 0 ]; then
  hdr "2/7  Ingesting CMS provider-data measures + HCRIS + POS + BETOS"
  # The full registry hits all provider-data, FDA, Open Payments, HCPCS,
  # Stark, and Trials datasets. We split into phases below so failures in
  # one phase don't block the rest; here we run ONLY the provider-data
  # measure family.
  PROVIDER_DATASETS=(
    xubh-q36u ynj2-r877 632h-zaca 9n3s-kdb3 77hc-ibv8 muwa-iene dgck-syfz
    bs2r-24vh qqw3-t4ie tqkv-mgxq 4jcv-atw7 48nr-hqxx y9us-9xdf
    yv7e-xc69 apyc-v239 isrn-hqyy yq43-i98g rrqw-56er rs6n-9qwg 3n5g-6b7f
    z8ax-x9j1 ypbt-wvdk nrdb-3fcy k2ze-bqvw yd3s-jyhd 84jm-wiui 99ue-w85f
  )
  for ds in "${PROVIDER_DATASETS[@]}"; do
    hdr "  ingest --dataset $ds"
    "$PY" run.py ingest --dataset "$ds" ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} || warn "  $ds failed (continuing)"
  done
  ok "provider-data phase complete"
fi

# ============================================================
# 3. Open Payments 2022-2024 (general/research/ownership)
# ============================================================
if [ "$SKIP_OP" -eq 0 ]; then
  hdr "3/7  Ingesting Open Payments 2022-2024"
  for ds in op_2022_general op_2023_general op_2024_general op_2024_research op_2024_ownership; do
    hdr "  ingest --dataset $ds"
    "$PY" run.py ingest --dataset "$ds" ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} || warn "  $ds failed (continuing)"
  done
  ok "open-payments phase complete"
fi

# ============================================================
# 4. FDA MAUDE + GUDID — DISABLED by default. Re-enable by passing
#    --include-fda. The datasets are also commented out in
#    src/datasets.py so they don't appear in `python run.py ingest`.
# ============================================================
if [ "$SKIP_FDA" -eq 0 ] && [ "${INCLUDE_FDA:-0}" = "1" ]; then
  hdr "4/7  Ingesting FDA MAUDE + GUDID  (INCLUDE_FDA=1)"
  for ds in fda_maude_events fda_gudid; do
    hdr "  ingest --dataset $ds"
    "$PY" run.py ingest --dataset "$ds" ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} || warn "  $ds failed (continuing)"
  done
  ok "FDA phase complete"
else
  hdr "4/7  FDA MAUDE + GUDID — skipped (set INCLUDE_FDA=1 to re-enable)"
fi

# ============================================================
# 5. HCPCS master + Stark DHS + ClinicalTrials.gov
# ============================================================
hdr "5/7  Ingesting HCPCS master + Stark DHS + ClinicalTrials.gov"

# HCPCS master is required for the Groq mapper later (provides long_desc
# corpus for token-overlap shortlisting), so always run it unless --only
# explicitly excluded.
if [ -z "$ONLY" ] || in_only hcpcs; then
  hdr "  ingest --dataset hcpcs_master"
  "$PY" run.py ingest --dataset hcpcs_master ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} || warn "hcpcs_master failed"
fi

if [ "$SKIP_STARK" -eq 0 ]; then
  hdr "  ingest --dataset stark_dhs"
  "$PY" run.py ingest --dataset stark_dhs ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} || warn "stark_dhs failed"
fi

if [ "$SKIP_TRIALS" -eq 0 ]; then
  hdr "  ingest --dataset clinical_trials"
  # ClinicalTrials.gov supports a per-bucket cap via the env var the
  # ingester reads; LIMIT here is a global cap across all buckets.
  "$PY" run.py ingest --dataset clinical_trials ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} || warn "clinical_trials failed"
fi
ok "HCPCS + Stark + Trials phase complete"

# ============================================================
# 6. CMS data-api datasets via TinyFish-resolved bulk CSVs
#    (Inpatient / Outpatient / DMEPOS / Physician PUF — the HCPCS-rich
#    utilization tables. data-api is Akamai-blocked, so we go the bulk
#    CSV route.)
# ============================================================
if [ "$SKIP_TINYFISH" -eq 0 ]; then
  hdr "6/7  Resolving CMS data-api CSV URLs via the data.cms.gov catalog"
  # The catalog (data.cms.gov/data.json) is a DCAT-1.1 dump of every
  # dataset with direct CSV downloadURLs. Cheaper + more reliable than
  # TinyFish-scraping the landing pages (which now return 404 on the
  # /data-viewer/?id=<uuid> pattern).
  if [ "$TF_DOWNLOAD" = "1" ]; then
    "$PY" scripts/fetch_cms_api_csvs.py
  else
    "$PY" scripts/fetch_cms_api_csvs.py --discover-only
    warn "TF_DOWNLOAD=0 — skipping CSV download. Set =1 to fetch."
  fi

  hdr "  ingesting CMS data-api CSVs from downloads/cms_api/"
  for ds in medicare_inpatient_by_provider \
            medicare_inpatient_by_provider_service \
            medicare_outpatient_by_provider_service \
            medicare_dmepos_by_supplier \
            medicare_dmepos_by_supplier_service \
            medicare_dmepos_by_referring_provider \
            medicare_physician_by_provider \
            medicare_physician_by_provider_service \
            provider_of_services \
            betos_classification \
            hospital_cost_report; do
    csv="downloads/cms_api/${ds}.csv"
    if [ ! -f "$csv" ]; then
      warn "  $ds: $csv not present — skipping (TinyFish may not have resolved it)"
      continue
    fi
    hdr "  ingest --dataset $ds --csv-path $csv"
    "$PY" run.py ingest --dataset "$ds" --csv-path "$csv" ${LIMIT_ARGS[@]+"${LIMIT_ARGS[@]}"} \
      || warn "  $ds failed (continuing)"
  done
  ok "CMS data-api CSV phase complete"
fi

# ============================================================
# 7. Groq trial→HCPCS mapper (requires hcpcs_master + clinical_trials
#    populated). Skips automatically if GROQ_API_KEY is missing.
# ============================================================
if [ "$SKIP_GROQ" -eq 0 ]; then
  hdr "7/7  Mapping ClinicalTrials interventions → HCPCS codes via Groq"
  MAPPER_ARGS=(--batch-size "$GROQ_BATCH_SIZE")
  [ -n "$LIMIT" ] && MAPPER_ARGS+=(--limit "$LIMIT")
  "$PY" run.py map-trials-to-hcpcs "${MAPPER_ARGS[@]}" || warn "Groq mapper failed"
  ok "Groq mapper complete"
fi

# ============================================================
# 8. NEW SOURCES (see NEW_SOURCES.md)
#    OIG LEIE, MPFS, Opt-Out, POS, Order&Referring, Taxonomy Crosswalk,
#    FFS Enrollment, HCRIS, Revalidation, MA Monthly Enrollment,
#    Star Ratings, BETOS (RBCS). Most ingesters resolve their CSV URL
#    via the data.cms.gov DCAT catalog; PFS + Star Ratings hit cms.gov
#    ZIPs directly. Each ingester is idempotent (ReplacingMergeTree).
# ============================================================
if [ "$SKIP_NEW" -eq 0 ]; then
  hdr "8/8  New sources (12 ingesters)"
  for mod in \
      oig_leie_ingest \
      opt_out_ingest \
      pos_ingest \
      order_referring_ingest \
      taxonomy_crosswalk_ingest \
      ffs_enrollment_ingest \
      hcris_ingest \
      revalidation_ingest \
      ma_enrollment_ingest \
      betos_ingest \
      mpfs_ingest \
      star_ratings_ingest; do
    hdr "  python3 -m src.${mod}"
    "$PY" -m "src.${mod}" || warn "  ${mod} failed (continuing)"
  done
  ok "New sources phase complete"
fi

# ============================================================
# Summary — ingestion log tail
# ============================================================
hdr "Pipeline complete — log: ${LOG_FILE}"
"$PY" run.py status | head -60 || true
echo ""
ok "All phases finished. Query by HCPCS:"
cat <<'EOF'
  -- Example HCPCS-centric join (every source by code)
  SELECT  m.hcpcs_code, m.short_desc,
          (SELECT count() FROM cms_provider_summary  WHERE hcpcs_code = m.hcpcs_code) AS utilization_rows,
          (SELECT count() FROM stark_dhs_codes        WHERE hcpcs_code = m.hcpcs_code) AS stark_dhs_rows,
          (SELECT count() FROM clinical_trial_interventions FINAL WHERE hcpcs_code = m.hcpcs_code) AS trial_rows
  FROM    hcpcs_master m FINAL
  WHERE   m.hcpcs_code = 'C9743'
EOF
