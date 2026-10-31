"""Wind forecasts for the coach: the athlete's home spot, always, in detail,
plus the spot they're currently sailing when that's somewhere else.

Uses Open-Meteo (free, no API key) rather than Windy's paid point-forecast
API. The home spot (HOME_SPOT, default Kaunas) is forecast where the athlete
actually sails -- the middle of their tracks there -- not the city centre.
The current spot isn't configured anywhere: it's inferred from the
`location` field of recent sessions, so it follows the athlete when they
travel instead of going stale.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx

from app.sailing.config import water_of

logger = logging.getLogger(__name__)

_GEOCODE_CACHE: dict[str, tuple[float, float] | None] = {}
_FORECAST_CACHE: dict[str, tuple[float, dict]] = {}
_FORECAST_TTL_S = 1800  # 30 min -- forecasts don't change fast enough to justify hitting the API every chat turn

_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


# Home spot: forecast every turn, in 3-hour blocks for the next few days.
HOME_SPOT = os.environ.get("HOME_SPOT", "Kaunas")
# Kaunas reservoir, where the athlete's Kaunas tracks are; used until tracks exist.
HOME_DEFAULT_COORDS = (54.884, 24.071)
HOME_FORECAST_DAYS = 5
HOME_BLOCKS = ((8, 11), (11, 14), (14, 17), (17, 20))   # local hours [start, end)


def _compass(deg: float) -> str:
    return _COMPASS[round(deg / 22.5) % 16]


def _circ_mean(degs: list[float]) -> float:
    """Mean direction (350 and 10 -> 0, not 180)."""
    s = sum(math.sin(math.radians(d)) for d in degs)
    c = sum(math.cos(math.radians(d)) for d in degs)
    return math.degrees(math.atan2(s, c)) % 360


def _dir_swing(degs: list[float]) -> float:
    """Largest angle between any direction and the mean direction, x2: how far
    the forecast direction swings over the hours given."""
    m = _circ_mean(degs)
    return 2 * max(abs((d - m + 180) % 360 - 180) for d in degs)


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


def _fetch_forecast(place: str, lat: float, lon: float, days: int = 3) -> dict | None:
    key = f"{place}|{lat:.3f},{lon:.3f}|{days}"
    cached = _FORECAST_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < _FORECAST_TTL_S:
        return cached[1]
    try:
        resp = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "windspeed_10m,windgusts_10m,winddirection_10m,temperature_2m",
                "wind_speed_unit": "kn",
                "forecast_days": days,
                "timezone": "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, KeyError, ValueError):
        logger.exception("wind forecast fetch failed for %s", place)
        return None
    _FORECAST_CACHE[key] = (time.monotonic(), data)
    return data


def home_coords(conn: sqlite3.Connection, spot: str = HOME_SPOT) -> tuple[float, float]:
    """Where the athlete sails at the home spot: the middle of their tracks
    there (median), else HOME_SPOT_LAT/LON from the environment, else the
    Kaunas reservoir default."""
    rows = conn.execute(
        "SELECT lat, lon FROM tracks WHERE lower(spot) = lower(?) AND lat IS NOT NULL AND lon IS NOT NULL", (spot,)
    ).fetchall()
    if rows:
        lats, lons = sorted(r["lat"] for r in rows), sorted(r["lon"] for r in rows)
        return round(lats[len(lats) // 2], 4), round(lons[len(lons) // 2], 4)
    if os.environ.get("HOME_SPOT_LAT") and os.environ.get("HOME_SPOT_LON"):
        return float(os.environ["HOME_SPOT_LAT"]), float(os.environ["HOME_SPOT_LON"])
    return HOME_DEFAULT_COORDS


def home_forecast_briefing(conn: sqlite3.Connection, spot: str = HOME_SPOT, now: datetime | None = None) -> str:
    """Detailed outlook for the home spot: per day the air temperature, how
    far the forecast direction swings, and 3-hour blocks (08-20 local) with
    mean wind, max gust and direction. Never raises."""
    lat, lon = home_coords(conn, spot)
    data = _fetch_forecast(spot, lat, lon, days=HOME_FORECAST_DAYS)
    if not data:
        return f"Home spot {spot}: wind forecast unavailable right now."
    h = data.get("hourly", {})
    rows = list(zip(h.get("time", []), h.get("windspeed_10m", []), h.get("windgusts_10m", []),
                    h.get("winddirection_10m", []), h.get("temperature_2m", []) or [None] * len(h.get("time", []))))
    by_day: dict[str, list[tuple]] = defaultdict(list)
    for t, s, g, d, temp in rows:
        if s is None or d is None:
            continue
        by_day[t[:10]].append((int(t[11:13]), s, g if g is not None else s, d, temp))
    if not by_day:
        return f"Home spot {spot}: no usable forecast data."
    water = "lake -- expect shifty, gusty wind; " if water_of(spot) == "lake" else ""
    now = now or datetime.now()  # forecast times are local (timezone=auto); so is the server
    today = now.date().isoformat()
    lines = [f"Home spot {spot} ({water}Open-Meteo model forecast at the athlete's sailing spot "
             f"{lat:.3f}, {lon:.3f}; knots, local time, wind = mean, gust = max, dir = from):"]
    for day, hours in list(by_day.items())[:HOME_FORECAST_DAYS]:
        daylight = [x for x in hours if HOME_BLOCKS[0][0] <= x[0] < HOME_BLOCKS[-1][1]]
        if not daylight:
            continue
        temps = [x[4] for x in daylight if x[4] is not None]
        head = f"- {day} ({date.fromisoformat(day).strftime('%a')})"
        if temps:
            head += f", air {min(temps):.0f}-{max(temps):.0f} C"
        head += f", direction swings ~{_dir_swing([x[3] for x in daylight]):.0f} deg over the day:"
        blocks = []
        for lo, hi in HOME_BLOCKS:
            if day < today or (day == today and hi <= now.hour):
                continue  # already over
            b = [x for x in daylight if lo <= x[0] < hi]
            if b:
                blocks.append(f"{lo:02d}-{hi:02d} {sum(x[1] for x in b) / len(b):.0f} kn g{max(x[2] for x in b):.0f} "
                              f"{_compass(_circ_mean([x[3] for x in b]))}")
        if blocks:
            lines.append(head + " " + "; ".join(blocks))
    return "\n".join(lines)


def forecast_briefing(conn: sqlite3.Connection) -> str:
    """Wind outlook for the coach's system prompt: the home spot in detail,
    always, then the current spot if the athlete is sailing somewhere else.
    Never raises -- explanatory lines instead when something is unavailable."""
    home = home_forecast_briefing(conn)
    place = primary_location(conn)
    if not place or place.lower() == HOME_SPOT.lower():
        return home
    return home + "\n" + current_spot_briefing(conn, place)


def current_spot_briefing(conn: sqlite3.Connection, place: str) -> str:
    """Daily outlook for the spot of the athlete's recent sessions."""
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
        avg_dir = _compass(_circ_mean([r[2] for r in rows]))
        lines.append(
            f"- {day}: {min(day_speeds):.0f}-{max(day_speeds):.0f} kn, "
            f"gusts to {max(day_gusts):.0f} kn, mostly {avg_dir}"
        )
    return "\n".join(lines)
