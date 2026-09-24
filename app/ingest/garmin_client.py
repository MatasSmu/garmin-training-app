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


class GarminUnavailable(RuntimeError):
    """Saved Garmin tokens are missing/expired, or Garmin can't be reached."""


def login_saved_tokens():
    """Log in with the tokens saved in ~/.garminconnect -- never prompts, so
    it's safe inside the web app. Raises GarminUnavailable with a fix-it hint."""
    from garminconnect import Garmin

    from app.config import TOKEN_DIR

    try:
        client = Garmin()
        client.login(str(TOKEN_DIR.expanduser()))
        return client
    except Exception as e:  # noqa: BLE001 -- garminconnect raises a zoo of types
        raise GarminUnavailable(
            f"Garmin login with the saved tokens failed ({type(e).__name__}). "
            "Sign in again once from a terminal: ./venv/bin/python garmin_export.py"
        ) from e


def fetch_original_file(activity_id: int, client=None) -> tuple[bytes, str]:
    """The activity's original upload (usually a .fit) as (bytes, filename)."""
    import io
    import zipfile

    from garminconnect import Garmin

    client = client or login_saved_tokens()
    try:
        blob = client.download_activity(str(activity_id), dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
    except Exception as e:  # noqa: BLE001
        raise GarminUnavailable(f"Couldn't download from Garmin ({type(e).__name__}): {e}") from e
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith((".fit", ".gpx"))]
            if not names:
                raise GarminUnavailable("Garmin's download contained no FIT/GPX file")
            return zf.read(names[0]), names[0]
    return blob, f"{activity_id}.fit"
