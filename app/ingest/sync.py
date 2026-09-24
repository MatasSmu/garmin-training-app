#!/usr/bin/env python3
"""Pull activities + daily health metrics from Garmin and upsert into SQLite.

Run with no flags for the routine rolling pull (last 35 days), or with
--start-date for a one-time full backfill:

    python -m app.ingest.sync
    python -m app.ingest.sync --start-date 2015-01-01
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

from garmin_auth import DEFAULT_TOKEN_DIR, connect

from app.config import DEFAULT_SYNC_WINDOW_DAYS
from app.db import (
    get_connection,
    init_db,
    replace_exercise_sets,
    upsert_daily_metric_partial,
    upsert_session,
)
from app.ingest.garmin_client import (
    fetch_activities,
    fetch_exercise_sets,
    fetch_hrv,
    fetch_rhr,
    fetch_sleep,
    fetch_stress_and_battery,
)
from app.ingest.transform import (
    activity_to_session_row,
    exercise_set_rows,
    hrv_row,
    rhr_row,
    sleep_row,
    stress_and_battery_row,
)


def run_sync(start: date, end: date, token_dir: Path = DEFAULT_TOKEN_DIR, client=None) -> dict:
    client = client or connect(token_dir)
    conn = get_connection()
    init_db(conn)

    counts = {"sessions": 0, "exercise_sets": 0, "daily_metrics_days": 0}

    activities = fetch_activities(client, start, end)
    for activity in activities:
        upsert_session(conn, activity_to_session_row(activity))
        counts["sessions"] += 1
        if activity.get("activityType", {}).get("typeKey") == "strength_training":
            sets_payload = fetch_exercise_sets(client, activity["activityId"])
            rows = exercise_set_rows(sets_payload)
            replace_exercise_sets(conn, activity["activityId"], rows)
            counts["exercise_sets"] += len(rows)
    conn.commit()

    touched_dates: set[str] = set()

    for entry in fetch_sleep(client, start, end):
        d = entry.get("calendarDate")
        if d:
            upsert_daily_metric_partial(conn, d, sleep_row(entry))
            touched_dates.add(d)
    conn.commit()

    for entry in fetch_hrv(client, start, end):
        d = entry.get("calendarDate")
        if d:
            upsert_daily_metric_partial(conn, d, hrv_row(entry))
            touched_dates.add(d)
    conn.commit()

    for entry in fetch_rhr(client, start, end):
        d = entry.get("calendarDate")
        if d:
            upsert_daily_metric_partial(conn, d, rhr_row(entry))
            touched_dates.add(d)
    conn.commit()

    # get_all_day_stress has no range variant -- it's one HTTP call per
    # calendar day. A multi-year backfill would mean thousands of calls and
    # risk a Garmin rate limit, so this loop is always capped to the recent
    # rolling window regardless of how far back `start` goes; older
    # stress/body-battery history isn't useful for the coach's signals anyway.
    stress_start = max(start, end - timedelta(days=DEFAULT_SYNC_WINDOW_DAYS))
    cur = stress_start
    while cur <= end:
        payload = fetch_stress_and_battery(client, cur)
        if payload:
            upsert_daily_metric_partial(conn, cur.isoformat(), stress_and_battery_row(payload))
            touched_dates.add(cur.isoformat())
        cur += timedelta(days=1)

    counts["daily_metrics_days"] = len(touched_dates)

    conn.commit()
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=date.fromisoformat, default=None)
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--days", type=int, default=DEFAULT_SYNC_WINDOW_DAYS)
    parser.add_argument("--token-dir", type=Path, default=DEFAULT_TOKEN_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    start = args.start_date or (args.end_date - timedelta(days=args.days))
    counts = run_sync(start, args.end_date, args.token_dir)
    print(
        f"Synced {counts['sessions']} sessions, {counts['exercise_sets']} exercise sets, "
        f"{counts['daily_metrics_days']} days of daily metrics "
        f"({start.isoformat()} to {args.end_date.isoformat()})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
