"""Tests never talk to DeepSeek: the key is blanked and the HTTP call
replaced. Tests that exercise the analysis install their own fake."""

import os
import tempfile
from pathlib import Path

# Before anything imports app.config: importing app.main runs init_db on
# config.DB_PATH, and tests must never open the real data/garmin.db.
os.environ["APP_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="garmin-tests-")) / "app.db")

import pytest  # noqa: E402

from app import config  # noqa: E402
from app.sailing import deepseek  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", None)

    def refuse(body):
        raise AssertionError("tests must not call DeepSeek")

    monkeypatch.setattr(deepseek, "post_chat", refuse)
