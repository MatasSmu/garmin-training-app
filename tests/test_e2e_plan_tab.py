"""Playwright (phone-sized): upload a plan PDF in the Plan tab and see its
pages; the real app on a throwaway DB and plans folder.

    venv/bin/python -m pytest tests/test_e2e_plan_tab.py
"""

import os
import shutil

import pytest

from tests.live_server import AUTH, PHONE, live_server
from tests.test_plans_api import make_pdf

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="pdftoppm (poppler-utils) not installed")]

SHOTS = os.environ.get("PLAN_SHOTS")  # set to a folder to save screenshots


def test_upload_and_view_plan(tmp_path):
    pdf = tmp_path / "Coach plan.pdf"
    pdf.write_bytes(make_pdf(3, "Winter"))
    with live_server(tmp_path / "plan.db", import_plan=False) as base, sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium for Playwright not available: {e}")
        ctx = browser.new_context(**PHONE, http_credentials={"username": AUTH[0], "password": AUTH[1]})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/")
        page.click('.tab-btn[data-tab="plan"]')
        page.wait_for_selector("#planRoot .plan-upload")
        assert "No plan yet" in page.inner_text("#planRoot")

        page.set_input_files('#planRoot input[type="file"]', str(pdf))
        page.fill('#planRoot .plan-upload input[type="text"]', "Winter 8 weeks")
        page.click("#planRoot .plan-upload .btn-primary")
        page.wait_for_selector("#planHead")
        assert page.inner_text("#planHead h2") == "Winter 8 weeks"
        assert "3 pages" in page.inner_text("#planHead")
        imgs = page.locator(".plan-page img")
        assert imgs.count() == 3
        page.wait_for_function("document.querySelector('.plan-page img').naturalWidth > 500")
        assert page.get_attribute('#planHead a[href$="/pdf"]', "target") == "_blank"
        if SHOTS:
            page.screenshot(path=os.path.join(SHOTS, "plan-tab.png"))
        assert not errors, errors
        browser.close()
