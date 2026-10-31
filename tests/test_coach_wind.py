"""Coach wind forecasts: the home spot (Kaunas) always, in 3-hour blocks at
the athlete's sailing spot, plus the current spot when travelling."""

import sqlite3
from datetime import datetime

import pytest

from app.coach import wind
from app.db import init_db


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "w.db")
    c.row_factory = sqlite3.Row
    init_db(c)
    yield c
    c.close()


def _hourly(days=("2026-11-02", "2026-11-03"), kn=10.0, gust=16.0, dirs=(350.0, 10.0), temp=6.0):
    times, s, g, d, t = [], [], [], [], []
    for day in days:
        for h in range(24):
            times.append(f"{day}T{h:02d}:00")
            s.append(kn + (2 if 11 <= h < 14 else 0))
            g.append(gust)
            d.append(dirs[h % len(dirs)])
            t.append(temp + (h >= 12))
    return {"hourly": {"time": times, "windspeed_10m": s, "windgusts_10m": g, "winddirection_10m": d,
                       "temperature_2m": t}}


def _track(conn, spot, lat, lon, i):
    conn.execute(
        """INSERT INTO tracks (file_name, file_sha1, file_path, sport, start_utc, end_utc, start_date, lat, lon, spot,
                               created_at, updated_at) VALUES ('f', ?, 'p', 'windfoil', 0, 1, '2026-10-01', ?, ?, ?, 'x', 'x')""",
        (f"sha{i}", lat, lon, spot))


def test_home_coords_from_tracks_else_default(conn, monkeypatch):
    monkeypatch.delenv("HOME_SPOT_LAT", raising=False)
    assert wind.home_coords(conn) == wind.HOME_DEFAULT_COORDS
    for i, (lat, lon) in enumerate([(54.88, 24.06), (54.89, 24.07), (54.87, 24.13)]):
        _track(conn, "Kaunas", lat, lon, i)
    _track(conn, "Puck", 54.7, 18.4, 9)
    assert wind.home_coords(conn) == (54.88, 24.07)  # median of the Kaunas tracks only


def test_home_briefing_blocks(conn, monkeypatch):
    calls = []

    def fake(place, lat, lon, days=3):
        calls.append((place, lat, lon, days))
        return _hourly()

    monkeypatch.setattr(wind, "_fetch_forecast", fake)
    text = wind.home_forecast_briefing(conn, now=datetime(2026, 11, 2, 12, 30))
    assert calls == [("Kaunas", *wind.HOME_DEFAULT_COORDS, wind.HOME_FORECAST_DAYS)]
    assert text.startswith("Home spot Kaunas (lake -- expect shifty")
    lines = text.splitlines()
    # today: blocks already over are dropped (08-11 ended before 12:30; 11-14 is still on)
    assert "08-11" not in lines[1] and "11-14 12 kn g16 N" in lines[1]
    assert lines[1].startswith("- 2026-11-02 (Mon), air 6-7 C, direction swings ~20 deg")
    assert "08-11 10 kn g16 N; 11-14 12 kn g16 N; 14-17 10 kn g16 N; 17-20 10 kn g16 N" in lines[2]


def test_direction_mean_across_north():
    assert round(wind._circ_mean([350, 10])) % 360 == 0
    assert wind._compass(wind._circ_mean([350, 10])) == "N"
    assert round(wind._dir_swing([350, 10, 0])) == 20


def test_briefing_adds_current_spot_when_travelling(conn, monkeypatch):
    monkeypatch.setattr(wind, "_fetch_forecast", lambda *a, **k: _hourly())
    monkeypatch.setattr(wind, "_geocode", lambda place: (36.5, -6.3))
    monkeypatch.setattr(wind, "primary_location", lambda c: "Cadiz")
    text = wind.forecast_briefing(conn)
    assert text.startswith("Home spot Kaunas") and "Wind forecast for Cadiz" in text
    assert "mostly N" in text  # 350/10 averaged across north
    monkeypatch.setattr(wind, "primary_location", lambda c: "Kaunas")
    assert "Cadiz" not in wind.forecast_briefing(conn)


def test_briefing_never_raises_without_forecast(conn):
    # the autouse fixture makes every fetch fail
    assert wind.forecast_briefing(conn) == "Home spot Kaunas: wind forecast unavailable right now."
