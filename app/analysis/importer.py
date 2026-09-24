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
        from app.ingest.garmin_client import GarminUnavailable

        def show(i, n, r):
            print(f"[{i}/{n}] {r['start_date']} {r['activity_name']}")

        try:
            res = service.import_garmin_water(conn, args.garmin_since, fetch_conditions=not args.no_conditions,
                                              on_item=show)
        except GarminUnavailable as e:
            print(f"Garmin unavailable: {e}", file=sys.stderr)
            return 1
        failures += res["failed"]
        for err in res["errors"]:
            print(f"FAILED   {err}", file=sys.stderr)
        print(f"imported {res['imported']} water session(s) from Garmin")

    if args.reanalyze or args.files or args.garmin_since:
        n = service.reanalyze_all(conn)
        print(f"analyses up to date for {n} tracks")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
