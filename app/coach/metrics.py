"""Pure, DB-reading computations used by the coach engine and the dashboard."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta


def _date_n_days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def training_load(conn: sqlite3.Connection, days: int) -> dict:
    since = _date_n_days_ago(days)
    rows = conn.execute(
        "SELECT activity_type, duration_s, distance_m FROM sessions WHERE start_date >= ?",
        (since,),
    ).fetchall()
    by_type: dict[str, float] = {}
    total_duration_min = 0.0
    total_distance_km = 0.0
    for row in rows:
        minutes = (row["duration_s"] or 0) / 60
        by_type[row["activity_type"]] = by_type.get(row["activity_type"], 0.0) + minutes
        total_duration_min += minutes
        total_distance_km += (row["distance_m"] or 0) / 1000
    return {
        "session_count": len(rows),
        "total_duration_min": round(total_duration_min, 1),
        "total_distance_km": round(total_distance_km, 1),
        "by_type": {k: round(v, 1) for k, v in by_type.items()},
    }


def sleep_trend(conn: sqlite3.Connection, days: int = 7, baseline_days: int = 28) -> dict:
    recent = conn.execute(
        "SELECT sleep_duration_s FROM daily_metrics WHERE date >= ? AND sleep_duration_s IS NOT NULL",
        (_date_n_days_ago(days),),
    ).fetchall()
    baseline = conn.execute(
        "SELECT sleep_duration_s FROM daily_metrics WHERE date >= ? AND sleep_duration_s IS NOT NULL",
        (_date_n_days_ago(baseline_days),),
    ).fetchall()

    def avg_hours(rows) -> float | None:
        if not rows:
            return None
        return sum(r["sleep_duration_s"] for r in rows) / len(rows) / 3600

    avg = avg_hours(recent)
    base = avg_hours(baseline)
    delta_pct = round((avg - base) / base * 100, 1) if avg is not None and base else None
    return {
        "avg_sleep_h": round(avg, 2) if avg is not None else None,
        "baseline_avg_sleep_h": round(base, 2) if base is not None else None,
        "delta_pct": delta_pct,
        "nights_logged": len(recent),
    }


def days_since_rest(conn: sqlite3.Connection, lookback_days: int = 60) -> int | None:
    since = _date_n_days_ago(lookback_days)
    training_dates = {
        r["start_date"]
        for r in conn.execute(
            "SELECT DISTINCT start_date FROM sessions WHERE start_date >= ?", (since,)
        )
    }
    cur = date.today()
    for offset in range(lookback_days):
        d = (cur - timedelta(days=offset)).isoformat()
        if d not in training_dates:
            return offset
    return None  # no rest day found within the lookback window


def current_streak(conn: sqlite3.Connection, lookback_days: int = 60) -> dict:
    since = _date_n_days_ago(lookback_days)
    training_dates = {
        r["start_date"]
        for r in conn.execute(
            "SELECT DISTINCT start_date FROM sessions WHERE start_date >= ?", (since,)
        )
    }
    today_trained = date.today().isoformat() in training_dates
    streak_type = "training" if today_trained else "rest"
    count = 0
    cur = date.today()
    for offset in range(lookback_days):
        d = (cur - timedelta(days=offset)).isoformat()
        trained = d in training_dates
        if (streak_type == "training") == trained:
            count += 1
        else:
            break
    return {"type": streak_type, "days": count}


def recent_journal(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    rows = conn.execute(
        "SELECT date, text FROM journal_entries WHERE date >= ? ORDER BY date DESC",
        (_date_n_days_ago(days),),
    ).fetchall()
    return [{"date": r["date"], "text": r["text"]} for r in rows]


def calendar_outlook(
    conn: sqlite3.Connection, days_back: int = 7, days_ahead: int = 28
) -> dict:
    """Hand-entered calendar around today: what's planned or blocked soon, races
    further out, and whether recently planned trainings
    actually happened (a Garmin session logged that day)."""
    today = date.today()
    start = (today - timedelta(days=days_back)).isoformat()
    end = (today + timedelta(days=days_ahead)).isoformat()
    rows = conn.execute(
        "SELECT * FROM calendar_events WHERE COALESCE(end_date, date) >= ? AND date <= ? "
        "ORDER BY date, time IS NULL, time, id",
        (start, end),
    ).fetchall()
    session_dates = {
        r["start_date"]
        for r in conn.execute(
            "SELECT DISTINCT start_date FROM sessions WHERE start_date >= ?", (start,)
        )
    }
    today_iso = today.isoformat()

    events = []
    for r in rows:
        ev = {
            k: r[k]
            for k in ("id", "kind", "date", "end_date", "time", "title", "reason", "notes")
            if r[k] is not None
        }
        last_day = r["end_date"] or r["date"]
        if last_day < today_iso:
            ev["status"] = "past"
            if r["kind"] == "training":
                ev["done"] = r["date"] in session_dates
        elif r["date"] <= today_iso:
            ev["status"] = "today" if last_day == today_iso and r["date"] == today_iso else "ongoing"
        else:
            ev["status"] = "upcoming"
            ev["in_days"] = (date.fromisoformat(r["date"]) - today).days
        events.append(ev)

    # Races beyond the window still shape training now (build-up, taper), so
    # list them all for the coming year.
    later_races = conn.execute(
        "SELECT date, end_date, title, notes FROM calendar_events "
        "WHERE kind = 'race' AND date > ? AND date <= ? ORDER BY date LIMIT 10",
        (end, (today + timedelta(days=365)).isoformat()),
    ).fetchall()
    return {
        "events": events,
        "later_races": [
            {
                **{k: v for k, v in dict(r).items() if v is not None},
                "in_days": (date.fromisoformat(r["date"]) - today).days,
            }
            for r in later_races
        ],
    }
