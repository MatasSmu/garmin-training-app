"""Gym tracker data access: the plan as the phone needs it, idempotent sync of
logged workouts, history/analysis queries and the Garmin activity link.

Times are stored as ISO 8601 UTC ('2026-11-02T13:05:00Z'); dates shown to the
athlete (and plan weeks) use the server's local time zone."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

GARMIN_MATCH_SLACK = timedelta(minutes=30)
GARMIN_STRENGTH_TYPES = ("strength_training",)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive = server local time
    return dt.astimezone(timezone.utc)


def norm_ts(value: str | None) -> str | None:
    """Any ISO timestamp -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC), so strings sort as times."""
    dt = parse_ts(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def norm_ts_ms(value: str | None) -> str | None:
    """Like norm_ts but keeps milliseconds: used for updated_at, where two
    edits within one second must still compare as different."""
    dt = parse_ts(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z" if dt else None


def local_date(ts: str | None) -> date | None:
    dt = parse_ts(ts)
    return dt.astimezone().date() if dt else None


def e1rm(weight: float | None, reps: int | None) -> float | None:
    """Epley estimated one-rep max."""
    if not weight or not reps:
        return None
    return weight * (1 + reps / 30)


# --------------------------------------------------------------------- plan


def current_plan(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM gym_plans ORDER BY start_date DESC, id DESC LIMIT 1").fetchone()


def plan_week(plan: sqlite3.Row | dict | None, day: date | None) -> int | None:
    if plan is None or day is None:
        return None
    return (day - date.fromisoformat(plan["start_date"])).days // 7 + 1


def exercises(conn: sqlite3.Connection) -> dict[str, dict]:
    return {
        r["id"]: {**dict(r), "per_side": bool(r["per_side"])}
        for r in conn.execute("SELECT * FROM gym_exercises ORDER BY id")
    }


def plan_days(conn: sqlite3.Connection, plan_id: int) -> list[dict]:
    days = [dict(r) for r in conn.execute("SELECT * FROM gym_plan_days WHERE plan_id = ? ORDER BY code", (plan_id,))]
    items: dict[int, list] = {}
    for r in conn.execute(
        """SELECT pe.* FROM gym_plan_exercises pe JOIN gym_plan_days d ON d.id = pe.plan_day_id
           WHERE d.plan_id = ? ORDER BY pe.plan_day_id, pe.position""",
        (plan_id,),
    ):
        items.setdefault(r["plan_day_id"], []).append({k: r[k] for k in r.keys() if k not in ("id", "plan_day_id")})
    for d in days:
        d["items"] = items.get(d["id"], [])
    return days


def locations(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, name FROM gym_locations ORDER BY created_at, name")]


def last_numbers(conn: sqlite3.Connection, exclude_session: str | None = None) -> dict[str, dict]:
    """Per exercise, the sets from the most recent workout that logged it
    (on any plan day -- bench press is on A and D)."""
    rows = conn.execute(
        """WITH latest AS (
             SELECT sl.exercise_id, s.id AS session_id, s.started_at,
                    ROW_NUMBER() OVER (PARTITION BY sl.exercise_id ORDER BY s.started_at DESC, s.id) AS rn
             FROM gym_set_logs sl JOIN gym_sessions s ON s.id = sl.session_id
             WHERE s.id IS NOT ? AND s.started_at IS NOT NULL
             GROUP BY sl.exercise_id, s.id)
           SELECT l.exercise_id, l.session_id, l.started_at, d.code AS day_code,
                  sl.set_number, sl.reps, sl.weight_kg, sl.duration_sec
           FROM latest l
           JOIN gym_sessions s ON s.id = l.session_id
           LEFT JOIN gym_plan_days d ON d.id = s.plan_day_id
           JOIN gym_set_logs sl ON sl.session_id = l.session_id AND sl.exercise_id = l.exercise_id
           WHERE l.rn = 1
           ORDER BY l.exercise_id, sl.set_number""",
        (exclude_session,),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        entry = out.setdefault(r["exercise_id"], {
            "session_id": r["session_id"], "started_at": r["started_at"], "day_code": r["day_code"], "sets": [],
        })
        entry["sets"].append({k: r[k] for k in ("set_number", "reps", "weight_kg", "duration_sec")})
    return out


def bootstrap(conn: sqlite3.Connection) -> dict:
    """Everything the phone caches to log a workout offline."""
    plan = current_plan(conn)
    return {
        "plan": {k: plan[k] for k in ("id", "name", "start_date", "weeks")} if plan else None,
        "days": plan_days(conn, plan["id"]) if plan else [],
        "exercises": exercises(conn),
        "locations": locations(conn),
        "last": last_numbers(conn),
        "server_time": _now(),
    }


# --------------------------------------------------------------------- sync


def upsert_location(conn: sqlite3.Connection, loc_id: str, name: str) -> None:
    now = _now()
    conn.execute(
        """INSERT INTO gym_locations (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
           WHERE gym_locations.name IS NOT excluded.name""",
        (loc_id, name.strip(), now, now),
    )


def _exists(conn: sqlite3.Connection, table: str, value) -> bool:
    return value is not None and conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (value,)).fetchone() is not None


def upsert_session(conn: sqlite3.Connection, s: dict) -> str:
    """Store one session with its full set list. Returns 'created', 'updated',
    'unchanged' or 'stale' (the server copy was edited later; kept as is).
    Sets of the session missing from `s['sets']` are deleted -- the phone always
    sends the complete list, so an un-ticked set disappears on the server too."""
    incoming = norm_ts_ms(s["updated_at"])
    row = conn.execute("SELECT client_updated_at FROM gym_sessions WHERE id = ?", (s["id"],)).fetchone()
    if row is not None:
        if row["client_updated_at"] > incoming:
            return "stale"
        if row["client_updated_at"] == incoming:
            return "unchanged"

    now = _now()
    plan_day_id = s.get("plan_day_id") if _exists(conn, "gym_plan_days", s.get("plan_day_id")) else None
    location_id = s.get("location_id") if _exists(conn, "gym_locations", s.get("location_id")) else None
    conn.execute(
        """INSERT INTO gym_sessions (id, plan_day_id, location_id, started_at, ended_at, notes,
                                     client_updated_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET plan_day_id = excluded.plan_day_id, location_id = excluded.location_id,
             started_at = excluded.started_at, ended_at = excluded.ended_at, notes = excluded.notes,
             client_updated_at = excluded.client_updated_at, updated_at = excluded.updated_at""",
        (s["id"], plan_day_id, location_id, norm_ts(s.get("started_at")), norm_ts(s.get("ended_at")),
         (s.get("notes") or "").strip() or None, incoming, now, now),
    )
    known = set(exercises(conn))
    sets = [x for x in s.get("sets", []) if x["exercise_id"] in known]
    ids = [x["id"] for x in sets]
    conn.execute(
        f"DELETE FROM gym_set_logs WHERE session_id = ? AND id NOT IN ({', '.join('?' for _ in ids)})",
        (s["id"], *ids),
    )
    for x in sets:
        conn.execute(
            """INSERT INTO gym_set_logs (id, session_id, exercise_id, set_number, reps, weight_kg, duration_sec,
                                         rpe, completed_as_planned, done_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET session_id = excluded.session_id, exercise_id = excluded.exercise_id,
                 set_number = excluded.set_number, reps = excluded.reps, weight_kg = excluded.weight_kg,
                 duration_sec = excluded.duration_sec, rpe = excluded.rpe,
                 completed_as_planned = excluded.completed_as_planned, done_at = excluded.done_at""",
            (x["id"], s["id"], x["exercise_id"], x["set_number"], x.get("reps"), x.get("weight_kg"),
             x.get("duration_sec"), x.get("rpe"), int(bool(x.get("completed_as_planned"))), norm_ts(x.get("done_at"))),
        )
    return "updated" if row is not None else "created"


def sync(conn: sqlite3.Connection, payload: dict) -> dict:
    for loc in payload.get("locations", []):
        upsert_location(conn, loc["id"], loc["name"])
    results = {}
    for s in payload.get("sessions", []):
        results[s["id"]] = upsert_session(conn, s)
    link_garmin(conn)
    conn.commit()
    # Sessions the server kept a newer copy of go back to the phone.
    stale = [sid for sid, st in results.items() if st == "stale"]
    return {
        "results": results,
        "server_copies": [session_payload(conn, sid) for sid in stale],
        "locations": locations(conn),
        "last": last_numbers(conn),
    }


def session_payload(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """A session in the same shape the phone syncs it in."""
    s = conn.execute("SELECT * FROM gym_sessions WHERE id = ?", (session_id,)).fetchone()
    if s is None:
        return None
    sets = conn.execute(
        """SELECT id, exercise_id, set_number, reps, weight_kg, duration_sec, rpe, completed_as_planned, done_at
           FROM gym_set_logs WHERE session_id = ? ORDER BY exercise_id, set_number""",
        (session_id,),
    ).fetchall()
    return {
        "id": s["id"], "plan_day_id": s["plan_day_id"], "location_id": s["location_id"],
        "started_at": s["started_at"], "ended_at": s["ended_at"], "notes": s["notes"],
        "updated_at": s["client_updated_at"],
        "sets": [{**dict(x), "completed_as_planned": bool(x["completed_as_planned"])} for x in sets],
    }


SESSION_EDITABLE = ("location_id", "started_at", "ended_at", "notes", "plan_day_id")


def update_session(conn: sqlite3.Connection, session_id: str, fields: dict) -> bool:
    """Edit from the laptop (Gym tab). Bumps client_updated_at so an older phone
    copy syncing later can't overwrite it."""
    row = conn.execute("SELECT client_updated_at FROM gym_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return False
    values = {k: v for k, v in fields.items() if k in SESSION_EDITABLE}
    for k in ("started_at", "ended_at"):
        if k in values:
            values[k] = norm_ts(values[k])
    if "notes" in values:
        values["notes"] = (values["notes"] or "").strip() or None
    if "location_id" in values and values["location_id"] is not None and not _exists(conn, "gym_locations", values["location_id"]):
        raise ValueError("unknown location")
    if "plan_day_id" in values and values["plan_day_id"] is not None and not _exists(conn, "gym_plan_days", values["plan_day_id"]):
        raise ValueError("unknown plan day")
    now = _now()
    # Strictly newer than the copy it replaces, even if the phone's clock runs ahead.
    bumped = norm_ts_ms((parse_ts(row["client_updated_at"]) + timedelta(milliseconds=1)).isoformat())
    values.update({"client_updated_at": max(norm_ts_ms(now), bumped), "updated_at": now})
    if {"started_at", "ended_at"} & fields.keys():
        values["garmin_activity_id"] = None  # times moved: match again
    conn.execute(
        f"UPDATE gym_sessions SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
        (*values.values(), session_id),
    )
    link_garmin(conn)
    conn.commit()
    return True


# --------------------------------------------------------------------- Garmin link


def _garmin_candidates(conn: sqlite3.Connection, day: date) -> list[dict]:
    placeholders = ", ".join("?" for _ in GARMIN_STRENGTH_TYPES)
    rows = conn.execute(
        f"""SELECT activity_id, json_extract(raw_json, '$.startTimeGMT') AS start_gmt, start_time_local,
                   duration_s, avg_hr, max_hr
            FROM sessions WHERE activity_type IN ({placeholders}) AND start_date BETWEEN ? AND ?""",
        (*GARMIN_STRENGTH_TYPES, (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()),
    ).fetchall()
    out = []
    for r in rows:
        if r["start_gmt"]:
            start = datetime.fromisoformat(r["start_gmt"]).replace(tzinfo=timezone.utc)
        else:
            start = datetime.fromisoformat(r["start_time_local"]).astimezone(timezone.utc)
        out.append({**dict(r), "start": start, "end": start + timedelta(seconds=r["duration_s"] or 0)})
    return out


def link_garmin(conn: sqlite3.Connection) -> int:
    """Link finished workouts to the Garmin strength activity overlapping
    [start - 30 min, end + 30 min] (largest overlap wins; one activity per
    workout). Garmin data usually arrives after the workout, so this runs on
    every sync and history read. Returns how many links were made."""
    taken = {r[0] for r in conn.execute("SELECT garmin_activity_id FROM gym_sessions WHERE garmin_activity_id IS NOT NULL")}
    made = 0
    for s in conn.execute(
        """SELECT id, started_at, ended_at FROM gym_sessions
           WHERE garmin_activity_id IS NULL AND started_at IS NOT NULL AND ended_at IS NOT NULL"""
    ).fetchall():
        start, end = parse_ts(s["started_at"]), parse_ts(s["ended_at"])
        lo, hi = start - GARMIN_MATCH_SLACK, end + GARMIN_MATCH_SLACK
        best, best_overlap = None, timedelta(0)
        for g in _garmin_candidates(conn, start.astimezone().date()):
            if g["activity_id"] in taken:
                continue
            overlap = min(hi, g["end"]) - max(lo, g["start"])
            if overlap > best_overlap or (overlap == best_overlap and best is not None
                                          and abs(g["start"] - start) < abs(best["start"] - start)):
                best, best_overlap = g, overlap
        if best is not None and best_overlap > timedelta(0):
            conn.execute("UPDATE gym_sessions SET garmin_activity_id = ? WHERE id = ?", (best["activity_id"], s["id"]))
            taken.add(best["activity_id"])
            made += 1
    return made


# --------------------------------------------------------------------- analysis


def _set_volume(x, ex: dict) -> float:
    if not x["weight_kg"] or not x["reps"]:
        return 0.0
    return x["reps"] * x["weight_kg"] * (2 if ex.get("per_side") else 1)


def _planned_items(conn: sqlite3.Connection, plan_day_id: int | None) -> list[sqlite3.Row]:
    if plan_day_id is None:
        return []
    return conn.execute(
        "SELECT * FROM gym_plan_exercises WHERE plan_day_id = ? ORDER BY position", (plan_day_id,)
    ).fetchall()


def _summary(conn: sqlite3.Connection, s: sqlite3.Row, ex: dict, plan) -> dict:
    sets = conn.execute("SELECT * FROM gym_set_logs WHERE session_id = ?", (s["id"],)).fetchall()
    items = _planned_items(conn, s["plan_day_id"])
    reps_items = {i["exercise_id"]: i for i in items if i["reps_min"]}
    sets_planned = sum(i["sets"] for i in items)
    reps_target = sum(i["sets"] * i["reps_min"] for i in reps_items.values())
    reps_done = sum((x["reps"] or 0) for x in sets if x["exercise_id"] in reps_items)
    start, end = parse_ts(s["started_at"]), parse_ts(s["ended_at"])
    day = local_date(s["started_at"])
    out = {
        "id": s["id"],
        "day_code": s["day_code"],
        "plan_day_id": s["plan_day_id"],
        "location_id": s["location_id"],
        "location": s["location_name"],
        "started_at": s["started_at"],
        "ended_at": s["ended_at"],
        "date": day.isoformat() if day else None,
        "plan_week": plan_week(plan, day),
        "duration_min": round((end - start).total_seconds() / 60) if start and end else None,
        "notes": s["notes"],
        "sets_done": len(sets),
        "sets_planned": sets_planned,
        "sets_as_planned": sum(1 for x in sets if x["completed_as_planned"]),
        "reps_done": reps_done,
        "reps_target": reps_target,
        "volume_kg": round(sum(_set_volume(x, ex.get(x["exercise_id"], {})) for x in sets)),
        "garmin": None,
    }
    if s["garmin_activity_id"]:
        g = conn.execute(
            "SELECT activity_id, activity_name, duration_s, avg_hr, max_hr, calories FROM sessions WHERE activity_id = ?",
            (s["garmin_activity_id"],),
        ).fetchone()
        if g:
            out["garmin"] = {
                "activity_id": g["activity_id"], "name": g["activity_name"],
                "duration_min": round(g["duration_s"] / 60) if g["duration_s"] else None,
                "avg_hr": g["avg_hr"], "max_hr": g["max_hr"], "calories": g["calories"],
            }
    return out


SESSION_SELECT = """SELECT s.*, d.code AS day_code, l.name AS location_name FROM gym_sessions s
                    LEFT JOIN gym_plan_days d ON d.id = s.plan_day_id
                    LEFT JOIN gym_locations l ON l.id = s.location_id"""


def list_sessions(conn: sqlite3.Connection, limit: int = 100, since: str | None = None) -> list[dict]:
    if link_garmin(conn):
        conn.commit()
    ex, plan = exercises(conn), current_plan(conn)
    query, params = SESSION_SELECT, []
    if since:
        query += " WHERE s.started_at >= ?"
        params.append(norm_ts(since))
    query += " ORDER BY s.started_at DESC LIMIT ?"
    params.append(limit)
    return [_summary(conn, s, ex, plan) for s in conn.execute(query, params).fetchall()]


def _target_text(i, ex: dict) -> str:
    def rng(lo, hi, unit=""):
        if lo is None and hi is None:
            return None
        if lo is None or hi is None or lo == hi:
            return f"{lo if lo is not None else hi}{unit}"
        return f"{lo}–{hi}{unit}"

    def num(v):
        return None if v is None else (int(v) if float(v).is_integer() else v)

    kg = rng(num(i["weight_kg_min"]), num(i["weight_kg_max"]), " kg") or (f"{num(i['weight_kg'])} kg" if i["weight_kg"] else None)
    if i["reps_min"] or i["reps_max"]:
        body = rng(i["reps_min"], i["reps_max"]) + (" /leg" if ex.get("per_side") else "")
    else:
        body = rng(i["duration_sec_min"], i["duration_sec_max"], " s") or (f"{i['duration_sec']} s" if i["duration_sec"] else "")
    return f"{i['sets']} × {body}" + (f" @ {kg}" if kg else "")


def session_detail(conn: sqlite3.Connection, session_id: str) -> dict | None:
    if link_garmin(conn):
        conn.commit()
    s = conn.execute(SESSION_SELECT + " WHERE s.id = ?", (session_id,)).fetchone()
    if s is None:
        return None
    ex, plan = exercises(conn), current_plan(conn)
    out = _summary(conn, s, ex, plan)
    sets = conn.execute(
        "SELECT * FROM gym_set_logs WHERE session_id = ? ORDER BY set_number", (session_id,)
    ).fetchall()
    by_ex: dict[str, list] = {}
    for x in sets:
        by_ex.setdefault(x["exercise_id"], []).append({
            "set_number": x["set_number"], "reps": x["reps"], "weight_kg": x["weight_kg"],
            "duration_sec": x["duration_sec"], "rpe": x["rpe"], "completed_as_planned": bool(x["completed_as_planned"]),
        })
    rows = []
    for i in _planned_items(conn, s["plan_day_id"]):
        e = ex.get(i["exercise_id"], {})
        done = by_ex.pop(i["exercise_id"], [])
        rows.append({
            "exercise_id": i["exercise_id"], "name_lt": e.get("name_lt"), "name_en": e.get("name_en"),
            "superset_group": i["superset_group"], "target": _target_text(i, e),
            "sets_planned": i["sets"], "reps_min": i["reps_min"], "sets": done,
        })
    for ex_id, done in by_ex.items():  # logged but not in that day's plan
        e = ex.get(ex_id, {})
        rows.append({"exercise_id": ex_id, "name_lt": e.get("name_lt"), "name_en": e.get("name_en"),
                     "superset_group": None, "target": None, "sets_planned": 0, "reps_min": None, "sets": done})
    out["exercises"] = rows
    return out


def exercise_list(conn: sqlite3.Connection) -> list[dict]:
    counts = dict(conn.execute(
        "SELECT exercise_id, COUNT(DISTINCT session_id) FROM gym_set_logs GROUP BY exercise_id"
    ).fetchall())
    return [{**e, "sessions": counts.get(k, 0)} for k, e in exercises(conn).items()]


def exercise_history(conn: sqlite3.Connection, exercise_id: str) -> dict | None:
    ex = exercises(conn).get(exercise_id)
    if ex is None:
        return None
    rows = conn.execute(
        """SELECT s.id AS session_id, s.started_at, d.code AS day_code,
                  sl.set_number, sl.reps, sl.weight_kg, sl.duration_sec, sl.rpe
           FROM gym_set_logs sl JOIN gym_sessions s ON s.id = sl.session_id
           LEFT JOIN gym_plan_days d ON d.id = s.plan_day_id
           WHERE sl.exercise_id = ? AND s.started_at IS NOT NULL
           ORDER BY s.started_at, sl.set_number""",
        (exercise_id,),
    ).fetchall()
    sessions: dict[str, dict] = {}
    for r in rows:
        h = sessions.setdefault(r["session_id"], {
            "session_id": r["session_id"], "started_at": r["started_at"],
            "date": local_date(r["started_at"]).isoformat(), "day_code": r["day_code"], "sets": [], "rpe": None,
        })
        h["sets"].append({k: r[k] for k in ("set_number", "reps", "weight_kg", "duration_sec")})
        h["rpe"] = r["rpe"] or h["rpe"]
    out = []
    for h in sessions.values():
        sets = h["sets"]
        weights = [x["weight_kg"] for x in sets if x["weight_kg"]]
        e1 = [v for v in (e1rm(x["weight_kg"], x["reps"]) for x in sets) if v]
        out.append({
            **h,
            "top_weight_kg": max(weights) if weights else None,
            "e1rm_kg": round(max(e1), 1) if e1 else None,
            "total_reps": sum(x["reps"] or 0 for x in sets),
            "max_duration_sec": max((x["duration_sec"] or 0 for x in sets), default=0) or None,
            "volume_kg": round(sum(_set_volume(x, ex) for x in sets)),
        })
    return {"exercise": ex, "history": out}


def weekly_volume(conn: sqlite3.Connection) -> dict:
    """Per plan week (week 1 starts on the plan's start_date): sessions, sets,
    reps and volume (Σ reps × kg; per-side exercises count both legs)."""
    plan = current_plan(conn)
    ex = exercises(conn)
    weeks: dict[int, dict] = {}
    for s in conn.execute("SELECT id, started_at FROM gym_sessions WHERE started_at IS NOT NULL").fetchall():
        day = local_date(s["started_at"])
        if plan is not None:
            wk = plan_week(plan, day)
            wk_start = date.fromisoformat(plan["start_date"]) + timedelta(days=7 * (wk - 1))
        else:
            wk_start = day - timedelta(days=day.weekday())
            wk = int(wk_start.strftime("%G%V"))
        w = weeks.setdefault(wk, {"week": wk, "week_start": wk_start.isoformat(), "sessions": 0,
                                  "sets": 0, "reps": 0, "volume_kg": 0.0})
        w["sessions"] += 1
        for x in conn.execute("SELECT * FROM gym_set_logs WHERE session_id = ?", (s["id"],)):
            w["sets"] += 1
            w["reps"] += x["reps"] or 0
            w["volume_kg"] += _set_volume(x, ex.get(x["exercise_id"], {}))
    rows = sorted(weeks.values(), key=lambda w: w["week_start"])
    for w in rows:
        w["volume_kg"] = round(w["volume_kg"])
        w["in_plan"] = plan is not None and 1 <= w["week"] <= (plan["weeks"] or 0)
    info = {k: plan[k] for k in ("name", "start_date", "weeks")} if plan else None
    return {"plan": info, "weeks": rows}


# --------------------------------------------------------------------- coach


def coach_summary(conn: sqlite3.Connection, limit: int = 6) -> dict:
    """Compact recent-gym view for the coach briefing."""
    plan = current_plan(conn)
    if plan is None:
        return {}
    ex = exercises(conn)
    today = date.today()
    out = {
        "plan": plan["name"], "start_date": plan["start_date"], "weeks": plan["weeks"],
        "week_now": plan_week(plan, today),
        "schedule": [
            f"{d['code']} {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][d['weekday']]} {d['default_time'] or ''}".strip()
            for d in plan_days(conn, plan["id"])
        ],
        "sessions": [],
    }
    for s in list_sessions(conn, limit=limit):
        # Each exercise's heaviest set (then most reps / longest), heaviest exercises first.
        best: dict[str, sqlite3.Row] = {}
        for x in conn.execute("SELECT * FROM gym_set_logs WHERE session_id = ?", (s["id"],)):
            key = (x["weight_kg"] or 0, x["reps"] or 0, x["duration_sec"] or 0)
            cur = best.get(x["exercise_id"])
            if cur is None or key > (cur["weight_kg"] or 0, cur["reps"] or 0, cur["duration_sec"] or 0):
                best[x["exercise_id"]] = x
        top = sorted(best.values(), key=lambda x: -(x["weight_kg"] or 0))[:4]
        lifts = [
            f"{ex.get(t['exercise_id'], {}).get('name_en') or t['exercise_id']} "
            + (f"{t['weight_kg']:g} kg x{t['reps']}" if t["weight_kg"] and t["reps"] is not None
               else f"{t['reps']} reps" if t["reps"] else f"{t['duration_sec']} s")
            for t in top
        ]
        out["sessions"].append({
            "id": s["id"], "date": s["date"], "day": s["day_code"], "location": s["location"],
            "duration_min": s["duration_min"], "sets": f"{s['sets_done']}/{s['sets_planned']}",
            "reps": f"{s['reps_done']}/{s['reps_target']}", "volume_kg": s["volume_kg"],
            "avg_hr": s["garmin"]["avg_hr"] if s["garmin"] else None,
            "top": lifts, "notes": s["notes"],
        })
    return out


def plan_extras(conn: sqlite3.Connection) -> dict | None:
    """The plan file's parts that aren't logged here: cardio, HR zones, nutrition, rules."""
    plan = current_plan(conn)
    if plan is None:
        return None
    data = json.loads(plan["source_json"])
    days = plan_days(conn, plan["id"])
    ex = exercises(conn)
    return {
        "plan": data.get("plan"),
        "general_rules": data.get("general_rules"),
        "gym_days": [
            {"day": d["code"], "weekday": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][d["weekday"]],
             "time": d["default_time"], "focus": d["focus"],
             "items": [f"{ex[i['exercise_id']]['name_en']}: {_target_text(i, ex[i['exercise_id']])}"
                       + (f" (superset {i['superset_group']})" if i["superset_group"] else "")
                       + (f" rest {i['rest_sec']} s" if i["rest_sec"] else "") for i in d["items"]]}
            for d in days
        ],
        "cardio": data.get("cardio"),
        "hr_zones": data.get("hr_zones"),
        "nutrition": data.get("nutrition"),
    }
