"""On-demand session lookups and calendar edits the DeepSeek coach can call as tools.

The default system prompt only carries aggregates (load, sleep, streak), which
keeps every turn cheap. When the athlete asks about a specific session, the
model calls find_sessions to locate it and get_session_details to pull the
full breakdown -- so per-session detail only costs tokens when it's asked for.
"""

from __future__ import annotations

import json
import sqlite3

from pydantic import ValidationError

from app.calendar_schema import CalendarEvent
from app.sailing.reports import coach_session_view
from app.db import (
    CALENDAR_FIELDS,
    delete_calendar_event,
    get_calendar_event,
    get_session,
    insert_calendar_event,
    list_calendar_events,
    list_daily_metrics,
    list_exercise_sets,
    list_sessions,
    update_calendar_event,
)

FIND_SESSIONS_LIMIT_MAX = 30

# Fields from the raw Garmin activity dict worth showing the coach; the rest
# (profile image URLs, device ids, boolean flags) is noise.
RAW_DETAIL_FIELDS = [
    "movingDuration",
    "elapsedDuration",
    "elevationLoss",
    "minTemperature",
    "maxTemperature",
    "aerobicTrainingEffect",
    "anaerobicTrainingEffect",
    "trainingEffectLabel",
    "activityTrainingLoad",
    "avgRespirationRate",
    "maxRespirationRate",
    "moderateIntensityMinutes",
    "vigorousIntensityMinutes",
    "lapCount",
    "waterEstimated",
    "averageRunningCadenceInStepsPerMinute",
    "avgPower",
    "maxPower",
    "normPower",
    "totalSets",
    "activeSets",
    "totalReps",
]

_EVENT_PROPERTIES = {
    "kind": {
        "type": "string",
        "enum": ["training", "race", "plan", "blocked"],
        "description": "blocked = a day the athlete can't train",
    },
    "date": {"type": "string", "description": "YYYY-MM-DD (first day)"},
    "end_date": {"type": "string", "description": "YYYY-MM-DD, last day of a multi-day entry"},
    "time": {"type": "string", "description": "HH:MM, optional"},
    "title": {"type": "string", "description": "Short, e.g. 'Intervals 6x3 min Z4'"},
    "reason": {
        "type": "string",
        "enum": ["exam", "friends", "party", "other"],
        "description": "Only for kind=blocked",
    },
    "notes": {"type": "string", "description": "Optional details: structure, targets, why"},
}

# A runaway model shouldn't be able to flood the calendar in one reply.
MAX_CALENDAR_WRITES_PER_TURN = 40

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "find_sessions",
            "description": (
                "List the athlete's logged sessions (one line each: id, date, time, type, "
                "name, duration, distance, location). Use this to locate the session the "
                "athlete is asking about, e.g. 'yesterday's windsurf' or 'my last gym session'. "
                "Only call it when they ask about specific sessions -- not for general questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                    "activity_type": {
                        "type": "string",
                        "description": (
                            "Exact Garmin type key, e.g. windsurfing_v2, kiteboarding_v2, "
                            "strength_training, road_biking, running"
                        ),
                    },
                    "limit": {"type": "integer", "description": "Max rows, newest first (default 10)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session_details",
            "description": (
                "Full detail for one session by activity_id: HR and HR zones, speed, training "
                "effect and load, temperature, strength sets, the journal notes for that day and "
                "that night's sleep/HRV/body battery. Water sessions with a GPS analysis also get "
                "gps_analysis: tacks and gybes (count, % completed on the foil, metres lost, min "
                "speed), upwind/downwind VMG, angles and speeds, % foiling, top speeds, port vs "
                "starboard, gear, wind band, % vs the athlete's baseline in that wind band and "
                "the stored coach analysis. Speeds in knots; for angle_up_deg lower is better. "
                "Call it only when the athlete wants a specific session analysed."
            ),
            "parameters": {
                "type": "object",
                "properties": {"activity_id": {"type": "integer"}},
                "required": ["activity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_calendar_events",
            "description": (
                "The athlete's own calendar entries overlapping a date range: planned "
                "trainings, races, plans, and days they can't train (with reason: exam, "
                "friends, party, other). The briefing already covers last week to 4 weeks "
                "ahead -- call this only for dates outside that window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_calendar_event",
            "description": (
                "Add one entry to the athlete's calendar. Only call it when the athlete asks "
                "you to put something in the calendar, or says yes to a plan you offered to "
                "save. Call it once per entry (several calls in one turn are fine)."
            ),
            "parameters": {
                "type": "object",
                "properties": {**_EVENT_PROPERTIES},
                "required": ["kind", "date", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_calendar_event",
            "description": (
                "Change an existing calendar entry by its id (shown as [#id] in the briefing "
                "or returned by find_calendar_events), e.g. move it to another date or rename "
                "it. Pass only the fields to change; set a field to null to clear it."
            ),
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, **_EVENT_PROPERTIES},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": (
                "Delete a calendar entry by its id. Only when the athlete explicitly asks "
                "for it to be removed."
            ),
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
    },
]

FIND_CALENDAR_LIMIT_MAX = 100


def _summary_line(row: sqlite3.Row) -> dict:
    return {
        "activity_id": row["activity_id"],
        "start": row["start_time_local"],
        "type": row["activity_type"],
        "name": row["activity_name"],
        "duration_min": round(row["duration_s"] / 60) if row["duration_s"] else None,
        "distance_km": round(row["distance_m"] / 1000, 1) if row["distance_m"] else None,
        "location": row["location"],
    }


def find_sessions(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    activity_type: str | None = None,
    limit: int = 10,
) -> dict:
    limit = max(1, min(int(limit or 10), FIND_SESSIONS_LIMIT_MAX))
    rows = list_sessions(
        conn, start=start_date, end=end_date, activity_type=activity_type, limit=limit
    )
    return {"sessions": [_summary_line(r) for r in rows]}


def get_session_details(conn: sqlite3.Connection, activity_id: int) -> dict:
    row = get_session(conn, int(activity_id))
    if row is None:
        return {"error": f"no session with activity_id {activity_id}"}
    raw = json.loads(row["raw_json"])

    detail = _summary_line(row)
    detail.update(
        {
            "elevation_gain_m": row["elevation_gain_m"],
            "avg_speed_kmh": round(row["avg_speed_mps"] * 3.6, 1) if row["avg_speed_mps"] else None,
            "max_speed_kmh": round(row["max_speed_mps"] * 3.6, 1) if row["max_speed_mps"] else None,
            "avg_hr": row["avg_hr"],
            "max_hr": row["max_hr"],
            "calories": row["calories"],
        }
    )
    detail.update({k: raw[k] for k in RAW_DETAIL_FIELDS if raw.get(k) is not None})
    zones = {
        f"z{i}_min": round(raw[f"hrTimeInZone_{i}"] / 60, 1)
        for i in range(1, 6)
        if raw.get(f"hrTimeInZone_{i}") is not None
    }
    if zones:
        detail["hr_time_in_zone"] = zones

    if row["activity_type"] == "strength_training":
        # Rows without an exercise name are the rest periods between sets.
        detail["sets"] = [
            {
                "exercise": s["exercise_name"],
                "reps": s["reps"],
                "weight_kg": s["weight_kg"],
                "duration_s": round(s["duration_s"]) if s["duration_s"] else None,
            }
            for s in list_exercise_sets(conn, row["activity_id"])
            if s["exercise_name"]
        ]

    day = row["start_date"]
    detail["journal"] = [
        e["text"]
        for e in conn.execute(
            "SELECT text FROM journal_entries WHERE date = ? OR activity_id = ? ORDER BY id",
            (day, row["activity_id"]),
        )
    ]
    metrics = list_daily_metrics(conn, start=day, end=day)
    if metrics:
        detail["that_day_metrics"] = {
            k: v for k, v in dict(metrics[0]).items() if v is not None and k != "updated_at"
        }
    gps = coach_session_view(conn, row["activity_id"])
    if gps:
        detail["gps_analysis"] = gps
    return detail


def _event_brief(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in ("id", *CALENDAR_FIELDS) if row[k] is not None}


def find_calendar_events(conn: sqlite3.Connection, start_date: str, end_date: str) -> dict:
    rows = list_calendar_events(conn, start=start_date, end=end_date)[:FIND_CALENDAR_LIMIT_MAX]
    return {"events": [_event_brief(r) for r in rows]}


def _validated(fields: dict) -> dict:
    return CalendarEvent(**fields).model_dump()


def _check_write_budget(changes: list) -> None:
    if len(changes) >= MAX_CALENDAR_WRITES_PER_TURN:
        raise ValueError(f"at most {MAX_CALENDAR_WRITES_PER_TURN} calendar changes per reply")


def add_calendar_event(conn: sqlite3.Connection, changes: list, **fields) -> dict:
    _check_write_budget(changes)
    event_id = insert_calendar_event(conn, _validated(fields))
    after = dict(get_calendar_event(conn, event_id))
    changes.append({"action": "added", "event": after})
    return {"added": _event_brief(after)}


def update_calendar_event_tool(
    conn: sqlite3.Connection, changes: list, id: int, **fields
) -> dict:
    _check_write_budget(changes)
    row = get_calendar_event(conn, int(id))
    if row is None:
        return {"error": f"no calendar entry with id {id}"}
    before = dict(row)
    merged = {k: before[k] for k in CALENDAR_FIELDS}
    merged.update({k: v for k, v in fields.items() if k in CALENDAR_FIELDS})
    update_calendar_event(conn, before["id"], _validated(merged))
    after = dict(get_calendar_event(conn, before["id"]))
    changes.append({"action": "updated", "event": after, "previous": before})
    return {"updated": _event_brief(after)}


def delete_calendar_event_tool(conn: sqlite3.Connection, changes: list, id: int) -> dict:
    _check_write_budget(changes)
    row = get_calendar_event(conn, int(id))
    if row is None:
        return {"error": f"no calendar entry with id {id}"}
    delete_calendar_event(conn, row["id"])
    changes.append({"action": "deleted", "previous": dict(row)})
    return {"deleted": _event_brief(row)}


_EXECUTORS = {
    "find_sessions": find_sessions,
    "get_session_details": get_session_details,
    "find_calendar_events": find_calendar_events,
}

# Tools that change the calendar get the per-reply change log as well, so the
# chat can show (and undo) exactly what the coach did.
_WRITE_EXECUTORS = {
    "add_calendar_event": add_calendar_event,
    "update_calendar_event": update_calendar_event_tool,
    "delete_calendar_event": delete_calendar_event_tool,
}


def run_tool(
    conn: sqlite3.Connection, name: str, arguments: str, changes: list | None = None
) -> str:
    """Execute one model tool call and return its JSON result as a string.
    Errors are returned to the model rather than raised, so it can recover."""
    try:
        args = json.loads(arguments or "{}")
        if name in _WRITE_EXECUTORS:
            if changes is None:
                return json.dumps({"error": "calendar changes aren't allowed here"})
            result = _WRITE_EXECUTORS[name](conn, changes, **args)
        elif name in _EXECUTORS:
            result = _EXECUTORS[name](conn, **args)
        else:
            return json.dumps({"error": f"unknown tool {name}"})
        return json.dumps(result, ensure_ascii=False, default=str)
    except ValidationError as e:
        msgs = "; ".join(str(err["msg"]).removeprefix("Value error, ") for err in e.errors())
        return json.dumps({"error": f"invalid calendar entry: {msgs}"})
    except (TypeError, ValueError) as e:
        return json.dumps({"error": str(e)})
