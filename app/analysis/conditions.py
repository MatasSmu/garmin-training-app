"""Session conditions from Open-Meteo (free, no key): wind, gusts, direction,
air temperature for the hours the session spans, plus sea-surface
temperature where the marine model covers the spot.

The historical-forecast API (high-res models, 2022+) is tried first -- it
resolves coastal wind far better than the ERA5 reanalysis the archive API
falls back to.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import numpy as np

from app.analysis.geo import circ_mean

log = logging.getLogger(__name__)

SOURCES = [
    ("open-meteo (historical forecast)", "https://historical-forecast-api.open-meteo.com/v1/forecast"),
    ("open-meteo (ERA5 archive)", "https://archive-api.open-meteo.com/v1/archive"),
]
MARINE = "https://marine-api.open-meteo.com/v1/marine"


def _hours(start_utc: float, end_utc: float) -> tuple[str, str, int, int]:
    s = datetime.fromtimestamp(start_utc, timezone.utc)
    e = datetime.fromtimestamp(end_utc, timezone.utc)
    return s.date().isoformat(), e.date().isoformat(), s.hour, e.hour


def fetch(lat: float, lon: float, start_utc: float, end_utc: float, timeout: float = 20.0) -> dict | None:
    d0, d1, h0, h1 = _hours(start_utc, end_utc)
    params = {
        "latitude": round(lat, 4), "longitude": round(lon, 4), "start_date": d0, "end_date": d1,
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m",
        "wind_speed_unit": "kn", "timezone": "GMT",
    }
    for label, url in SOURCES:
        try:
            r = httpx.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            hourly = r.json()["hourly"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.info("conditions: %s failed: %s", label, e)
            continue
        times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp() for t in hourly["time"]]
        idx = [i for i, t in enumerate(times) if start_utc - 3600 < t <= end_utc]
        ws = [hourly["wind_speed_10m"][i] for i in idx]
        if not idx or any(v is None for v in ws):
            continue
        wd = [hourly["wind_direction_10m"][i] for i in idx]
        gs = [hourly["wind_gusts_10m"][i] for i in idx]
        ta = [hourly["temperature_2m"][i] for i in idx]
        out = {
            "source": label,
            "wind_kn": round(float(np.mean(ws)), 1),
            "gust_kn": round(float(np.nanmax(np.array(gs, dtype=float))), 1),
            "wind_dir": round(circ_mean(wd)) % 360,
            "air_c": round(float(np.nanmean(np.array(ta, dtype=float))), 1),
            "hourly": [{"t": times[i], "wind_kn": hourly["wind_speed_10m"][i],
                        "gust_kn": hourly["wind_gusts_10m"][i],
                        "wind_dir": hourly["wind_direction_10m"][i]} for i in idx],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        out["water_c"] = _sea_temp(lat, lon, d0, d1, idx, timeout)
        return out
    return None


NOMINATIM = "https://nominatim.openstreetmap.org/reverse"


def place_name(lat: float, lon: float, timeout: float = 10.0) -> str | None:
    """Best-effort spot name for a position (OpenStreetMap Nominatim, one
    call per imported track -- well inside its usage policy)."""
    try:
        r = httpx.get(NOMINATIM, params={"lat": round(lat, 4), "lon": round(lon, 4), "format": "jsonv2", "zoom": 12},
                      headers={"User-Agent": "personal-training-app/1.0 (session analysis)"}, timeout=timeout)
        r.raise_for_status()
        addr = r.json().get("address", {})
    except (httpx.HTTPError, ValueError):
        return None
    for key in ("village", "town", "city", "hamlet", "suburb", "municipality", "county"):
        if addr.get(key):
            return str(addr[key])
    return None


def _sea_temp(lat, lon, d0, d1, idx, timeout) -> float | None:
    try:
        r = httpx.get(MARINE, params={"latitude": round(lat, 4), "longitude": round(lon, 4),
                                      "start_date": d0, "end_date": d1,
                                      "hourly": "sea_surface_temperature", "timezone": "GMT"}, timeout=timeout)
        r.raise_for_status()
        vals = [r.json()["hourly"]["sea_surface_temperature"][i] for i in idx]
        vals = [v for v in vals if v is not None]
        return round(float(np.mean(vals)), 1) if vals else None
    except (httpx.HTTPError, KeyError, ValueError, IndexError):
        return None
