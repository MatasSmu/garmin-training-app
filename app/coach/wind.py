"""Short-term wind forecast for the athlete's current sailing spot.

Uses Open-Meteo (free, no API key) rather than Windy's paid point-forecast
API. The spot itself isn't configured anywhere -- it's inferred from the
`location` field of recent sessions, so it follows the athlete automatically
when they travel instead of going stale.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import defaultdict
from datetime import date, timedelta

import httpx

logger = logging.getLogger(__name__)

_GEOCODE_CACHE: dict[str, tuple[float, float] | None] = {}
_FORECAST_CACHE: dict[str, tuple[float, dict]] = {}
_FORECAST_TTL_S = 1800  # 30 min -- forecasts don't change fast enough to justify hitting the API every chat turn

_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _compass(deg: float) -> str:
    return _COMPASS[round(deg / 22.5) % 16]


def primary_location(conn: sqlite3.Connection, days: int = 30) -> str | None:
    """Most-used real location in the last `days`, falling back to the most
    recent session with a real location ever logged."""
    since = (date.today() - timedelta(days=days)).isoformat()
    row = conn.execute(
        """
        SELECT location, COUNT(*) c FROM sessions
        WHERE start_date >= ? AND location IS NOT NULL
          AND location != '' AND location != 'Unspecified'
        GROUP BY location ORDER BY c DESC, MAX(start_date) DESC LIMIT 1
        """,
        (since,),
    ).fetchone()
    if row:
        return row["location"]
    row = conn.execute(
        "SELECT location FROM sessions WHERE location IS NOT NULL "
        "AND location != '' AND location != 'Unspecified' "
        "ORDER BY start_date DESC LIMIT 1"
    ).fetchone()
    return row["location"] if row else None


def _geocode(place: str) -> tuple[float, float] | None:
    if place in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[place]
    coords = None
    try:
        resp = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": place, "count": 1},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if results:
            coords = (results[0]["latitude"], results[0]["longitude"])
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        logger.exception("geocoding failed for %r", place)
    _GEOCODE_CACHE[place] = coords
    return coords


def _fetch_forecast(place: str, lat: float, lon: float) -> dict | None:
    cached = _FORECAST_CACHE.get(place)
    if cached and time.monotonic() - cached[0] < _FORECAST_TTL_S:
        return cached[1]
    try:
        resp = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "windspeed_10m,windgusts_10m,winddirection_10m",
                "wind_speed_unit": "kn",
                "forecast_days": 3,
                "timezone": "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, KeyError, ValueError):
        logger.exception("wind forecast fetch failed for %s", place)
        return None
    _FORECAST_CACHE[place] = (time.monotonic(), data)
    return data


def forecast_briefing(conn: sqlite3.Connection) -> str:
    """Plain-language wind outlook for the athlete's current spot, ready to
    drop into the coach's system prompt. Never raises -- returns an
    explanatory line instead when anything upstream is unavailable."""
    place = primary_location(conn)
    if not place:
        return "Wind forecast: no session location on record to base one on."
    coords = _geocode(place)
    if not coords:
        return f"Wind forecast: couldn't resolve a location for \"{place}\"."
    data = _fetch_forecast(place, *coords)
    if not data:
        return f"Wind forecast: unavailable right now for {place}."

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    speeds = hourly.get("windspeed_10m", [])
    gusts = hourly.get("windgusts_10m", [])
    dirs = hourly.get("winddirection_10m", [])

    by_day: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for t, s, g, d in zip(times, speeds, gusts, dirs):
        hour = int(t[11:13])
        if 6 <= hour <= 20:  # daylight / plausible session hours only
            by_day[t[:10]].append((s, g, d))

    if not by_day:
        return f"Wind forecast: no usable data returned for {place}."

    lines = [f"Wind forecast for {place} (daylight hours, knots, next {len(by_day)} day(s)):"]
    for day, rows in list(by_day.items())[:3]:
        day_speeds = [r[0] for r in rows]
        day_gusts = [r[1] for r in rows]
        avg_dir = _compass(sum(r[2] for r in rows) / len(rows))
        lines.append(
            f"- {day}: {min(day_speeds):.0f}-{max(day_speeds):.0f} kn, "
            f"gusts to {max(day_gusts):.0f} kn, mostly {avg_dir}"
        )
    return "\n".join(lines)
