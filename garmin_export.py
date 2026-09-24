#!/usr/bin/env python3
"""Export activities from a personal Garmin Connect account."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_auth import DEFAULT_TOKEN_DIR, connect


def parse_args() -> argparse.Namespace:
    today = date.today()
    parser = argparse.ArgumentParser(
        description="Export Garmin Connect activity summaries and optional FIT files."
    )
    parser.add_argument(
        "--start-date",
        default=(today - timedelta(days=30)).isoformat(),
        help="First activity date in YYYY-MM-DD format (default: 30 days ago).",
    )
    parser.add_argument(
        "--end-date",
        default=today.isoformat(),
        help="Last activity date in YYYY-MM-DD format (default: today).",
    )
    parser.add_argument(
        "--activity-type",
        help="Optional filter, for example: running, cycling, swimming, or walking.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("activities.json"),
        help="JSON summary output path (default: activities.json).",
    )
    parser.add_argument(
        "--download-originals",
        action="store_true",
        help="Also download each original activity as a ZIP containing its FIT file.",
    )
    parser.add_argument(
        "--activity-dir",
        type=Path,
        default=Path("activities"),
        help="Directory for original activity downloads (default: activities).",
    )
    parser.add_argument(
        "--token-dir",
        type=Path,
        default=DEFAULT_TOKEN_DIR,
        help="Directory used to store login tokens (default: ~/.garminconnect).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        client = connect(args.token_dir)
        activities = client.get_activities_by_date(
            startdate=args.start_date,
            enddate=args.end_date,
            activitytype=args.activity_type,
        )

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(activities, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Saved {len(activities)} activity summaries to {args.output}")

        if args.download_originals:
            args.activity_dir.mkdir(parents=True, exist_ok=True)
            for position, activity in enumerate(activities, start=1):
                activity_id = str(activity["activityId"])
                destination = args.activity_dir / f"{activity_id}.zip"

                if destination.exists():
                    print(f"[{position}/{len(activities)}] Skipping {destination}")
                    continue

                content = client.download_activity(
                    activity_id,
                    dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL,
                )
                destination.write_bytes(content)
                print(f"[{position}/{len(activities)}] Downloaded {destination}")

        return 0
    except GarminConnectTooManyRequestsError as error:
        print(f"Garmin rate limit reached: {error}", file=sys.stderr)
    except GarminConnectAuthenticationError as error:
        print(f"Garmin authentication failed: {error}", file=sys.stderr)
    except GarminConnectConnectionError as error:
        print(f"Garmin connection failed: {error}", file=sys.stderr)
    except (OSError, ValueError, KeyError) as error:
        print(f"Export failed: {error}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
