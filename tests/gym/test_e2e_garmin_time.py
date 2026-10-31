"""Playwright: a workout logged on the phone at the wrong time gets the
watch's times only after the athlete taps "Use Garmin time" -- and the phone
then shows them and never syncs the old ones back.

    venv/bin/python -m pytest tests/gym/test_e2e_garmin_time.py
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from tests.live_server import AUTH, PHONE

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.e2e

TZ = "Europe/Vilnius"


def _api(base, path):
    r = httpx.get(base + path, auth=AUTH, timeout=10)
    r.raise_for_status()
    return r.json()


def _add_garmin_strength(db, activity_id, start_utc, minutes):
    local = start_utc.astimezone(ZoneInfo(TZ))
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO sessions (activity_id, activity_type, activity_name, start_time_local, start_date,
                                 duration_s, avg_hr, max_hr, raw_json, updated_at)
           VALUES (?, 'strength_training', 'Strength', ?, ?, ?, 122, 163, ?, 'x')""",
        (activity_id, local.strftime("%Y-%m-%d %H:%M:%S"), local.date().isoformat(), minutes * 60,
         json.dumps({"startTimeGMT": start_utc.strftime("%Y-%m-%d %H:%M:%S")})),
    )
    conn.commit()
    conn.close()


def test_garmin_time_applied_only_after_confirming(server):
    base, db = server
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium for Playwright not available: {e}")
        ctx = browser.new_context(**PHONE, timezone_id=TZ,
                                  http_credentials={"username": AUTH[0], "password": AUTH[1]})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # Log a short workout now (as if typed in after the gym).
        page.goto(base + "/gym/")
        page.wait_for_selector("#syncStatus.ok")
        page.click('.day-tab[data-day="A"]')
        page.click("#startBtn")
        page.click('.set-check[data-slot="3:0"]')
        page.click("#restSkip")
        page.click("#finishBtn")
        page.wait_for_selector('[data-test="summary-sets"]')
        page.wait_for_selector("#syncStatus.ok", timeout=20000)
        logged = _api(base, "/api/gym/sessions")[0]
        assert logged["garmin_suggestion"] is None

        # The watch recorded the real workout three hours earlier.
        g_start = (datetime.fromisoformat(logged["started_at"].replace("Z", "+00:00"))
                   - timedelta(hours=3)).replace(second=0)
        _add_garmin_strength(db, 777, g_start, 100)
        g_end = g_start + timedelta(minutes=100)
        local = lambda dt: dt.astimezone(ZoneInfo(TZ)).strftime("%H:%M")  # noqa: E731

        page.reload()
        box = page.wait_for_selector('[data-test="garmin-box"]', timeout=20000)
        assert f"{local(g_start)}–{local(g_end)}" in box.inner_text()
        assert "100 min" in box.inner_text()
        # Shown, not applied.
        assert _api(base, "/api/gym/sessions")[0]["started_at"] == logged["started_at"]

        page.click('[data-act="garmin-use"]')
        page.wait_for_selector('[data-test="garmin-box"]', state="detached", timeout=20000)
        s = _api(base, "/api/gym/sessions")[0]
        assert s["started_at"] == g_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert s["ended_at"] == g_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert s["garmin"]["activity_id"] == 777 and s["duration_min"] == 100
        # The phone shows the new times too.
        page.wait_for_function("document.querySelector('[data-test=summary]').textContent.includes('100 min')")
        assert page.input_value('[data-act="edit-start"]') == g_start.astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%dT%H:%M")

        # A later edit on the phone keeps Garmin's times.
        page.fill('#sessionCard textarea[data-act="note"]', "times from the watch")
        page.wait_for_timeout(700)
        page.wait_for_selector("#syncStatus.ok", timeout=20000)
        page.click("#syncStatus")
        page.wait_for_selector("#syncStatus.ok", timeout=20000)
        assert not errors, errors
        browser.close()

    s = _api(base, "/api/gym/sessions")[0]
    assert s["notes"] == "times from the watch"
    assert s["started_at"] == g_start.strftime("%Y-%m-%dT%H:%M:%SZ") and s["duration_min"] == 100
