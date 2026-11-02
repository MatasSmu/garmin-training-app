"""Tests never talk to DeepSeek: the key is blanked and the HTTP call
replaced. Tests that exercise the analysis install their own fake."""

import os
import tempfile
from pathlib import Path

# Before anything imports app.config: importing app.main runs init_db on
# config.DB_PATH, and tests must never open the real data/garmin.db.
_TMP = Path(tempfile.mkdtemp(prefix="garmin-tests-"))
os.environ["APP_DB_PATH"] = str(_TMP / "app.db")
# No Google Sheet auto-import thread, and never the real service-account key.
os.environ["SHEETS_IMPORT_MINUTES"] = "0"
os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = str(_TMP / "no-google-key.json")

import pytest  # noqa: E402

from app import config  # noqa: E402
from app.coach import wind as coach_wind  # noqa: E402
from app.nutrition import openfoodfacts, sheets  # noqa: E402
from app.sailing import deepseek  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", None)

    def refuse(body):
        raise AssertionError("tests must not call DeepSeek")

    monkeypatch.setattr(deepseek, "post_chat", refuse)


@pytest.fixture(autouse=True)
def no_real_forecast(monkeypatch):
    """The coach briefing fetches Open-Meteo forecasts; tests get 'unavailable'
    unless they install their own fake."""
    monkeypatch.setattr(coach_wind, "_fetch_forecast", lambda *a, **k: None)
    monkeypatch.setattr(coach_wind, "_geocode", lambda place: None)


@pytest.fixture(autouse=True)
def no_real_openfoodfacts(monkeypatch):
    def refuse(code):
        raise AssertionError("tests must not call Open Food Facts")

    monkeypatch.setattr(openfoodfacts, "fetch_product", refuse)


@pytest.fixture(autouse=True)
def no_real_google(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("tests must not call Google")

    monkeypatch.setattr(sheets, "_get", refuse)
