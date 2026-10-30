"""The coach sees recent gym workouts in its briefing and can read the plan
(including cardio / HR zones / nutrition) and single workouts via tools."""

import json
import uuid

from app.coach.engine import build_context
from app.coach.responder import _context_briefing
from app.coach.tools import TOOL_SPECS, run_tool
from app.gym import store


def _log(conn, code="D", start="2026-11-07T08:45:00Z", end="2026-11-07T09:50:00Z"):
    day = conn.execute("SELECT id FROM gym_plan_days WHERE code = ?", (code,)).fetchone()[0]
    sid = str(uuid.uuid4())
    store.sync(conn, {"locations": [], "sessions": [{
        "id": sid, "plan_day_id": day, "location_id": "bsb", "started_at": start, "ended_at": end,
        "notes": "knee ok", "updated_at": end, "sets": [
            {"id": f"{sid}-1", "exercise_id": "back_squat", "set_number": 1, "reps": 5, "weight_kg": 100},
            {"id": f"{sid}-2", "exercise_id": "back_squat", "set_number": 2, "reps": 3, "weight_kg": 105},
            {"id": f"{sid}-3", "exercise_id": "back_squat", "set_number": 3, "reps": 6, "weight_kg": 95},
        ]}]})
    return sid


def test_briefing_lists_recent_gym_workouts(conn):
    _log(conn)
    text = _context_briefing(build_context(conn))
    assert "Gym plan 'Žiemos planas – 8 savaitės'" in text
    assert "2026-11-07 day D, Sporto salė (BSB), 65 min, sets 3/18" in text
    assert "Back squat 105 kg x3" in text  # heaviest set with its own reps
    assert "note: knee ok" in text


def test_briefing_without_workouts(conn):
    assert "No gym workouts logged" in _context_briefing(build_context(conn))


def test_gym_tools(conn):
    sid = _log(conn)
    names = {t["function"]["name"] for t in TOOL_SPECS}
    assert {"find_gym_workouts", "get_gym_workout", "get_gym_plan", "get_gym_exercise_history"} <= names

    found = json.loads(run_tool(conn, "find_gym_workouts", '{"start_date": "2026-11-01"}'))["workouts"]
    assert [w["id"] for w in found] == [sid]
    assert json.loads(run_tool(conn, "find_gym_workouts", '{"end_date": "2026-11-06"}'))["workouts"] == []

    w = json.loads(run_tool(conn, "get_gym_workout", json.dumps({"id": sid})))
    squat = next(e for e in w["exercises"] if e["exercise_id"] == "back_squat")
    assert [s["weight_kg"] for s in squat["sets"]] == [100, 105, 95]

    plan = json.loads(run_tool(conn, "get_gym_plan", "{}"))
    assert plan["hr_zones"]["max_hr"] == 200
    assert plan["nutrition"]["protein_g_per_day"] == 160
    assert [c["id"] for c in plan["cardio"]["sessions"]] == ["VO2MAX_1", "VO2MAX_2", "CARDIO"]
    assert [d["day"] for d in plan["gym_days"]] == ["A", "B", "C", "D"]

    hist = json.loads(run_tool(conn, "get_gym_exercise_history", '{"exercise_id": "back_squat"}'))
    assert hist["history"][0]["e1rm_kg"] == round(max(100 * (1 + 5 / 30), 105 * 1.1, 95 * 1.2), 1)
    assert "error" in json.loads(run_tool(conn, "get_gym_workout", '{"id": "nope"}'))
