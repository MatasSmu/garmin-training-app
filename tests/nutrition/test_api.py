"""API: batch weights, foods, saved meals, sync, quick-text estimate (DeepSeek
mocked), barcode lookup (Open Food Facts mocked), summaries and the coach."""

import json
from datetime import date, timedelta

import pytest

from app import config
from app.coach.engine import build_context
from app.coach.responder import _context_briefing
from app.nutrition import openfoodfacts
from app.sailing import deepseek


def week(monday: date, morning: list, evening: list):
    entries = []
    for i in range(7):
        day = (monday + timedelta(days=i)).isoformat()
        entries.append({"date": day, "slot": "morning", "weight_kg": morning[i]})
        entries.append({"date": day, "slot": "evening", "weight_kg": evening[i]})
    return {"entries": entries}


def test_batch_save_a_week_of_weights(client, conn):
    monday = date.today() - timedelta(days=date.today().weekday() + 7)
    morning = [81.0, 81.1, None, 81.3, 81.2, 81.5, 81.6]
    evening = [82.1, None, 82.4, 82.5, None, 82.6, 82.8]
    r = client.put("/api/nutrition/weights", json=week(monday, morning, evening))
    assert r.status_code == 200, r.text
    res = r.json()["results"]
    assert sum(v == "created" for v in res.values()) == 11 and sum(v == "unchanged" for v in res.values()) == 3
    w = r.json()["weight"]
    assert w["trend_kg"] is not None and w["status"] in {"on_track", "too_slow", "too_fast"}
    assert w["rate_kg_week"] is not None

    rows = client.get("/api/nutrition/weights").json()
    assert len(rows) == 11

    # Saving the same week again changes nothing; fixing one cell and clearing another
    assert set(client.put("/api/nutrition/weights", json=week(monday, morning, evening)).json()["results"].values()) \
        == {"unchanged"}
    fix = {"entries": [{"date": monday.isoformat(), "slot": "morning", "weight_kg": 80.8},
                       {"date": monday.isoformat(), "slot": "evening", "weight_kg": None}]}
    assert client.put("/api/nutrition/weights", json=fix).json()["results"] == {
        f"{monday}|morning": "updated", f"{monday}|evening": "deleted"}
    assert conn.execute("SELECT COUNT(*) FROM weight_entries").fetchone()[0] == 10
    assert conn.execute("SELECT weight_kg FROM weight_entries WHERE date = ? AND slot = 'morning'",
                        (monday.isoformat(),)).fetchone()[0] == 80.8


def test_weights_are_validated(client):
    bad = {"entries": [{"date": "2026-11-02", "slot": "noon", "weight_kg": 81}]}
    assert client.put("/api/nutrition/weights", json=bad).status_code == 422
    bad = {"entries": [{"date": "2026-11-02", "slot": "morning", "weight_kg": 5}]}
    assert client.put("/api/nutrition/weights", json=bad).status_code == 422
    bad = {"entries": [{"date": "2026-13-02", "slot": "morning", "weight_kg": 81}]}
    assert client.put("/api/nutrition/weights", json=bad).status_code == 422


def _shake(client):
    milk = client.post("/api/nutrition/foods", json={"name": "Oat milk", "kcal_100g": 45, "protein_100g": 1}).json()
    whey = client.post("/api/nutrition/foods", json={"name": "Pea protein", "kcal_100g": 380,
                                                     "protein_100g": 80, "carbs_100g": 5, "fat_100g": 6}).json()
    meal = client.post("/api/nutrition/saved-meals", json={"name": "Protein shake with plant milk", "items": [
        {"food_id": milk["id"], "grams": 400}, {"food_id": whey["id"], "grams": 50},
        {"description": "Banana", "kcal": 105, "protein_g": 1.3, "carbs_g": 27}]})
    assert meal.status_code == 200, meal.text
    return meal.json()


def test_log_saved_meal(client, conn):
    meal = _shake(client)
    # 400 g oat milk 180 kcal / 4 g + 50 g pea protein 190 kcal / 40 g + banana 105 / 1.3
    assert meal["totals"]["kcal"] == 475 and meal["totals"]["protein_g"] == 45.3
    r = client.post(f"/api/nutrition/saved-meals/{meal['id']}/log",
                    json={"id": "tap-1", "date": "2026-11-05", "time": "16:10"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["log"]["kcal"], body["log"]["protein_g"], body["log"]["meal"]) == (475, 45.3, "snack")
    assert body["totals"]["kcal"] == 475 and body["totals"]["entries"] == 1
    # a double tap with the same id doesn't log it twice
    client.post(f"/api/nutrition/saved-meals/{meal['id']}/log", json={"id": "tap-1", "date": "2026-11-05"})
    day = client.get("/api/nutrition/day/2026-11-05").json()
    assert len(day["logs"]) == 1 and day["totals"]["protein_g"] == 45.3
    assert client.post("/api/nutrition/saved-meals/nope/log", json={}).status_code == 404

    assert client.get("/api/nutrition/saved-meals").json()[0]["uses_30d"] in (0, 1)  # depends on today's date
    assert client.delete(f"/api/nutrition/saved-meals/{meal['id']}").status_code == 200
    assert client.get("/api/nutrition/saved-meals").json() == []
    assert client.get("/api/nutrition/day/2026-11-05").json()["totals"]["kcal"] == 475  # history kept


def test_sync_endpoint_twice_stores_once(client, conn):
    payload = {
        "foods": [{"id": "f-1", "name": "Cottage cheese", "kcal_100g": 98, "protein_100g": 11,
                   "updated_at": "2026-11-05T07:00:00.000Z"}],
        "saved_meals": [{"id": "m-1", "name": "Usual breakfast", "items": [{"food_id": "f-1", "grams": 200}],
                         "updated_at": "2026-11-05T07:00:00.000Z"}],
        "logs": [
            {"id": "l-1", "date": "2026-11-05", "time": "07:10", "kcal": 196, "protein_g": 22, "food_id": "f-1",
             "grams": 200, "source": "food", "updated_at": "2026-11-05T07:10:00.000Z"},
            {"id": "l-2", "date": "2026-11-05", "time": "07:12", "kcal": 196, "protein_g": 22, "saved_meal_id": "m-1",
             "description": "Usual breakfast", "source": "saved_meal", "updated_at": "2026-11-05T07:12:00.000Z"},
        ],
        "weights": [{"date": "2026-11-05", "slot": "morning", "weight_kg": 81.4, "updated_at": "2026-11-05T07:00:00Z"}],
    }
    for _ in range(2):
        assert client.post("/api/nutrition/sync", json=payload).status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM weight_entries").fetchone()[0] == 1
    boot = client.get("/api/nutrition/bootstrap").json()
    assert {"targets", "foods", "saved_meals", "logs", "weights", "weight", "weeks", "expenditure"} <= set(boot)
    assert boot["targets"]["protein_g"] == 160
    assert boot["saved_meals"][0]["totals"]["kcal"] == 196
    assert boot["foods"][0]["last_grams"] == 200
    assert client.post("/api/nutrition/sync", json={"logs": [{**payload["logs"][0], "source": "nope"}]}).status_code == 422
    assert client.post("/api/nutrition/sync", json={"foods": [{"name": "x", "kcal_100g": 1}]}).status_code == 422


# ------------------------------------------------------------------ quick text (DeepSeek mocked)


def fake_deepseek(monkeypatch, content, calls=None):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "test-key")

    def post_chat(body):
        if calls is not None:
            calls.append(body)
        return {"model": "deepseek-chat", "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "choices": [{"finish_reason": "stop", "message": {"content": content}}]}

    monkeypatch.setattr(deepseek, "post_chat", post_chat)


def test_quick_text_estimate_is_returned_not_saved(client, conn, monkeypatch):
    calls = []
    fake_deepseek(monkeypatch, json.dumps({"items": [
        {"name": "Burger", "grams": 250, "kcal": 600, "protein_g": 30, "carbs_g": 45, "fat_g": 32},
        {"name": "Burger", "grams": 250, "kcal": 600, "protein_g": 30, "carbs_g": 45, "fat_g": 32},
        {"name": "Milk 2.5% 500 ml", "grams": 515, "kcal": 260, "protein_g": 17, "carbs_g": 24, "fat_g": 12.5},
    ], "note": "Typical fast-food burgers."}), calls)
    r = client.post("/api/nutrition/estimate", json={"text": "2 burgers and 500 ml milk"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["name"] for i in body["items"]] == ["Burger", "Burger", "Milk 2.5% 500 ml"]
    assert body["totals"]["kcal"] == 1460 and body["totals"]["protein_g"] == 77
    assert calls and calls[0]["messages"][1]["content"] == "2 burgers and 500 ml milk"
    assert calls[0]["response_format"] == {"type": "json_object"}
    # nothing logged until the athlete confirms
    assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 0
    assert conn.execute("SELECT purpose, ok FROM llm_usage").fetchall()[0][:] == ("nutrition_estimate", 1)

    # confirming = the phone syncs the (possibly edited) items as estimate entries
    logs = [{"id": f"e-{i}", "date": "2026-11-05", "time": "13:00", "kcal": it["kcal"], "protein_g": it["protein_g"],
             "description": it["name"], "source": "estimate", "updated_at": "2026-11-05T13:00:00Z"}
            for i, it in enumerate(body["items"])]
    client.post("/api/nutrition/sync", json={"logs": logs})
    rows = conn.execute("SELECT source FROM food_logs").fetchall()
    assert [r[0] for r in rows] == ["estimate"] * 3


def test_quick_text_bad_model_answer(client, monkeypatch):
    fake_deepseek(monkeypatch, '{"items": []}')
    assert client.post("/api/nutrition/estimate", json={"text": "some food"}).status_code == 502


def test_quick_text_without_key(client):
    r = client.post("/api/nutrition/estimate", json={"text": "some food"})
    assert r.status_code == 503 and "DEEPSEEK_API_KEY" in r.json()["detail"]


# ------------------------------------------------------------------ barcode (Open Food Facts mocked)


PRODUCT = {"product_name": "Varškės sūrelis", "brands": "Magija, Pieno žvaigždės", "serving_quantity": "45",
           "nutriments": {"energy-kcal_100g": 400, "proteins_100g": 8.5, "carbohydrates_100g": 30, "fat_100g": 27}}


def test_barcode_lookup_is_cached(client, conn, monkeypatch):
    calls = []

    def fetch(code):
        calls.append(code)
        return PRODUCT

    monkeypatch.setattr(openfoodfacts, "fetch_product", fetch)
    r = client.get("/api/nutrition/barcode/4770190000016")
    assert r.status_code == 200, r.text
    food = r.json()["food"]
    assert r.json()["found"] is True
    assert (food["name"], food["brand"], food["kcal_100g"], food["protein_100g"], food["serving_g"], food["source"]) == \
        ("Varškės sūrelis", "Magija", 400, 8.5, 45, "openfoodfacts")
    # second scan: from the cache, no request
    assert client.get("/api/nutrition/barcode/4770190000016").json()["food"]["id"] == food["id"]
    assert calls == ["4770190000016"]
    assert conn.execute("SELECT COUNT(*) FROM foods WHERE barcode = '4770190000016'").fetchone()[0] == 1


def test_barcode_unknown_then_label_entered_once(client, monkeypatch):
    monkeypatch.setattr(openfoodfacts, "fetch_product", lambda code: None)
    r = client.get("/api/nutrition/barcode/4770000000000").json()
    assert (r["found"], r["food"]) == (False, None)
    created = client.post("/api/nutrition/foods", json={"name": "Local bread", "kcal_100g": 250, "protein_100g": 9,
                                                       "barcode": "4770000000000"}).json()
    monkeypatch.setattr(openfoodfacts, "fetch_product", lambda code: pytest.fail("should come from the cache"))
    assert client.get("/api/nutrition/barcode/4770000000000").json()["food"]["id"] == created["id"]


def test_barcode_kj_only_and_errors(client, monkeypatch):
    monkeypatch.setattr(openfoodfacts, "fetch_product",
                        lambda code: {"product_name": "Juice", "nutriments": {"energy_100g": 184, "proteins_100g": 0.5}})
    assert client.get("/api/nutrition/barcode/12345678").json()["food"]["kcal_100g"] == 44.0
    assert client.get("/api/nutrition/barcode/abc").status_code == 400

    def down(code):
        raise openfoodfacts.LookupUnavailable("couldn't reach Open Food Facts (ConnectError)")

    monkeypatch.setattr(openfoodfacts, "fetch_product", down)
    assert client.get("/api/nutrition/barcode/87654321").status_code == 503


# ------------------------------------------------------------------ summaries + coach


def _history(client, days=21):
    today = date.today()
    entries = [{"date": (today - timedelta(days=days - i)).isoformat(), "slot": "morning", "weight_kg": 81 + 0.08 * i}
               for i in range(days)]
    client.put("/api/nutrition/weights", json={"entries": entries})
    logs = [{"id": f"d{i}", "date": (today - timedelta(days=i)).isoformat(), "time": "12:00", "kcal": 3600,
             "protein_g": 150, "source": "manual", "description": "food", "updated_at": "2026-11-05T12:00:00Z"}
            for i in range(7)]
    client.post("/api/nutrition/sync", json={"logs": logs})


def test_summary_and_dashboard(client):
    _history(client)
    s = client.get("/api/nutrition/summary").json()
    assert s["today"]["protein_g"] == 150 and s["weight"]["trend_kg"] > 81
    assert s["weeks"] and s["expenditure"]["basis"] == "weight_only"
    d = client.get("/api/dashboard/summary").json()["nutrition"]
    assert d["protein_today_g"] == 150 and d["protein_target_g"] == 160 and d["trend_kg"] == s["weight"]["trend_kg"]


def test_coach_briefing_has_weight_and_nutrition(client, conn):
    assert "Bodyweight" not in _context_briefing(build_context(conn))  # nothing logged: no block
    _history(client)
    text = _context_briefing(build_context(conn))
    assert "Bodyweight (morning trend, EMA):" in text and "goal 86 kg by 2026-12-27" in text
    assert "kg/week" in text
    assert "Nutrition last 7 days (targets 4000-4200 kcal, 160 g protein" in text
    assert "3600 kcal / 150 g P" in text
    assert "Weight-trend advice: gaining" in text
