#!/usr/bin/env bash
# Start the CMS Hospital Data Explorer UI (FastAPI + Uvicorn).
#
# Usage:
#   ./run_server.sh              # defaults: host 127.0.0.1, port 8000, --reload
#   PORT=9000 ./run_server.sh    # override port
#   HOST=0.0.0.0 ./run_server.sh # bind on all interfaces
set -euo pipefail

cd "$(dirname "$0")"

# Activate venv if present
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

echo "→ CMS Hospital Explorer on http://${HOST}:${PORT}"
exec uvicorn ui.app:app --host "${HOST}" --port "${PORT}" --reload
