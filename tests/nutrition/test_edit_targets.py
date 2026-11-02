"""Own daily targets (override the coach's plan, carbs / fat optional) and
editing an eaten item from the laptop (PATCH, picked up by the phone)."""

from datetime import date

from app.nutrition import store


def test_own_targets_override_the_plan_and_reset(client, conn):
    t = client.get("/api/nutrition/targets").json()
    assert (t["protein_g"], t["kcal_min"], t["kcal_max"], t["carbs_g"], t["fat_g"], t["own"]) == (160, 4000, 4200, None, None, [])

    t = client.put("/api/nutrition/targets", json={"protein_g": 175, "carbs_g": 520, "fat_g": 110}).json()
    assert (t["protein_g"], t["carbs_g"], t["fat_g"]) == (175, 520, 110)
    assert (t["kcal_min"], t["kcal_max"]) == (4000, 4200)             # not set -> still the plan's
    assert t["own"] == ["carbs_g", "fat_g", "protein_g"] and t["plan"]["protein_g"] == 160

    # Everything that uses targets follows: the summary, the dashboard and the coach.
    assert client.get("/api/nutrition/summary").json()["targets"]["protein_g"] == 175
    assert store.dashboard_summary(conn)["protein_target_g"] == 175

    assert client.put("/api/nutrition/targets", json={"kcal_min": 4500, "kcal_max": 4300}).status_code == 422
    assert client.put("/api/nutrition/targets", json={"kcal_min": 4500}).status_code == 422   # above the plan's max
    assert client.put("/api/nutrition/targets", json={"protein_g": -5}).status_code == 422

    t = client.delete("/api/nutrition/targets").json()
    assert (t["protein_g"], t["carbs_g"], t["own"]) == (160, None, [])


def _log(conn, **kw):
    entry = {"id": "kokoso", "date": date.today().isoformat(), "time": "18:35", "meal": "snack", "kcal": 54,
             "protein_g": 2.7, "carbs_g": 8.4, "fat_g": 54, "grams": 300, "source": "estimate",
             "description": "kokoso pienas", "updated_at": "2026-11-02T16:35:00.000Z", **kw}
    store.upsert_log(conn, entry)
    conn.commit()


def test_edit_an_eaten_item_from_the_laptop(client, conn):
    _log(conn)
    r = client.patch("/api/nutrition/logs/kokoso", json={"fat_g": 4, "kcal": 90, "meal": "dinner"})
    assert r.status_code == 200, r.text
    log = r.json()["log"]
    assert (log["fat_g"], log["kcal"], log["meal"], log["protein_g"], log["carbs_g"], log["description"]) == \
        (4, 90, "dinner", 2.7, 8.4, "kokoso pienas")
    assert log["updated_at"] > "2026-11-02T16:35:00.000Z"               # newer -> the phone takes it over
    assert r.json()["totals"]["fat_g"] == 4

    # The phone's copy (older timestamp) can't undo the laptop edit.
    client.post("/api/nutrition/sync", json={"logs": [{
        "id": "kokoso", "date": log["date"], "time": "18:35", "meal": "snack", "kcal": 54, "protein_g": 2.7,
        "source": "estimate", "updated_at": "2026-11-02T16:40:00.000Z"}]})
    assert store.get_log(conn, "kokoso")["fat_g"] == 4

    # Delete, and validation.
    assert client.patch("/api/nutrition/logs/kokoso", json={"deleted": True}).json()["log"]["deleted"] is True
    assert client.get(f"/api/nutrition/day/{log['date']}").json()["totals"]["entries"] == 0
    assert client.patch("/api/nutrition/logs/nope", json={"kcal": 1}).status_code == 404
    assert client.patch("/api/nutrition/logs/kokoso", json={"kcal": None}).status_code == 422
    assert client.patch("/api/nutrition/logs/kokoso", json={"fat_g": -1}).status_code == 422
