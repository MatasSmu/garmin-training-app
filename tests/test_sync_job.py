"""The Sync button's background job, with Garmin stubbed out."""

import sqlite3
from datetime import date

import pytest

from app.analysis import service
from app.db import init_db
from app.ingest import garmin_client, job
from app.ingest import sync as sync_mod


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"

    def conn_factory():
        c = sqlite3.connect(path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    c = conn_factory()
    init_db(c)
    c.execute("""INSERT INTO sessions (activity_id, activity_type, activity_name, start_time_local, start_date,
                 raw_json, updated_at) VALUES (1, 'other', 'Windfoiling', '2026-09-10 10:00:00', '2026-09-10', '{}', 'x')""")
    c.commit()
    monkeypatch.setattr(job, "get_connection", conn_factory)
    job._state.update(running=False, ok=None, message=None, step=None)
    return c


def test_sync_window():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE sessions (start_date TEXT)")
    today = date(2026, 9, 24)
    assert job.sync_window(c, today) == date(2026, 9, 21)          # empty db: last 3 days
    c.execute("INSERT INTO sessions VALUES ('2026-09-10')")
    assert job.sync_window(c, today) == date(2026, 9, 9)           # back to the last activity
    c.execute("DELETE FROM sessions"); c.execute("INSERT INTO sessions VALUES ('2025-01-01')")
    assert job.sync_window(c, today) == date(2026, 8, 20)          # capped at 35 days


def test_successful_sync_reports_new_data(db, monkeypatch):
    calls = {}
    monkeypatch.setattr(garmin_client, "login_saved_tokens", lambda: "client")

    def fake_run_sync(start, end, client=None):
        calls["run_sync"] = (start, client)
        c = job.get_connection()
        c.execute("""INSERT INTO sessions (activity_id, activity_type, activity_name, start_time_local, start_date,
                     raw_json, updated_at) VALUES (2, 'other', 'Windfoiling', '2026-09-24 10:00:00', '2026-09-24', '{}', 'y')""")
        c.commit()
        return {}

    monkeypatch.setattr(sync_mod, "run_sync", fake_run_sync)
    monkeypatch.setattr(service, "import_garmin_water", lambda conn, since, client=None, on_item=None:
                        {"imported": 1, "failed": 0, "errors": [], "ids": [5]})
    job._state["running"] = True
    job._run()
    st = job.status(db)
    assert st["running"] is False and st["ok"] is True
    assert st["message"] == "1 new activity, 1 new session on the Session map."
    assert calls["run_sync"][1] == "client"
    assert st["last_synced"] == "y"


def test_expired_login_is_reported_not_hung(db, monkeypatch):
    def boom():
        raise garmin_client.GarminUnavailable("Garmin login with the saved tokens failed")

    monkeypatch.setattr(garmin_client, "login_saved_tokens", boom)
    job._state["running"] = True
    job._run()
    st = job.status(db)
    assert st["ok"] is False and "saved tokens" in st["message"]
    assert st["running"] is False


def test_only_one_sync_at_a_time(monkeypatch):
    started = []
    monkeypatch.setattr(job, "_run", lambda: started.append(1))
    job._state["running"] = False
    assert job.start() is True
    assert job.start() is False  # still flagged running (the stub never clears it)
    job._state["running"] = False
