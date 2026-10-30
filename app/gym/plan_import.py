"""Load gym_plan.json into the database. Idempotent: every row is upserted by
its natural key (plan name + start date, day letter, exercise key, day +
order, location id), so running it again changes nothing.

    venv/bin/python -m app.gym.plan_import [path/to/gym_plan.json]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import PROJECT_ROOT

DEFAULT_PLAN_PATH = PROJECT_ROOT / "gym_plan.json"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

ITEM_FIELDS = [
    "sets", "reps_min", "reps_max", "weight_kg", "weight_kg_min", "weight_kg_max",
    "duration_sec", "duration_sec_min", "duration_sec_max", "rest_sec", "superset_group", "notes",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upsert(conn: sqlite3.Connection, table: str, keys: dict, values: dict) -> None:
    """INSERT or UPDATE by `keys`; the UPDATE only fires when a value differs,
    so a re-import leaves unchanged rows (and conn.total_changes) alone."""
    cols = [*keys, *values]
    sets = ", ".join(f"{c} = excluded.{c}" for c in values)
    guard = f"({', '.join(f'{table}.{c}' for c in values)}) IS NOT ({', '.join(f'excluded.{c}' for c in values)})"
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {sets} WHERE {guard}",
        [*keys.values(), *values.values()],
    )


def import_plan(conn: sqlite3.Connection, data: dict) -> int:
    """Upsert the plan in `data` (the parsed JSON) and return its plan id."""
    plan = data["plan"]

    for loc in data.get("locations", []):
        now = _now()
        conn.execute(
            """INSERT INTO gym_locations (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
               WHERE gym_locations.name IS NOT excluded.name""",
            (loc["id"], loc["name"], now, now),
        )

    source = json.dumps(data, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """INSERT INTO gym_plans (name, start_date, weeks, source_json, updated_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(name, start_date) DO UPDATE SET weeks = excluded.weeks,
             source_json = excluded.source_json, updated_at = excluded.updated_at
           WHERE (gym_plans.weeks, gym_plans.source_json) IS NOT (excluded.weeks, excluded.source_json)""",
        (plan["name"], plan["start_date"], plan.get("weeks"), source, _now()),
    )
    plan_id = conn.execute(
        "SELECT id FROM gym_plans WHERE name = ? AND start_date = ?", (plan["name"], plan["start_date"])
    ).fetchone()[0]

    for key, ex in data["exercises"].items():
        _upsert(conn, "gym_exercises", {"id": key}, {
            "name_lt": ex["name_lt"], "name_en": ex.get("name_en"), "type": ex["type"],
            "per_side": int(bool(ex.get("per_side"))), "weight_mode": ex.get("weight_mode"),
            "notes": ex.get("notes"),
        })

    schedule = {s["workout_id"]: s for s in data.get("schedule", {}).get("sessions", [])}
    for w in data["workouts"]:
        sched = schedule.get(w["id"], {})
        _upsert(conn, "gym_plan_days", {"plan_id": plan_id, "code": w["id"]}, {
            "name": w.get("name"),
            "weekday": WEEKDAYS.index((sched.get("weekday") or w["weekday"]).lower()),
            "default_time": sched.get("default_time"),
            "default_location_id": sched.get("default_location"),
            "focus": w.get("focus"),
            "duration_min": w.get("duration_min"),
            "warmup_min": w.get("warmup_min"),
            "cooldown": w.get("cooldown"),
        })
        day_id = conn.execute(
            "SELECT id FROM gym_plan_days WHERE plan_id = ? AND code = ?", (plan_id, w["id"])
        ).fetchone()[0]

        positions = []
        for item in w["items"]:
            positions.append(item["order"])
            _upsert(conn, "gym_plan_exercises", {"plan_day_id": day_id, "position": item["order"]},
                    {"exercise_id": item["exercise"], **{f: item.get(f) for f in ITEM_FIELDS}})
        # Items dropped from the file disappear from the day; logged sets point
        # at exercises, not plan items, so history is unaffected.
        conn.execute(
            f"DELETE FROM gym_plan_exercises WHERE plan_day_id = ? AND position NOT IN ({', '.join('?' for _ in positions)})",
            (day_id, *positions),
        )
    conn.commit()
    return plan_id


def counts(conn: sqlite3.Connection, plan_id: int) -> dict:
    per_day = {
        r["code"]: r["n"]
        for r in conn.execute(
            """SELECT d.code, COUNT(pe.id) AS n FROM gym_plan_days d
               LEFT JOIN gym_plan_exercises pe ON pe.plan_day_id = d.id
               WHERE d.plan_id = ? GROUP BY d.id ORDER BY d.code""",
            (plan_id,),
        )
    }
    return {
        "plan_days": len(per_day),
        "exercises": conn.execute("SELECT COUNT(*) FROM gym_exercises").fetchone()[0],
        "items_per_day": per_day,
        "locations": conn.execute("SELECT COUNT(*) FROM gym_locations").fetchone()[0],
    }


def main(argv: list[str]) -> None:
    from app.db import get_connection, init_db

    path = Path(argv[0]) if argv else DEFAULT_PLAN_PATH
    conn = get_connection()
    init_db(conn)
    before = conn.total_changes
    plan_id = import_plan(conn, json.loads(path.read_text()))
    changed = conn.total_changes - before
    print(f"Imported {path.name} into plan #{plan_id} ({changed} row changes)")
    print(json.dumps(counts(conn, plan_id), ensure_ascii=False))
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
