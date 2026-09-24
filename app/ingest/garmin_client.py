"""Thin wrappers around garminconnect calls used by the sync pipeline."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

MAX_RANGE_DAYS = 365  # Garmin rejects ranges over 366 days on some endpoints


def _chunk_range(start: date, end: date, max_days: int = MAX_RANGE_DAYS):
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_days), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def _fetch_chunked(
    client, start: date, end: date, fetch_one: Callable[[Any, date, date], list]
) -> list:
    results: list = []
    for chunk_start, chunk_end in _chunk_range(start, end):
        results.extend(fetch_one(client, chunk_start, chunk_end) or [])
    return results


def fetch_activities(client, start: date, end: date) -> list[dict[str, Any]]:
    return client.get_activities_by_date(start.isoformat(), end.isoformat(), None)


def fetch_exercise_sets(client, activity_id: int) -> dict[str, Any]:
    return client.get_activity_exercise_sets(activity_id)


def fetch_sleep(client, start: date, end: date) -> list[dict[str, Any]]:
    # get_sleep_daily auto-chunks internally (28-day windows), no cap here needed.
    return client.get_sleep_daily(start.isoformat(), end.isoformat()) or []


def fetch_hrv(client, start: date, end: date) -> list[dict[str, Any]]:
    def _one(c, s: date, e: date):
        result = c.get_hrv_data_range(s.isoformat(), e.isoformat())
        return (result or {}).get("hrvSummaries", [])

    return _fetch_chunked(client, start, end, _one)


def fetch_rhr(client, start: date, end: date) -> list[dict[str, Any]]:
    def _one(c, s: date, e: date):
        return c.get_rhr_daily(s.isoformat(), e.isoformat())

    return _fetch_chunked(client, start, end, _one)


def fetch_stress_and_battery(client, day: date) -> dict[str, Any]:
    """One day's stress + body battery, both embedded in get_all_day_stress()."""
    return client.get_all_day_stress(day.isoformat())
