"""Playwright end-to-end on the phone page /food/ (Chromium, phone viewport,
real uvicorn on localhost with its own throwaway DB):

1. type a week of weights into the grid and save -> the weight view shows the
   trend and the status against the plan;
2. log a saved meal (one tap) and a manual food -> today's protein/kcal totals;
3. repeat one food offline -> it syncs exactly once when back online.

    venv/bin/python -m pytest tests/nutrition/test_e2e_food.py
(needs `venv/bin/python -m playwright install chromium` once)."""

import sqlite3
from datetime import date, timedelta

import httpx
import pytest

from tests.live_server import AUTH, PHONE

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.e2e

MORNING = ["81.0", "81,2", "81.1", "81.4", "", "81.5", "81.7"]   # a comma works too; Friday missed
EVENING = ["82.1", "82.3", "", "82.6", "82.4", "82.7", "82.9"]


def _api(base, path, method="GET", json=None):
    r = httpx.request(method, base + path, auth=AUTH, json=json, timeout=10)
    r.raise_for_status()
    return r.json()


def _seed_saved_meal(base):
    milk = _api(base, "/api/nutrition/foods", "POST", {"name": "Oat milk", "kcal_100g": 45, "protein_100g": 1})
    pea = _api(base, "/api/nutrition/foods", "POST", {"name": "Pea protein", "kcal_100g": 380, "protein_100g": 80})
    meal = _api(base, "/api/nutrition/saved-meals", "POST", {"name": "Protein shake with plant milk", "items": [
        {"food_id": milk["id"], "grams": 400}, {"food_id": pea["id"], "grams": 50}]})
    assert (meal["totals"]["kcal"], meal["totals"]["protein_g"]) == (370, 44)
    return meal


def _totals(page):
    return page.inner_text('[data-test="kcal-total"]'), page.inner_text('[data-test="protein-total"]')


def test_weights_food_and_offline_sync(server):
    base, db = server
    _seed_saved_meal(base)
    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)

    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # browser binary not installed
            pytest.skip(f"Chromium for Playwright not available: {e}")
        ctx = browser.new_context(**PHONE, http_credentials={"username": AUTH[0], "password": AUTH[1]})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(base + "/food/")
        page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=15000)
        page.wait_for_selector("#syncStatus.ok")

        # ---- 1. a week of weights from paper, typed into the grid
        page.click('[data-view="weight"]')
        assert page.inner_text('[data-test="trend"]') == "—"
        page.click('[data-act="week"][data-d="-7"]')
        first = f'.w-in[data-key="{last_monday.isoformat()}|morning"]'
        page.click(first)
        for m, e in zip(MORNING, EVENING):  # row by row, Next (Enter) moves to the next cell
            page.keyboard.type(m)
            page.keyboard.press("Enter")
            page.keyboard.type(e)
            page.keyboard.press("Enter")
        assert page.evaluate("document.activeElement.id") == "saveWeek"
        page.click("#saveWeek")
        page.wait_for_function(
            "document.querySelector('[data-test=trend]').textContent.indexOf('kg') > 0", timeout=20000)
        page.wait_for_selector("#syncStatus.ok")
        trend = float(page.inner_text('[data-test="trend"]').split()[0])
        assert 81.0 <= trend <= 81.7
        assert page.inner_text('[data-test="status"]') in {"On track", "Too slow", "Too fast"}
        assert page.inner_text('[data-test="rate"]').endswith("kg/wk")
        assert page.locator("svg.wc circle.wc-m").count() == 6
        assert page.locator("svg.wc circle.wc-e").count() == 6
        # the grid shows what was saved, after a reload too
        page.reload()
        page.wait_for_selector('[data-test="trend"]')
        page.click('[data-act="week"][data-d="-7"]')
        assert page.input_value(f'.w-in[data-key="{(last_monday + timedelta(days=1)).isoformat()}|morning"]') == "81.2"
        assert page.input_value(f'.w-in[data-key="{(last_monday + timedelta(days=4)).isoformat()}|morning"]') == ""

        # ---- 2. saved meal (one tap) + a manual food
        page.click('[data-view="today"]')
        page.click('.chip[data-act="log-meal"]')
        assert _totals(page) == ("370", "44")
        page.click('[data-sheet="manual"]')
        page.fill('[name="name"]', "Chicken & rice")
        page.fill('[name="kcal"]', "650")
        page.fill('[name="protein"]', "52")
        page.click('[data-act="manual-save"]')
        assert _totals(page) == ("1,020", "96")
        page.wait_for_selector("#syncStatus.ok", timeout=20000)

        # ---- 3. offline: repeat the manual food from Recent
        ctx.set_offline(True)
        page.click('[data-act="relog"] >> text=Chicken & rice')
        assert _totals(page) == ("1,670", "148")
        page.wait_for_function("document.querySelector('#syncStatus').textContent === '1 change waiting to sync'",
                               timeout=10000)
        page.reload()  # served by the service worker, entries from IndexedDB
        page.wait_for_selector('[data-test="kcal-total"]')
        assert _totals(page) == ("1,670", "148")
        assert page.locator(".tag.wait").count() == 1

        ctx.set_offline(False)
        page.wait_for_selector("#syncStatus.ok", timeout=40000)
        page.click("#syncStatus")  # a manual re-sync and a reload must not duplicate anything
        page.reload()
        page.wait_for_selector("#syncStatus.ok", timeout=20000)
        assert _totals(page) == ("1,670", "148")
        assert page.locator(".tag.wait").count() == 0
        assert not errors, errors
        browser.close()

    day = _api(base, f"/api/nutrition/day/{today.isoformat()}")
    assert [x["description"] for x in day["logs"]].count("Chicken & rice") == 2
    assert (day["totals"]["kcal"], day["totals"]["protein_g"]) == (1670, 148)
    assert len(_api(base, "/api/nutrition/weights")) == 12

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM food_logs").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(DISTINCT id) FROM food_logs WHERE description = 'Chicken & rice'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM weight_entries").fetchone()[0] == 12
    conn.close()


def test_nutrition_tab_shows_trend_and_dashboard_tiles(server):
    base, _db = server
    today = date.today()
    entries = [{"date": (today - timedelta(days=20 - i)).isoformat(), "slot": "morning", "weight_kg": 81 + 0.08 * i}
               for i in range(20)]
    _api(base, "/api/nutrition/weights", "PUT", {"entries": entries})
    _api(base, "/api/nutrition/sync", "POST", {"logs": [
        {"id": "x1", "date": today.isoformat(), "time": "08:00", "kcal": 900, "protein_g": 55,
         "description": "Breakfast", "source": "manual", "updated_at": "2026-11-01T08:00:00Z"}]})
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium for Playwright not available: {e}")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                  http_credentials={"username": AUTH[0], "password": AUTH[1]})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/")
        page.wait_for_function("document.getElementById('statWeight').textContent.indexOf('kg') > 0")
        assert page.inner_text("#statProtein") == "55 / 160 g"
        page.click('.tab-btn[data-tab="nutrition"]')
        page.wait_for_selector('[data-test="nut-trend"]')
        assert page.inner_text('[data-test="nut-trend"]').endswith(" kg")
        assert page.inner_text('[data-test="nut-status"]') in {"On track", "Too slow", "Too fast"}
        assert page.locator("#tab-nutrition svg.wc circle.wc-m").count() == 20
        assert page.inner_text('[data-test="nut-advice"]').startswith("Gaining")
        assert not errors, errors
        browser.close()
