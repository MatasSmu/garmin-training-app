"""Playwright: logger UI details.

    venv/bin/python -m pytest tests/gym/test_e2e_logger_ui.py
"""

import os

import pytest

from tests.live_server import AUTH, PHONE

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.e2e

SHOTS = os.environ.get("GYM_SHOTS")  # set to a folder to save screenshots


def test_finished_workout_card_folds_and_stays_folded(server):
    base, _ = server
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium for Playwright not available: {e}")
        ctx = browser.new_context(**PHONE, http_credentials={"username": AUTH[0], "password": AUTH[1]})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/gym/")
        page.wait_for_selector("#syncStatus.ok")
        page.click('.day-tab[data-day="A"]')
        page.click("#startBtn")
        page.click('.set-check[data-slot="3:0"]')
        page.click("#restSkip")
        page.click("#finishBtn")
        page.wait_for_selector('[data-test="summary-sets"]')

        card = "#sessionCard"
        assert page.is_visible(f'{card} [data-act="edit-start"]')
        assert page.inner_text(f'{card} [data-act="fold"]') == "−"
        if SHOTS:
            page.screenshot(path=os.path.join(SHOTS, "finished-open.png"))

        page.click(f'{card} [data-act="fold"]')
        assert page.inner_text(f'{card} [data-act="fold"]') == "+"
        assert page.locator(f'{card} [data-act="edit-start"]').count() == 0
        assert page.locator(f'{card} [data-act="resume"]').count() == 0
        assert page.is_visible(f'{card} [data-act="new"]')
        assert page.inner_text('[data-test="summary-sets"]') == "1 set"
        if SHOTS:
            page.screenshot(path=os.path.join(SHOTS, "finished-folded.png"))

        page.reload()  # the choice is remembered on the phone
        page.wait_for_selector('[data-test="summary-sets"]')
        assert page.inner_text(f'{card} [data-act="fold"]') == "+"
        assert page.locator(f'{card} [data-act="edit-start"]').count() == 0

        page.click(f'{card} [data-act="fold"]')
        assert page.is_visible(f'{card} [data-act="edit-start"]')
        assert page.is_visible(f'{card} [data-act="resume"]')
        assert not errors, errors
        browser.close()
