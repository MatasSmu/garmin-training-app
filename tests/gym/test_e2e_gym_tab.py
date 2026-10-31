"""Playwright: the Gym tab of the main app (phone-sized screen).

    venv/bin/python -m pytest tests/gym/test_e2e_gym_tab.py
"""

import os
import uuid

import httpx
import pytest

from tests.live_server import AUTH, PHONE

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.e2e

SHOTS = os.environ.get("GYM_SHOTS")  # set to a folder to save screenshots


def _log_workout(base):
    boot = httpx.get(base + "/api/gym/bootstrap", auth=AUTH, timeout=10).json()
    day = next(d for d in boot["days"] if d["code"] == "A")
    sid = str(uuid.uuid4())
    sets = [{"id": f"{sid}-{n}", "exercise_id": "back_squat", "set_number": n, "reps": 10, "weight_kg": 80,
             "completed_as_planned": True} for n in (1, 2, 3)]
    httpx.post(base + "/api/gym/sync", auth=AUTH, timeout=10, json={"locations": [], "sessions": [{
        "id": sid, "plan_day_id": day["id"], "location_id": "bsb", "started_at": "2026-11-02T13:05:00Z",
        "ended_at": "2026-11-02T14:20:00Z", "notes": "good", "updated_at": "2026-11-02T14:20:00Z",
        "sets": sets}]}).raise_for_status()


def test_workout_detail_folds_with_minus_button(server):
    base, _ = server
    _log_workout(base)
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium for Playwright not available: {e}")
        ctx = browser.new_context(**PHONE, http_credentials={"username": AUTH[0], "password": AUTH[1]})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def open_workout():
            page.click('.tab-btn[data-tab="gym"]')
            page.click("#gymRoot .tk-table tbody tr.clickable")
            page.wait_for_selector("#gymDetailPanel")

        page.goto(base + "/")
        open_workout()
        panel = "#gymDetailPanel"
        fold = f'{panel} [data-act="gym-fold"]'
        assert page.inner_text(fold) == "−"
        assert page.is_visible(f"{panel} .tk-table") and page.is_visible(f"{panel} .gym-edit")
        if SHOTS:
            page.locator(panel).screenshot(path=os.path.join(SHOTS, "tab-detail-open.png"))

        page.click(fold)
        assert page.inner_text(fold) == "+"
        assert not page.is_visible(f"{panel} .tk-table") and not page.is_visible(f"{panel} .gym-edit")
        assert "Workout" in page.inner_text(f"{panel} h2")  # title and summary stay
        assert "sets 3 / 27" in page.inner_text(panel)
        if SHOTS:
            page.locator(panel).screenshot(path=os.path.join(SHOTS, "tab-detail-folded.png"))

        page.reload()  # remembered in this browser
        open_workout()
        assert page.inner_text(fold) == "+"
        assert not page.is_visible(f"{panel} .tk-table")

        page.click(fold)
        assert page.is_visible(f"{panel} .tk-table") and page.is_visible(f"{panel} .gym-edit")
        assert not errors, errors
        browser.close()
