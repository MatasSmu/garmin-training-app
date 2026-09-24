"""End-to-end API test on a throwaway database: upload -> analyse -> edit ->
gear -> cross-session views -> delete."""

import base64
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.analysis import conditions, service
from app.api.deps import get_db
from app.db import init_db
from app.main import app

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"{config.APP_USERNAME}:{config.APP_PASSWORD}".encode()).decode()}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)

    def _db():
        yield conn

    monkeypatch.setattr(service, "TRACKS_DIR", tmp_path / "tracks")
    monkeypatch.setattr(conditions, "fetch", lambda *a, **k: {
        "source": "open-meteo (test)", "wind_kn": 14.0, "gust_kn": 20.0, "wind_dir": 350, "air_c": 16.0,
        "water_c": None, "hourly": []})
    app.dependency_overrides[get_db] = _db
    with TestClient(app, headers=AUTH) as c:
        yield c
    app.dependency_overrides.clear()
    conn.close()


def _upload(client, name):
    p = SAMPLES / name
    r = client.post("/api/tracks", files={"file": (p.name, p.read_bytes())})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_requires_auth():
    with TestClient(app) as c:
        assert c.get("/api/tracks").status_code == 401


def test_full_flow(client):
    tid = _upload(client, "2026-09-22_puck_windfoil.fit")
    # same file again is de-duplicated
    again = client.post("/api/tracks", files={"file": ("x.fit", (SAMPLES / "2026-09-22_puck_windfoil.fit").read_bytes())})
    assert again.json() == {"id": tid, "created": False}
    gpx = _upload(client, "2026-09-21_puck_windfoil.gpx")

    rows = client.get("/api/tracks").json()
    assert {r["id"] for r in rows} == {tid, gpx}
    assert all(r["max_kn"] and r["wind_deg"] is not None for r in rows)

    a = client.get(f"/api/tracks/{tid}/analysis").json()
    assert a["track"]["conditions"]["wind_kn"] == 14.0
    assert a["wind"]["prior_source"] == "open-meteo"
    n = len(a["series"]["t"])
    assert n > 1000 and all(len(v) == n for v in a["series"].values())
    assert a["summary"]["tacks"] == sum(m["type"] == "tack" for m in a["maneuvers"])
    assert a["personal_polar"] is not None

    # settings: manual wind + custom thresholds re-run the engine
    r = client.patch(f"/api/tracks/{tid}", json={"settings": {"wind_override_deg": 10, "on_kn": 12, "off_kn": 9},
                                                 "notes": "gusty", "spot": "Puck bay"})
    assert r.status_code == 200 and r.json()["notes"] == "gusty"
    a2 = client.get(f"/api/tracks/{tid}/analysis").json()
    assert a2["wind"]["source"] == "manual" and a2["wind"]["deg"] == 10
    assert a2["state"]["on_kn"] == 12
    # null resets a setting
    client.patch(f"/api/tracks/{tid}", json={"settings": {"wind_override_deg": None}})
    assert client.get(f"/api/tracks/{tid}/analysis").json()["wind"]["source"] == "auto"

    # conditions: re-fetch, then override manually
    assert client.post(f"/api/tracks/{tid}/conditions/fetch").json()["source"] == "open-meteo (test)"
    r = client.patch(f"/api/tracks/{tid}", json={"conditions": {"wind_kn": 18, "gust_kn": 25, "wind_dir": 340}})
    assert r.json()["conditions"]["source"] == "manual"
    assert client.get(f"/api/tracks/{tid}/analysis").json()["wind"]["prior_source"] == "manual"

    # gear
    board = client.post("/api/gear", json={"kind": "board", "name": "Foil board 105"}).json()
    sail = client.post("/api/gear", json={"kind": "sail", "name": "7.0", "brand": "X"}).json()
    assert client.post("/api/gear", json={"kind": "boat", "name": "x"}).status_code == 422
    client.patch(f"/api/tracks/{tid}", json={"gear_ids": [board["id"], sail["id"]]})
    client.patch(f"/api/tracks/{gpx}", json={"gear_ids": [board["id"]]})
    g = {x["id"]: x for x in client.get("/api/gear").json()}
    assert g[board["id"]]["sessions"] == 2 and g[board["id"]]["hours"] > 1
    assert g[sail["id"]]["sessions"] == 1
    assert [r["id"] for r in client.get(f"/api/tracks?gear_id={sail['id']}").json()] == [tid]

    # filters, cross-session views
    assert len(client.get("/api/tracks?wind_min=16").json()) == 1  # manual 18 kn on tid, 14 on gpx
    assert len(client.get("/api/tracks/trends?sport=windfoil").json()) == 2
    cmp_ = client.get(f"/api/tracks/compare?ids={tid},{gpx}").json()
    assert [c["track"]["id"] for c in cmp_] == [tid, gpx]
    polar = client.get("/api/tracks/polar?sport=windfoil").json()
    assert polar["sessions"] == 2 and polar["polar"]["best_upwind"]
    sm = client.get("/api/tracks/speedmap?spot=Puck bay").json()
    assert sm["cells"] and sm["track_ids"] == [tid]
    opts = client.get("/api/tracks/options").json()
    assert any(s["spot"] == "Puck bay" for s in opts["spots"])

    # race start + marks
    t0 = a["summary"]["start_utc"]
    client.patch(f"/api/tracks/{tid}", json={"settings": {"race_start": t0 + 600}})
    race = client.get(f"/api/tracks/{tid}/analysis").json()["race"]
    assert race["pre_start"]["duration_s"] == pytest.approx(600, abs=2)

    assert client.get(f"/api/tracks/{tid}/file").status_code == 200
    assert client.delete(f"/api/tracks/{tid}").json() == {"deleted": True}
    assert client.get(f"/api/tracks/{tid}").status_code == 404
    assert client.get("/api/gear").json()[0]["sessions"] == 1


def test_rejects_junk_upload(client):
    r = client.post("/api/tracks", files={"file": ("x.txt", b"hello")})
    assert r.status_code == 422
