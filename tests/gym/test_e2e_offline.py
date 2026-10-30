"""Playwright end-to-end: the phone logger works with no network and syncs
exactly once when it's back.

Chromium with a phone viewport against a real uvicorn on localhost (own
throwaway DB): load online -> go offline -> start day A, tick sets, adjust one
set's reps and kg, finish -> reload while offline, data still there -> back
online -> the API shows the session and every set exactly once.

    venv/bin/python -m pytest tests/gym/test_e2e_offline.py
(needs `venv/bin/python -m playwright install chromium` once)."""

import sqlite3

import httpx
import pytest

from tests.live_server import AUTH, live_server

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.e2e

PHONE = {  # roughly a Xiaomi 14T in Chrome
    "viewport": {"width": 393, "height": 851},
    "device_scale_factor": 2.75,
    "is_mobile": True,
    "has_touch": True,
}


@pytest.fixture()
def server(tmp_path):
    db = tmp_path / "e2e.db"
    with live_server(db) as base:
        yield base, db


def _api(base, path):
    r = httpx.get(base + path, auth=AUTH, timeout=10)
    r.raise_for_status()
    return r.json()


def test_offline_workout_syncs_exactly_once(server):
    base, db = server
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # browser binary not installed
            pytest.skip(f"Chromium for Playwright not available: {e}")
        ctx = browser.new_context(**PHONE, http_credentials={"username": AUTH[0], "password": AUTH[1]})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # 1. Online: first load installs the service worker and caches the plan.
        page.goto(base + "/gym/")
        page.wait_for_selector('.day-tab[data-day="A"]')
        page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=15000)
        page.wait_for_selector("#syncStatus.ok")

        # 2. Offline from here on.
        ctx.set_offline(True)
        page.click('.day-tab[data-day="A"]')
        page.click("#startBtn")
        page.wait_for_selector("#finishBtn")

        # Pull-ups / push-ups superset, then two squat sets as planned (10 x 80 kg).
        for slot in ("1:0", "2:0", "3:0", "3:1"):
            page.click(f'.set-check[data-slot="{slot}"]')
            if page.is_visible("#restBar"):  # pull-ups have no rest (superset), the rest do
                page.click("#restSkip")
        # Squat set 3: 8 reps instead of 10, 85 kg instead of 80.
        page.fill('input.in-reps[data-slot="3:2"]', "8")
        page.click('[data-act="step"][data-slot="3:2"][data-f="weight_kg"][data-d="1"]')
        page.click('[data-act="step"][data-slot="3:2"][data-f="weight_kg"][data-d="1"]')
        assert page.input_value('input.in-weight_kg[data-slot="3:2"]') == "85"
        page.click('.set-check[data-slot="3:2"]')
        assert page.is_visible("#restBar")  # rest timer started after the tick
        assert "Rest" in page.inner_text("#restLabel")
        page.click("#restSkip")
        page.click('[data-act="feel"][data-ex="back_squat"][data-v="4"]')
        page.fill('textarea[data-act="note"]', "Offline test session")
        page.wait_for_timeout(600)  # note is saved after a short pause
        page.click("#finishBtn")

        page.wait_for_selector('[data-test="summary-sets"]')
        assert page.inner_text('[data-test="summary-sets"]') == "5 sets"
        assert page.inner_text("#syncStatus") == "1 session waiting to sync"
        # Nothing can have reached the server yet.
        assert _api(base, "/api/gym/sessions") == []

        # 3. Reload while still offline: served by the service worker, data from IndexedDB.
        page.reload()
        page.wait_for_selector('[data-test="summary-sets"]')
        assert page.inner_text('[data-test="summary-sets"]') == "5 sets"
        assert page.inner_text("#syncStatus") == "1 session waiting to sync"
        assert page.locator(".hist-row .tag.wait").count() == 1
        page.click(".hist-row [data-act=edit]")
        assert page.input_value(".hist-edit textarea") == "Offline test session"

        # 4. Back online: the `online` event syncs it.
        ctx.set_offline(False)
        page.wait_for_selector("#syncStatus.ok", timeout=40000)
        assert page.inner_text("#syncStatus") == "All synced"

        # A manual re-sync and another reload must not duplicate anything.
        page.click("#syncStatus")
        page.reload()
        page.wait_for_selector("#syncStatus.ok", timeout=20000)
        assert not errors, errors
        browser.close()

    # 5. Verify via the API: one session, all 5 sets, exactly once.
    sessions = _api(base, "/api/gym/sessions")
    assert len(sessions) == 1
    s = sessions[0]
    assert (s["day_code"], s["location_id"], s["sets_done"], s["notes"]) == ("A", "bsb", 5, "Offline test session")
    assert s["started_at"] and s["ended_at"]
    detail = _api(base, f"/api/gym/sessions/{s['id']}")
    sets = {e["exercise_id"]: e["sets"] for e in detail["exercises"] if e["sets"]}
    assert [x["reps"] for x in sets["pull_up"]] == [12]
    assert [x["reps"] for x in sets["push_up"]] == [20]
    squat = sets["back_squat"]
    assert [(x["set_number"], x["reps"], x["weight_kg"], x["completed_as_planned"]) for x in squat] == [
        (1, 10, 80, True), (2, 10, 80, True), (3, 8, 85, False)]
    assert all(x["rpe"] == 4 for x in squat)

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM gym_sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM gym_set_logs").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(DISTINCT id) FROM gym_set_logs").fetchone()[0] == 5
    conn.close()
