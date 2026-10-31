"""Gym tests run against a fresh throwaway database with the real plan file
imported (never data/garmin.db -- see tests/conftest.py)."""

import base64
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api.deps import get_db
from app.db import init_db
from app.gym.plan_import import DEFAULT_PLAN_PATH, import_plan
from app.main import app
from tests.live_server import live_server

AUTH = {"Authorization": "Basic " + base64.b64encode(f"{config.APP_USERNAME}:{config.APP_PASSWORD}".encode()).decode()}


@pytest.fixture()
def plan_data():
    return json.loads(DEFAULT_PLAN_PATH.read_text())


@pytest.fixture()
def conn(tmp_path, plan_data):
    c = sqlite3.connect(tmp_path / "gym.db", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)
    import_plan(c, plan_data)
    yield c
    c.close()


@pytest.fixture()
def client(conn):
    def _db():
        yield conn

    app.dependency_overrides[get_db] = _db
    with TestClient(app, headers=AUTH) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def server(tmp_path):
    """The real app on a free port with its own throwaway DB: (base_url, db_path)."""
    db = tmp_path / "e2e.db"
    with live_server(db) as base:
        yield base, db
