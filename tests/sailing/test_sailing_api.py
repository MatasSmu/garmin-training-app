"""End to end on a throwaway database with a fake DeepSeek: gear codes and
setups -> tagging -> baselines -> analysis (stored, never re-run on its
own) -> weekly summary -> review -> coach focus."""

import base64
import json
import sqlite3
import threading
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.analysis import conditions, service
from app.api.deps import get_db
from app.coach.responder import _sailing_focus_briefing
from app.db import init_db
from app.main import app
from app.sailing import deepseek, prompts, reports

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"{config.APP_USERNAME}:{config.APP_PASSWORD}".encode()).decode()}

SESSION_REPLY = {
    "summary": "Solid 14 kn session.", "strengths": ["Upwind VMG up 4 %"], "weaknesses": "Gybes touched down",
    "vs_baseline": "Above the rolling average upwind.",
    "possible_causes": {"Technique": ["late weight shift"], "gear": [], "fatigue/recovery": ["HRV below average"]},
    "focus_next_session": ["10 foiling gybes each way"], "plan_adjustment": "none",
}
WEEK_REPLY = {"summary": "Good week.", "progress": ["VMG up"], "recurring_issues": ["gybes"],
              "focus_next_week": ["gybe drills"], "plan_adjustment": "add one light-wind session"}
REVIEW_REPLY = {"summary": "Steady month.", "trends": ["VMG improving"], "strengths": ["upwind"],
                "weaknesses": ["gybes"], "priorities": ["gybes"]}


class FakeDeepSeek:
    def __init__(self):
        self.calls = []
        self.bad = 0  # number of upcoming calls that return junk

    def __call__(self, body):
        self.calls.append(body)
        system = body["messages"][0]["content"]
        if self.bad:
            self.bad -= 1
            content = "not json {"
        elif prompts.WEEKLY_TASK in system:
            content = json.dumps(WEEK_REPLY)
        elif prompts.REVIEW_TASK in system:
            content = json.dumps(REVIEW_REPLY)
        else:
            content = json.dumps(SESSION_REPLY)
        return {"model": "deepseek-chat", "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1800, "completion_tokens": 300, "prompt_cache_hit_tokens": 1280,
                          "prompt_cache_miss_tokens": 520}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "t.db"

    def connect():
        c = sqlite3.connect(db, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    conn = connect()
    init_db(conn)

    def _db():
        yield conn

    monkeypatch.setattr(service, "TRACKS_DIR", tmp_path / "tracks")
    monkeypatch.setattr(conditions, "fetch", lambda *a, **k: {
        "source": "open-meteo (test)", "wind_kn": 14.0, "gust_kn": 18.0, "wind_dir": 300, "air_c": 16.0,
        "water_c": None, "hourly": []})
    monkeypatch.setattr(conditions, "place_name", lambda *a, **k: None)
    monkeypatch.setattr(reports, "CONNECT", connect)
    fake = FakeDeepSeek()
    app.dependency_overrides[get_db] = _db
    with TestClient(app, headers=AUTH) as c:
        yield c, conn, fake, monkeypatch
    app.dependency_overrides.clear()
    conn.close()


def _upload(client, name):
    r = client.post("/api/tracks", files={"file": (name, (SAMPLES / name).read_bytes())})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _enable_llm(monkeypatch, fake):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(deepseek, "post_chat", fake)


def _join_auto_threads():
    for t in threading.enumerate():
        if t.name.startswith("sail-analysis-"):
            t.join(timeout=30)


def test_gear_inventory_codes_and_setups(env):
    client, conn, _, _ = env
    fw = client.post("/api/gear", json={"kind": "front_wing", "code": "SC 392 899", "notes": "race wing"}).json()
    assert fw["code"] == "SC 392 899" and fw["name"] == "SC 392 899"  # stored as typed, name defaults to code
    dup = client.post("/api/gear", json={"kind": "front_wing", "code": "  sc392899 "})
    assert dup.status_code == 422 and "already in the inventory" in dup.json()["detail"]
    # the same code on another kind is a different part
    assert client.post("/api/gear", json={"kind": "back_wing", "code": "SC 392 899"}).status_code == 200
    assert client.post("/api/gear", json={"kind": "sail"}).status_code == 422  # needs a code or name
    sail = client.post("/api/gear", json={"kind": "sail", "code": "IQ 9.0 A"}).json()
    sail2 = client.post("/api/gear", json={"kind": "sail", "code": "IQ 8.0 B"}).json()
    board = client.post("/api/gear", json={"kind": "board", "code": "BRD 1"}).json()

    s = client.post("/api/gear/setups", json={"name": "Race 9.0", "gear_ids": [fw["id"], sail["id"], board["id"]]})
    assert s.status_code == 200 and {p["kind"] for p in s.json()["parts"]} == {"front_wing", "sail", "board"}
    sid = s.json()["id"]
    bad = client.post("/api/gear/setups", json={"name": "Two sails", "gear_ids": [sail["id"], sail2["id"]]})
    assert bad.status_code == 422
    # swap one part
    r = client.patch(f"/api/gear/setups/{sid}", json={"gear_ids": [fw["id"], sail2["id"], board["id"]]})
    assert {p["code"] for p in r.json()["parts"]} == {"SC 392 899", "IQ 8.0 B", "BRD 1"}
    assert r.json()["name"] == "Race 9.0"
    assert client.get("/api/tracks/options").json()["setups"][0]["id"] == sid
    assert client.delete(f"/api/gear/setups/{sid}").json() == {"deleted": True}
    assert client.get("/api/gear/setups").json() == []


def test_analysis_flow(env):
    client, conn, fake, monkeypatch = env
    ids = [_upload(client, n) for n in ("2026-08-30_liepaja_windfoil.fit", "2026-09-06_teizai_windfoil.fit",
                                        "2026-09-10_samylai_windfoil.fit", "2026-09-19_puck_windfoil.fit",
                                        "2026-09-22_puck_windfoil.fit")]
    last = ids[-1]
    fw = client.post("/api/gear", json={"kind": "front_wing", "code": "SC 392 899"}).json()
    sail = client.post("/api/gear", json={"kind": "sail", "code": "IQ 9.0"}).json()
    fw2 = client.post("/api/gear", json={"kind": "front_wing", "code": "SC 1"}).json()
    assert client.patch(f"/api/tracks/{last}", json={"gear_ids": [fw["id"], fw2["id"]]}).status_code == 422

    # before gear: band baseline, flagged as a fallback
    st = client.get(f"/api/sailing/sessions/{last}").json()
    assert st["eligible"] and st["band"] == "12-16" and st["baseline"]["fallback"] is True
    assert st["baseline"]["sessions"] == 4 and st["analysis"] is None

    for tid in ids[:3] + [last]:
        r = client.patch(f"/api/tracks/{tid}", json={"gear_ids": [fw["id"], sail["id"]]})
        assert r.status_code == 200
    base = {(b["band"], b["gear_key"]): b for b in client.get("/api/sailing/baselines").json()["baselines"]}
    assert base[("12-16", "")]["sessions"] == 5
    combo = base[("12-16", "front_wing=SC392899|sail=IQ9.0")]
    assert combo["sessions"] == 4 and combo["enough_data"] is True and combo["gear_label"] == "SC 392 899 + IQ 9.0"

    st = client.get(f"/api/sailing/sessions/{last}").json()
    assert st["baseline"]["level"] == "band+gear" and st["baseline"]["combo_sessions"] == 3
    assert set(st["comparison"]) == {"vmg_up_kn", "vmg_down_kn", "max_kn", "angle_up_deg"}

    # the packet: compact, no track points
    pk = client.get(f"/api/sailing/sessions/{last}/packet").json()
    assert pk["tokens"] < 1500
    txt = json.dumps(pk["packet"])
    assert "series" not in txt and "lat" not in pk["packet"] and "polar" not in txt
    assert pk["packet"]["gear"] == {"front_wing": "SC 392 899", "sail": "IQ 9.0"}
    assert pk["packet"]["baseline"]["level"] == "band+gear"
    assert "upwind" in pk["packet"]["port_vs_starboard"]

    # no key -> 503, nothing stored or logged
    assert client.post(f"/api/sailing/sessions/{last}/analyze").status_code == 503
    assert fake.calls == []

    _enable_llm(monkeypatch, fake)
    client.patch(f"/api/tracks/{last}", json={"notes": "Felt tired, mast rake +1 cm"})
    r = client.post(f"/api/sailing/sessions/{last}/analyze")
    assert r.status_code == 200, r.text
    a = r.json()["analysis"]
    assert a["result"]["weaknesses"] == ["Gybes touched down"]  # string coerced to list
    assert a["result"]["possible_causes"] == {"technique": ["late weight shift"], "gear": [],
                                              "fatigue_recovery": ["HRV below average"]}
    assert a["usage"]["prompt_tokens"] == 1800 and a["usage"]["cache_hit_tokens"] == 1280
    assert a["usage"]["cost_usd"] > 0 and a["stale"] is False
    # stable content first, packet last
    msgs = fake.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"].startswith(prompts.STABLE_PREFIX)
    assert "mast rake +1 cm" in msgs[1]["content"] and fake.calls[0]["response_format"] == {"type": "json_object"}

    # reading it again never calls the model
    client.get(f"/api/sailing/sessions/{last}")
    assert len(fake.calls) == 1

    # editing the note marks it out of date, but doesn't re-run it
    client.patch(f"/api/tracks/{last}", json={"notes": "Actually felt great"})
    st = client.get(f"/api/sailing/sessions/{last}").json()
    assert st["analysis"]["stale"] is True and len(fake.calls) == 1

    # invalid answers: retried once, then 502; the old analysis is kept; every attempt logged
    fake.bad = 2
    r = client.post(f"/api/sailing/sessions/{last}/analyze")
    assert r.status_code == 502 and "invalid answer" in r.json()["detail"]
    assert client.get(f"/api/sailing/sessions/{last}").json()["analysis"]["result"]["summary"] == "Solid 14 kn session."
    usage = client.get("/api/sailing/usage").json()
    assert usage["totals"]["calls"] == 3 and usage["totals"]["ok"] == 1

    # re-analyse on request
    assert client.post(f"/api/sailing/sessions/{last}/analyze").status_code == 200
    assert len(fake.calls) == 4

    # automatic first analysis: once, when gear is first saved on an unanalysed session
    first = ids[3]
    client.patch(f"/api/tracks/{first}", json={"gear_ids": [fw["id"], sail["id"]]})
    _join_auto_threads()
    assert client.get(f"/api/sailing/sessions/{first}").json()["analysis"] is not None
    n = len(fake.calls)
    client.patch(f"/api/tracks/{first}", json={"gear_ids": [fw["id"], sail["id"]]})
    _join_auto_threads()
    assert len(fake.calls) == n  # never re-run on its own

    # light wind: excluded from baselines and analysis
    client.patch(f"/api/tracks/{ids[0]}", json={"conditions": {"wind_kn": 5.0, "wind_dir": 200}})
    assert client.post(f"/api/sailing/sessions/{ids[0]}/analyze").status_code == 422
    assert client.get("/api/sailing/baselines").json()["baselines"][0]["sessions"] == 4

    # weekly summary from the stored analyses only
    w = client.post("/api/sailing/weekly", json={"week": "2026-09-22"})
    assert w.status_code == 200, w.text
    assert w.json()["week_start"] == "2026-09-21" and w.json()["track_ids"] == [last]
    week_msg = fake.calls[-1]["messages"][1]["content"]
    assert "Solid 14 kn session." in week_msg and '"session":{' not in week_msg
    assert client.post("/api/sailing/weekly", json={"week": "2026-08-03"}).status_code == 422

    # review from weekly summaries only
    r = client.post("/api/sailing/reviews", json={"kind": "month", "start": "2026-09-01", "end": "2026-09-30"})
    assert r.status_code == 200 and r.json()["weeks"] == ["2026-09-21"]
    assert "Good week." in fake.calls[-1]["messages"][1]["content"]
    assert "Solid 14 kn session." not in fake.calls[-1]["messages"][1]["content"]

    # the weekly planner (coach) sees the focus points
    focus = reports.planner_focus(conn, today=date(2026, 9, 23))
    assert focus["sessions"][0]["focus_next_session"] == ["10 foiling gybes each way"]
    assert focus["week"]["focus_next_week"] == ["gybe drills"]
    lines = "\n".join(_sailing_focus_briefing(focus))
    assert "10 foiling gybes each way" in lines and "gybe drills" in lines


def test_weekly_auto_runs_once_per_finished_week(env):
    client, conn, fake, monkeypatch = env
    tid = _upload(client, "2026-09-22_puck_windfoil.fit")
    assert reports.ensure_last_week(conn, today=date(2026, 9, 29)) is None  # no key
    _enable_llm(monkeypatch, fake)
    assert reports.ensure_last_week(conn, today=date(2026, 9, 29)) is None  # no analyses that week
    assert client.post(f"/api/sailing/sessions/{tid}/analyze").status_code == 200
    w = reports.ensure_last_week(conn, today=date(2026, 9, 29))
    assert w["week_start"] == "2026-09-21"
    n = len(fake.calls)
    assert reports.ensure_last_week(conn, today=date(2026, 9, 30)) is None and len(fake.calls) == n
