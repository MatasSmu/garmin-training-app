"""The "Fresh light" look: greeting name and the optional background photo in
the page, the photo route, and the dashboard's "How you feel" data."""

import base64
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api.deps import get_db
from app.db import init_db
from app.main import app
from app.nutrition import store

AUTH = {"Authorization": "Basic " + base64.b64encode(f"{config.APP_USERNAME}:{config.APP_PASSWORD}".encode()).decode()}


@pytest.fixture()
def client(tmp_path):
    c = sqlite3.connect(tmp_path / "design.db", check_same_thread=False)
    c.row_factory = sqlite3.Row
    init_db(c)

    def _db():
        yield c

    app.dependency_overrides[get_db] = _db
    with TestClient(app, headers=AUTH) as tc:
        yield tc, c
    app.dependency_overrides.clear()
    c.close()


def test_page_without_photo(client, monkeypatch, tmp_path):
    tc, _ = client
    monkeypatch.setattr(config, "BACKGROUND_PHOTO", tmp_path / "none")
    monkeypatch.setattr(config, "ATHLETE_NAME", "Matas <b>")
    html = tc.get("/").text
    assert 'data-name="Matas &lt;b&gt;"' in html and 'class=""' in html and "--bg-photo" not in html
    assert "{{" not in html
    assert tc.get("/background-photo").status_code == 404


def test_page_with_photo(client, monkeypatch, tmp_path):
    tc, _ = client
    photo = tmp_path / "bg.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    monkeypatch.setattr(config, "BACKGROUND_PHOTO", photo)
    html = tc.get("/").text
    assert 'class="has-photo"' in html and '--bg-photo:url("/background-photo?v=' in html
    r = tc.get("/background-photo")
    assert r.status_code == 200 and r.content == photo.read_bytes() and r.headers["content-type"] == "image/jpeg"


def test_photo_folder_uses_newest_picture(client, monkeypatch, tmp_path):
    import os

    tc, _ = client
    folder = tmp_path / "background"
    folder.mkdir()
    (folder / "notes.txt").write_text("not a picture")
    old, new = folder / "IQFOIL old.jpg", folder / "sunset.png"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1_000_000, 1_000_000))
    monkeypatch.setattr(config, "BACKGROUND_PHOTO", folder)
    r = tc.get("/background-photo")
    assert r.content == b"new" and r.headers["content-type"] == "image/png"
    new.unlink()
    assert tc.get("/background-photo").content == b"old"
    old.unlink()
    assert tc.get("/background-photo").status_code == 404 and 'class=""' in tc.get("/").text


def test_dashboard_how_you_feel(client):
    tc, c = client
    assert tc.get("/api/dashboard/summary").json()["nutrition"]["checkin"] is None
    store.upsert_weight(c, "2026-11-01", "morning", 81.1, score=8.0, note="sunku keltis", time="08:40")
    store.upsert_weight(c, "2026-11-01", "evening", 82.4, note="pavalgis", time="23:20")
    store.upsert_weight(c, "2026-11-02", "morning", 81.9, score=8.5, note="atsikėliau", time="07:49")
    c.commit()
    from datetime import date
    ci = store.latest_checkin(c, date(2026, 11, 2))
    assert (ci["date"], ci["slot"], ci["note"]) == ("2026-11-02", "morning", "atsikėliau")
    assert ci["last_score"] == {"date": "2026-11-02", "slot": "morning", "score": 8.5}
    assert (ci["avg_score_7d"], ci["scores_7d"]) == (8.2, 2)
