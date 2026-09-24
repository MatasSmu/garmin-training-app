#!/usr/bin/env bash
# Refresh activities.json from Garmin and rebuild windlog.html.
# Safe to run unattended (cron/systemd) once you've logged in once interactively.
set -euo pipefail
cd "$(dirname "$0")"

./venv/bin/python3 garmin_export.py --start-date 2015-01-01
./venv/bin/python3 build_windlog.py

echo "Windlog updated: $(date)"
