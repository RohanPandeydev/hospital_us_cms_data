#!/usr/bin/env bash
# One-command launcher: init schema, warm the risk data, and start the UI with
# live logs streaming to the foreground.
#
# Usage:
#   ./start_all.sh                # full: schema + ingest (regex+Groq) + build + server
#   ./start_all.sh --no-llm       # same, but tag recalls with regex only (no Groq)
#   ./start_all.sh --skip-ingest  # just (re)build JSON + serve
#   ./start_all.sh --server-only  # skip everything, just serve (fastest)
#   PORT=9000 ./start_all.sh      # override port
#   HOST=0.0.0.0 ./start_all.sh   # bind all interfaces
#
# All stdout/stderr from each step streams directly to this terminal AND to
# logs/run-<timestamp>.log (everything, not just the server phase). Ctrl+C
# stops the server and exits.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

# --- flags ---
SKIP_INGEST=0
SERVER_ONLY=0
NO_LLM=0
RECALL_LIMIT="${RECALL_LIMIT:-1000}"
TRIAL_LIMIT_PER_BUCKET="${TRIAL_LIMIT_PER_BUCKET:-30}"
HOSPITALS_LIMIT="${HOSPITALS_LIMIT:-}"   # "" = all hospitals

for a in "$@"; do
  case "$a" in
    --skip-ingest) SKIP_INGEST=1 ;;
    --server-only) SERVER_ONLY=1; SKIP_INGEST=1 ;;
    --no-llm)      NO_LLM=1 ;;
    -h|--help)
      sed -n '2,13p' "$0"; exit 0 ;;
  esac
done

# --- venv ---
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

PY="${PYTHON:-python}"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

# --- logging: tee every line of this script (ingest + server) to a log file ---
mkdir -p logs
LOG_FILE="logs/run-$(date +%Y%m%d-%H%M%S).log"
# Redirect fd 1/2 through `tee` so *all* subsequent commands — both the Python
# ingest steps and uvicorn — land in the log AND on the terminal.
exec > >(tee -a "$LOG_FILE") 2>&1

# ANSI color helpers for readable section headers
BLUE='\033[1;34m'; GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RESET='\033[0m'
hdr() { printf "\n${BLUE}▸ %s${RESET}\n" "$*"; }
ok()  { printf "${GREEN}  ✓ %s${RESET}\n" "$*"; }
warn(){ printf "${YELLOW}  ! %s${RESET}\n" "$*"; }

# Compose recall-ingest flags once so the --no-llm choice is visible at a glance.
RECALL_FLAGS=(--source recalls --limit "$RECALL_LIMIT")
if [ "$NO_LLM" -eq 1 ]; then
  RECALL_FLAGS+=(--no-llm)
fi

hdr "start_all.sh — mode: $( [ $NO_LLM -eq 1 ] && echo 'regex-only (no Groq)' || echo 'regex + Groq fallback')"

# --- pipeline ---
if [ "$SERVER_ONLY" -eq 0 ]; then
  hdr "Applying risk schema to Postgres cms_hospitals"
  "$PY" run.py risk-init
  ok "schema applied"
fi

if [ "$SKIP_INGEST" -eq 0 ]; then
  if [ "$NO_LLM" -eq 1 ]; then
    hdr "Pulling FDA device recalls (limit=${RECALL_LIMIT}) — regex/manual tagging ONLY (no LLM)"
  else
    hdr "Pulling FDA device recalls (limit=${RECALL_LIMIT}) + tagging with regex+Groq"
  fi
  "$PY" run.py risk-ingest "${RECALL_FLAGS[@]}"

  hdr "Pulling ClinicalTrials.gov studies (limit=${TRIAL_LIMIT_PER_BUCKET} per device bucket)"
  "$PY" run.py risk-ingest --source clinical_trials --limit "$TRIAL_LIMIT_PER_BUCKET"

  if [ -n "${OPEN_PAYMENTS_UUID:-}" ]; then
    hdr "Pulling Open Payments dataset ${OPEN_PAYMENTS_UUID}"
    "$PY" run.py risk-ingest --source open_payments --dataset "$OPEN_PAYMENTS_UUID" \
        ${OPEN_PAYMENTS_LIMIT:+--limit "$OPEN_PAYMENTS_LIMIT"}
  else
    warn "skipping Open Payments — set OPEN_PAYMENTS_UUID=<dkan-uuid> to include"
  fi
fi

if [ "$SERVER_ONLY" -eq 0 ]; then
  hdr "Assembling unified risk-intelligence JSON rows"
  if [ -n "$HOSPITALS_LIMIT" ]; then
    "$PY" run.py risk-build --limit-hospitals "$HOSPITALS_LIMIT"
  else
    "$PY" run.py risk-build
  fi
  ok "risk-build complete"
fi

hdr "Starting UI on http://${HOST}:${PORT}"
ok "full run log mirrored to ${LOG_FILE}"
ok "open http://${HOST}:${PORT}/risk for the Unified Risk Intelligence page"
echo "---"

# Run uvicorn in the foreground. fd1/2 are already teed to LOG_FILE from above.
exec uvicorn ui.app:app --host "$HOST" --port "$PORT" --reload --log-level info
