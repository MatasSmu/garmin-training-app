"""Tests never talk to DeepSeek: the key is blanked and the HTTP call
replaced. Tests that exercise the analysis install their own fake."""

import pytest

from app import config
from app.sailing import deepseek


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", None)

    def refuse(body):
        raise AssertionError("tests must not call DeepSeek")

    monkeypatch.setattr(deepseek, "post_chat", refuse)
