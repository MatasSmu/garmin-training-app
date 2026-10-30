"""gym_plan.json import: expected counts, and a second run changes nothing."""

from app.gym.plan_import import counts, import_plan

TABLES = ["gym_locations", "gym_plans", "gym_plan_days", "gym_exercises", "gym_plan_exercises"]


def _snapshot(conn):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in TABLES}


def test_import_counts(conn):
    plan_id = conn.execute("SELECT id FROM gym_plans").fetchone()[0]
    assert counts(conn, plan_id) == {
        "plan_days": 4,
        "exercises": 29,
        "items_per_day": {"A": 10, "B": 10, "C": 11, "D": 6},
        "locations": 2,
    }


def test_import_is_idempotent(conn, plan_data):
    before = _snapshot(conn)
    changes = conn.total_changes
    import_plan(conn, plan_data)
    import_plan(conn, plan_data)
    assert conn.total_changes == changes
    assert _snapshot(conn) == before


def test_import_details(conn):
    day = conn.execute("SELECT * FROM gym_plan_days WHERE code = 'A'").fetchone()
    assert (day["weekday"], day["default_time"], day["default_location_id"]) == (0, "15:00-17:00", "bsb")
    b = conn.execute("SELECT weekday, default_location_id FROM gym_plan_days WHERE code = 'B'").fetchone()
    assert tuple(b) == (2, "zb")
    squat = conn.execute(
        """SELECT pe.* FROM gym_plan_exercises pe JOIN gym_plan_days d ON d.id = pe.plan_day_id
           WHERE d.code = 'A' AND pe.exercise_id = 'back_squat'"""
    ).fetchone()
    assert (squat["sets"], squat["reps_min"], squat["reps_max"]) == (3, 8, 10)
    assert (squat["weight_kg_min"], squat["weight_kg_max"], squat["rest_sec"]) == (80, 90, 120)
    bss = conn.execute("SELECT per_side, type FROM gym_exercises WHERE id = 'bulgarian_split_squat'").fetchone()
    assert tuple(bss) == (1, "weighted")
    press = conn.execute("SELECT weight_mode FROM gym_exercises WHERE id = 'dumbbell_chest_press'").fetchone()
    assert press[0] == "total"
    pair = conn.execute(
        """SELECT pe.exercise_id FROM gym_plan_exercises pe JOIN gym_plan_days d ON d.id = pe.plan_day_id
           WHERE d.code = 'C' AND pe.superset_group = 'S2' ORDER BY pe.position"""
    ).fetchall()
    assert [r[0] for r in pair] == ["rope_curl", "rope_triceps_ext"]


def test_reimport_applies_edits_and_removed_items(conn, plan_data):
    plan_data["workouts"][3]["items"].pop()  # D loses its last item
    plan_data["exercises"]["plank"]["name_en"] = "Front plank"
    plan_id = import_plan(conn, plan_data)
    assert counts(conn, plan_id)["items_per_day"]["D"] == 5
    assert conn.execute("SELECT name_en FROM gym_exercises WHERE id = 'plank'").fetchone()[0] == "Front plank"
    assert conn.execute("SELECT COUNT(*) FROM gym_plans").fetchone()[0] == 1
