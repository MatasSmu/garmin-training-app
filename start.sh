#!/usr/bin/env bash
# One command to run the whole stack locally:
#   ./start.sh            -> http://localhost:8731 (PORT=... to change)
# Creates the venv and installs dependencies on first run, seeds the
# session-analysis database from samples/ if it has no tracks yet, then
# serves the app. (run_app.sh is the lean entry point the systemd unit uses.)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x venv/bin/python ]; then
  echo "Creating virtualenv…"
  python3 -m venv venv
fi
if ! venv/bin/python -c "import fastapi, uvicorn, numpy, fitparse, gpxpy, dotenv, multipart" 2>/dev/null; then
  echo "Installing dependencies…"
  venv/bin/pip install -q -r requirements.txt
fi
if [ ! -f .env ] && [ -z "${APP_USERNAME:-}" ]; then
  echo "Missing .env -- create one with at least:" >&2
  echo "  APP_USERNAME=you" >&2
  echo "  APP_PASSWORD=something-long" >&2
  exit 1
fi
shopt -s nullglob
samples=(samples/*.fit samples/*.gpx)
if [ ${#samples[@]} -gt 0 ]; then
  venv/bin/python -m app.analysis.importer --if-empty "${samples[@]}"
fi
exec venv/bin/uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8731}"
