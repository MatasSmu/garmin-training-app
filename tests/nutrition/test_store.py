"""Store: targets from the plan, weight upsert, food totals, saved meals,
idempotent sync, weekly summary and the expenditure estimate."""

import json
from datetime import date, timedelta

import pytest

from app.nutrition import store

TODAY = date(2026, 12, 1)


def test_targets_come_from_the_plan(conn):
    t = store.targets(conn)
    assert (t["start_weight_kg"], t["goal_weight_kg"]) == (81, 86)
    assert (t["kcal_min"], t["kcal_max"], t["protein_g"]) == (4000, 4200, 160)
    assert t["start_date"] == "2026-11-01" and t["goal_date"] == "2026-12-27"
    assert t["target_rate_kg_week"] == pytest.approx(0.625)


def test_targets_follow_a_changed_plan(conn):
    plan = json.loads(conn.execute("SELECT source_json FROM gym_plans").fetchone()[0])
    plan["nutrition"]["protein_g_per_day"] = 175
    plan["plan"]["athlete"]["target_bodyweight_kg"] = 85
    conn.execute("UPDATE gym_plans SET source_json = ?", (json.dumps(plan),))
    t = store.targets(conn)
    assert t["protein_g"] == 175 and t["target_rate_kg_week"] == pytest.approx(0.5)


def test_weight_upsert_same_date_and_slot_is_one_entry(conn):
    assert store.upsert_weight(conn, "2026-11-02", "morning", 81.2) == "created"
    assert store.upsert_weight(conn, "2026-11-02", "morning", 81.4) == "updated"
    assert store.upsert_weight(conn, "2026-11-02", "morning", 81.4) == "unchanged"
    assert store.upsert_weight(conn, "2026-11-02", "evening", 82.3) == "created"
    rows = conn.execute("SELECT date, slot, weight_kg FROM weight_entries ORDER BY slot").fetchall()
    assert [tuple(r) for r in rows] == [("2026-11-02", "evening", 82.3), ("2026-11-02", "morning", 81.4)]
    ids = {r[0] for r in conn.execute("SELECT id FROM weight_entries")}
    assert len(ids) == 2


def test_weight_none_clears_and_older_copy_is_ignored(conn):
    store.upsert_weight(conn, "2026-11-02", "morning", 81.2, "2026-11-08T10:00:00.000Z")
    assert store.upsert_weight(conn, "2026-11-02", "morning", 80.0, "2026-11-08T09:00:00.000Z") == "stale"
    assert store.upsert_weight(conn, "2026-11-02", "morning", None, "2026-11-08T11:00:00.000Z") == "deleted"
    assert store.list_weights(conn) == []
    with pytest.raises(ValueError):
        store.upsert_weight(conn, "2026-11-02", "noon", 80)


def _log(conn, i, kcal, protein, day="2026-11-05", **kw):
    entry = {"id": f"log-{i}", "date": day, "time": "12:00", "meal": "lunch", "kcal": kcal, "protein_g": protein,
             "source": "manual", "description": f"thing {i}", "updated_at": f"2026-11-05T12:00:0{i}.000Z", **kw}
    store.upsert_log(conn, entry)
    return entry


def test_food_log_totals_for_a_day(conn):
    _log(conn, 1, 600, 40, carbs_g=70)
    _log(conn, 2, 850.5, 55.5, fat_g=20)
    _log(conn, 3, 999, 99, deleted=True)          # deleted entries don't count
    _log(conn, 4, 500, 30, day="2026-11-06")      # other day
    t = store.day_totals(conn, "2026-11-05")
    assert t == {"kcal": 1450.5, "protein_g": 95.5, "carbs_g": 70, "fat_g": 20, "entries": 2,
                 # energy split: 382 kcal protein + 280 kcal carbs + 180 kcal fat; both at lunch = 1 meal
                 "protein_pct": 45, "carbs_pct": 33, "fat_pct": 21, "meals": 1, "snacks": 0, "eating_times": 1}


def test_saved_meal_adds_correct_totals(conn):
    store.upsert_food(conn, {"id": "oats", "name": "Oats", "kcal_100g": 380, "protein_100g": 13,
                             "carbs_100g": 60, "fat_100g": 7, "source": "manual"})
    store.upsert_food(conn, {"id": "whey", "name": "Whey", "kcal_100g": 400, "protein_100g": 80, "source": "manual"})
    store.upsert_saved_meal(conn, {"id": "bf", "name": "Usual breakfast", "items": [
        {"food_id": "oats", "grams": 100},
        {"food_id": "whey", "grams": 30},
        {"description": "Plant milk 300 ml", "kcal": 120, "protein_g": 3},
    ]})
    assert store.saved_meal_totals(conn, "bf") == {"kcal": 620.0, "protein_g": 40.0, "carbs_g": 60.0, "fat_g": 7.0}
    entry = store.log_saved_meal(conn, "bf", "2026-11-05", "07:30")
    assert (entry["kcal"], entry["protein_g"], entry["meal"], entry["source"]) == (620, 40, "breakfast", "saved_meal")
    assert entry["saved_meal_id"] == "bf" and entry["description"] == "Usual breakfast"
    assert store.day_totals(conn, "2026-11-05")["protein_g"] == 40
    meals = store.list_saved_meals(conn, date(2026, 11, 5))
    assert meals[0]["uses_30d"] == 1 and meals[0]["totals"]["kcal"] == 620


def test_saved_meals_most_used_first(conn):
    for mid, name in (("a", "Alpha"), ("b", "Beta")):
        store.upsert_saved_meal(conn, {"id": mid, "name": name, "items": [{"description": name, "kcal": 100}]})
    store.log_saved_meal(conn, "b", "2026-11-05")
    store.log_saved_meal(conn, "b", "2026-11-06")
    store.log_saved_meal(conn, "a", "2026-11-06")
    assert [m["name"] for m in store.list_saved_meals(conn, date(2026, 11, 7))] == ["Beta", "Alpha"]


def test_sync_is_idempotent_and_last_write_wins(conn):
    payload = {
        "foods": [{"id": "f1", "name": "Skyr", "kcal_100g": 63, "protein_100g": 11, "source": "manual",
                   "updated_at": "2026-11-05T08:00:00.000Z"}],
        "logs": [{"id": "l1", "date": "2026-11-05", "time": "08:05", "meal": None, "kcal": 126, "protein_g": 22,
                  "food_id": "f1", "grams": 200, "source": "food", "updated_at": "2026-11-05T08:05:00.000Z"}],
        "weights": [{"date": "2026-11-05", "slot": "morning", "weight_kg": 81.3,
                     "updated_at": "2026-11-05T08:06:00.000Z"}],
    }
    r1 = store.sync(conn, payload)["results"]
    assert r1["logs"] == {"l1": "created"} and r1["foods"] == {"f1": "created"}
    r2 = store.sync(conn, payload)["results"]
    assert r2["logs"] == {"l1": "unchanged"} and r2["weights"] == {"2026-11-05|morning": "unchanged"}
    assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 1
    assert store.get_log(conn, "l1")["meal"] == "breakfast"  # from the clock when not given
    # deleted on the phone later; an older copy can't bring it back
    store.sync(conn, {"logs": [{**payload["logs"][0], "deleted": True, "updated_at": "2026-11-05T09:00:00.000Z"}]})
    assert store.sync(conn, payload)["results"]["logs"] == {"l1": "stale"}
    assert store.day_totals(conn, "2026-11-05")["entries"] == 0


def test_log_with_unknown_food_keeps_its_values(conn):
    _log(conn, 1, 300, 20, food_id="missing", grams=100, source="food")
    entry = store.get_log(conn, "log-1")
    assert entry["food_id"] is None and entry["kcal"] == 300


def test_foods_remember_last_grams(conn):
    store.upsert_food(conn, {"id": "f1", "name": "Rice", "kcal_100g": 130, "protein_100g": 2.7, "source": "manual"})
    _log(conn, 1, 260, 5.4, food_id="f1", grams=200, source="food", day="2026-11-04")
    _log(conn, 2, 325, 6.8, food_id="f1", grams=250, source="food", day="2026-11-05")
    f = store.list_foods(conn)[0]
    assert (f["last_grams"], f["last_used"]) == (250, "2026-11-05")


def _weigh_rising(conn, start, days, per_day=0.09, base=81.0):
    for i in range(days):
        store.upsert_weight(conn, (start + timedelta(days=i)).isoformat(), "morning", base + per_day * i)
        store.upsert_weight(conn, (start + timedelta(days=i)).isoformat(), "evening", base + per_day * i + 1.1)


def test_weight_analysis(conn):
    _weigh_rising(conn, TODAY - timedelta(days=40), 40)
    w = store.weight_analysis(conn, TODAY)
    assert w["trend_kg"] is not None and 82 < w["trend_kg"] < 84.6
    assert 0.4 < w["rate_kg_week"] < 0.7
    assert w["status"] == "on_track" and w["status_label"] == "On track"
    assert w["projected_date"] > TODAY.isoformat()
    assert w["evening_diff_kg"] == pytest.approx(1.1)
    assert len(w["days"]) == 40 and w["days"][0]["evening"] is not None


def test_weight_analysis_without_data(conn):
    w = store.weight_analysis(conn, TODAY)
    assert (w["trend_kg"], w["rate_kg_week"], w["status"]) == (None, None, "no_data")


def test_weekly_summary(conn):
    monday = date(2026, 11, 23)
    _weigh_rising(conn, monday - timedelta(days=14), 21)
    for i in range(5):
        _log(conn, i, 4100, 170 if i < 3 else 120, day=(monday + timedelta(days=i)).isoformat())
    weeks = store.weekly_summary(conn, today=monday + timedelta(days=6))
    wk = weeks[0]
    assert wk["week_start"] == "2026-11-23"
    assert (wk["food_days"], wk["avg_kcal"], wk["avg_protein_g"], wk["protein_days"]) == (5, 4100, 150, 3)
    assert wk["weigh_ins"] == 7 and wk["trend_change_kg"] > 0
    assert len(weeks) == 3


def test_expenditure_needs_14_days_of_weight_and_food(conn):
    _weigh_rising(conn, TODAY - timedelta(days=28), 28, per_day=0.05)
    for i in range(10):
        _log(conn, i % 10, 4000, 160, day=(TODAY - timedelta(days=1 + i)).isoformat(), id=f"x{i}")
    e = store.expenditure_estimate(conn, TODAY)
    assert e["basis"] == "weight_only" and e["expenditure_kcal"] is None
    assert e["advice"].startswith("gaining")


def test_expenditure_estimate_from_intake_and_trend(conn):
    _weigh_rising(conn, TODAY - timedelta(days=60), 60, per_day=0.05)
    for i in range(20):
        _log(conn, 0, 4000, 160, day=(TODAY - timedelta(days=1 + i)).isoformat(), id=f"x{i}")
    _log(conn, 0, 500, 20, day=(TODAY - timedelta(days=22)).isoformat(), id="partial")  # too little to count
    e = store.expenditure_estimate(conn, TODAY)
    assert e["basis"] == "intake_and_weight" and e["days_with_both"] == 20
    assert e["avg_intake_kcal"] == 4000
    morning, _ = store._weights_by_slot(conn)
    from app.nutrition import calc
    series = calc.trend_series(morning)
    first, last = TODAY - timedelta(days=20), TODAY - timedelta(days=1)
    change = calc.trend_change(series, first, last)
    assert e["expenditure_kcal"] == round(4000 - change * 7700 / 19)
    rate = change / 19 * 7
    assert e["suggested_change_kcal"] == round((0.625 - rate) * 7700 / 7)
    assert e["suggested_intake_kcal"] == pytest.approx(e["expenditure_kcal"] + 0.625 * 7700 / 7, abs=1)
