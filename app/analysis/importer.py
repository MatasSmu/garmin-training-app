#!/usr/bin/env python3
"""Import tracks into the session-analysis database.

    python -m app.analysis.importer samples/*.fit samples/*.gpx
    python -m app.analysis.importer --garmin-since 2026-06-01   # water sessions already synced
    python -m app.analysis.importer --reanalyze                 # refresh every cached analysis
    python -m app.analysis.importer --if-empty samples/*        # first-run seeding (start.sh)
"""

from __future__ import annotations

import argparse
import sys
import time

from app.analysis import service
from app.db import get_connection, init_db

WATER_TYPES = ("windsurfing", "kiteboarding", "kitesurfing", "wing", "foil", "sailing")


def _is_water(row) -> bool:
    text = f"{row['activity_type']} {row['activity_name'] or ''}".lower()
    return any(w in text for w in WATER_TYPES) or ("wind" in text and "other" in text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--garmin-since", help="download + import water sessions synced since this date")
    ap.add_argument("--reanalyze", action="store_true")
    ap.add_argument("--no-conditions", action="store_true", help="skip the Open-Meteo lookup")
    ap.add_argument("--if-empty", action="store_true", help="only import when there are no tracks yet (first run)")
    args = ap.parse_args(argv)

    conn = get_connection()
    init_db(conn)
    if args.if_empty and conn.execute("SELECT 1 FROM tracks LIMIT 1").fetchone():
        return 0
    failures = 0
    for f in args.files:
        t0 = time.time()
        try:
            tid, created = service.import_path(conn, f, fetch_conditions=not args.no_conditions)
            print(f"{'imported' if created else 'exists  '} #{tid:<4} {f} ({time.time() - t0:.1f}s)")
        except (ValueError, OSError) as e:
            failures += 1
            print(f"FAILED   {f}: {e}", file=sys.stderr)

    if args.garmin_since:
        from app.ingest.garmin_client import GarminUnavailable, fetch_original_file

        rows = conn.execute(
            "SELECT s.activity_id, s.activity_type, s.activity_name, s.start_date FROM sessions s "
            "LEFT JOIN tracks t ON t.activity_id = s.activity_id "
            "WHERE s.start_date >= ? AND t.id IS NULL ORDER BY s.start_date", (args.garmin_since,)).fetchall()
        for r in rows:
            if not _is_water(r):
                continue
            try:
                data, name = fetch_original_file(r["activity_id"])
                tid, created = service.import_bytes(conn, data, name, activity_id=r["activity_id"],
                                                    fetch_conditions=not args.no_conditions)
                print(f"{'imported' if created else 'exists  '} #{tid:<4} {r['start_date']} {r['activity_name']}")
            except GarminUnavailable as e:
                print(f"Garmin unavailable: {e}", file=sys.stderr)
                return 1
            except ValueError as e:
                failures += 1
                print(f"FAILED   {r['start_date']} {r['activity_name']}: {e}", file=sys.stderr)

    if args.reanalyze or args.files or args.garmin_since:
        n = service.reanalyze_all(conn)
        print(f"analyses up to date for {n} tracks")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
