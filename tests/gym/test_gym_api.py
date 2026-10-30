"""Gym API: idempotent sync, edits, last numbers, Garmin link and analysis."""

import copy
import json
import uuid

import pytest


def _day_id(client, code):
    return next(d["id"] for d in client.get("/api/gym/bootstrap").json()["days"] if d["code"] == code)


def _payload(day_id, sid=None, location="bsb", started="2026-11-02T13:05:00Z", ended="2026-11-02T14:20:00Z",
             updated="2026-11-02T14:20:05Z", sets=None, locations=None):
    sid = sid or str(uuid.uuid4())
    if sets is None:
        sets = [
            {"id": f"{sid}-sq{n}", "exercise_id": "back_squat", "set_number": n, "reps": 10, "weight_kg": 80,
             "completed_as_planned": True, "done_at": started}
            for n in (1, 2, 3)
        ] + [
            {"id": f"{sid}-bp1", "exercise_id": "bench_press", "set_number": 1, "reps": 9, "weight_kg": 62.5,
             "completed_as_planned": False, "rpe": 4},
            {"id": f"{sid}-pl1", "exercise_id": "plank", "set_number": 1, "duration_sec": 150, "completed_as_planned": True},
        ]
    return {
        "locations": locations or [],
        "sessions": [{"id": sid, "plan_day_id": day_id, "location_id": location, "started_at": started,
                      "ended_at": ended, "notes": "felt good", "updated_at": updated, "sets": sets}],
    }


def _counts(conn, sid):
    return (
        conn.execute("SELECT COUNT(*) FROM gym_sessions WHERE id = ?", (sid,)).fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM gym_set_logs WHERE session_id = ?", (sid,)).fetchone()[0],
    )


def test_bootstrap_has_plan_for_offline_use(client):
    b = client.get("/api/gym/bootstrap").json()
    assert [d["code"] for d in b["days"]] == ["A", "B", "C", "D"]
    assert [len(d["items"]) for d in b["days"]] == [10, 10, 11, 6]
    assert len(b["exercises"]) == 29
    assert {loc["id"] for loc in b["locations"]} == {"bsb", "zb"}
    assert b["exercises"]["bulgarian_split_squat"]["per_side"] is True
    assert b["last"] == {}


def test_sync_stores_session_with_sets(client, conn):
    p = _payload(_day_id(client, "A"))
    r = client.post("/api/gym/sync", json=p)
    assert r.status_code == 200, r.text
    sid = p["sessions"][0]["id"]
    assert r.json()["results"] == {sid: "created"}
    assert _counts(conn, sid) == (1, 5)
    detail = client.get(f"/api/gym/sessions/{sid}").json()
    assert detail["day_code"] == "A" and detail["location"] == "Sporto salė (BSB)"
    assert detail["duration_min"] == 75 and detail["notes"] == "felt good"
    squat = next(e for e in detail["exercises"] if e["exercise_id"] == "back_squat")
    assert squat["target"] == "3 × 8–10 @ 80–90 kg"
    assert [s["reps"] for s in squat["sets"]] == [10, 10, 10]
    bench = next(e for e in detail["exercises"] if e["exercise_id"] == "bench_press")
    assert bench["sets"][0] == {"set_number": 1, "reps": 9, "weight_kg": 62.5, "duration_sec": None, "rpe": 4,
                                "completed_as_planned": False}


def test_same_payload_twice_is_one_session_no_duplicate_sets(client, conn):
    p = _payload(_day_id(client, "A"))
    sid = p["sessions"][0]["id"]
    first = client.post("/api/gym/sync", json=p)
    second = client.post("/api/gym/sync", json=copy.deepcopy(p))
    assert first.status_code == second.status_code == 200
    assert second.json()["results"] == {sid: "unchanged"}
    assert _counts(conn, sid) == (1, 5)
    assert conn.execute("SELECT COUNT(*) FROM gym_sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM gym_set_logs").fetchone()[0] == 5


def test_resync_with_changes_updates_and_removes_unticked_sets(client, conn):
    p = _payload(_day_id(client, "A"))
    sid = p["sessions"][0]["id"]
    client.post("/api/gym/sync", json=p)
    p2 = copy.deepcopy(p)
    s = p2["sessions"][0]
    s["updated_at"] = "2026-11-02T14:30:00Z"
    s["sets"] = [x for x in s["sets"] if x["id"] != f"{sid}-sq3"]  # un-ticked
    s["sets"][0]["reps"] = 8
    assert client.post("/api/gym/sync", json=p2).json()["results"] == {sid: "updated"}
    assert _counts(conn, sid) == (1, 4)
    assert conn.execute("SELECT reps FROM gym_set_logs WHERE id = ?", (f"{sid}-sq1",)).fetchone()[0] == 8


def test_new_location_from_phone_is_synced_once(client, conn):
    loc = {"id": str(uuid.uuid4()), "name": "Hotel gym"}
    p = _payload(_day_id(client, "B"), location=loc["id"], locations=[loc])
    client.post("/api/gym/sync", json=p)
    client.post("/api/gym/sync", json=p)
    assert conn.execute("SELECT COUNT(*) FROM gym_locations WHERE name = 'Hotel gym'").fetchone()[0] == 1
    detail = client.get(f"/api/gym/sessions/{p['sessions'][0]['id']}").json()
    assert detail["location"] == "Hotel gym"


def test_edit_location_and_times(client, conn):
    p = _payload(_day_id(client, "A"))
    sid = p["sessions"][0]["id"]
    client.post("/api/gym/sync", json=p)
    r = client.patch(f"/api/gym/sessions/{sid}", json={
        "location_id": "zb", "started_at": "2026-11-02T13:30:00Z", "ended_at": "2026-11-02T15:00:00+00:00"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["location_id"] == "zb" and d["location"] == "Sporto salė (ŽB)"
    assert d["started_at"] == "2026-11-02T13:30:00Z" and d["ended_at"] == "2026-11-02T15:00:00Z"
    assert d["duration_min"] == 90
    assert _counts(conn, sid) == (1, 5)  # sets untouched

    # An older copy still on the phone must not undo the edit; the server copy goes back.
    resync = client.post("/api/gym/sync", json=p).json()
    assert resync["results"] == {sid: "stale"}
    assert resync["server_copies"][0]["location_id"] == "zb"
    assert client.get(f"/api/gym/sessions/{sid}").json()["location_id"] == "zb"

    assert client.patch(f"/api/gym/sessions/{sid}", json={"location_id": "nowhere"}).status_code == 400
    assert client.patch("/api/gym/sessions/missing", json={"notes": "x"}).status_code == 404
    assert client.patch(f"/api/gym/sessions/{sid}", json={"started_at": "not a time"}).status_code == 422


def test_last_numbers_across_days(client):
    a = _payload(_day_id(client, "A"))
    client.post("/api/gym/sync", json=a)
    d_id = _day_id(client, "D")
    sid = str(uuid.uuid4())
    d = _payload(d_id, sid=sid, started="2026-11-07T08:45:00Z", ended="2026-11-07T09:45:00Z",
                 updated="2026-11-07T09:46:00Z", sets=[
                     {"id": f"{sid}-1", "exercise_id": "bench_press", "set_number": 1, "reps": 5, "weight_kg": 70},
                     {"id": f"{sid}-2", "exercise_id": "bench_press", "set_number": 2, "reps": 4, "weight_kg": 70}])
    client.post("/api/gym/sync", json=d)
    last = client.get("/api/gym/bootstrap").json()["last"]
    assert last["bench_press"]["day_code"] == "D"
    assert [(s["reps"], s["weight_kg"]) for s in last["bench_press"]["sets"]] == [(5, 70), (4, 70)]
    assert last["back_squat"]["day_code"] == "A"


def test_analysis_endpoints(client):
    client.post("/api/gym/sync", json=_payload(_day_id(client, "A")))
    rows = client.get("/api/gym/sessions").json()
    assert len(rows) == 1
    s = rows[0]
    assert (s["sets_done"], s["sets_planned"]) == (5, 27)
    assert s["plan_week"] == 1
    assert s["volume_kg"] == round(3 * 10 * 80 + 9 * 62.5)
    hist = client.get("/api/gym/exercises/back_squat/history").json()["history"]
    assert hist[0]["top_weight_kg"] == 80
    assert hist[0]["e1rm_kg"] == pytest.approx(round(80 * (1 + 10 / 30), 1))
    vol = client.get("/api/gym/volume").json()
    assert vol["weeks"][0]["week"] == 1 and vol["weeks"][0]["sets"] == 5
    assert client.get("/api/gym/exercises/nope/history").status_code == 404
    ex = {e["id"]: e for e in client.get("/api/gym/exercises").json()}
    assert ex["back_squat"]["sessions"] == 1 and ex["leg_press"]["sessions"] == 0


def _garmin(conn, activity_id, start_gmt, minutes, avg_hr=120, max_hr=160, kind="strength_training"):
    local = start_gmt.replace("T", " ")
    conn.execute(
        """INSERT INTO sessions (activity_id, activity_type, activity_name, start_time_local, start_date,
                                 duration_s, avg_hr, max_hr, raw_json, updated_at)
           VALUES (?, ?, 'Strength', ?, ?, ?, ?, ?, ?, 'x')""",
        (activity_id, kind, local, local[:10], minutes * 60, avg_hr, max_hr,
         json.dumps({"startTimeGMT": local})),
    )
    conn.commit()


def test_garmin_link_overlap(client, conn):
    _garmin(conn, 1, "2026-11-02T12:50:00", 90, avg_hr=118, max_hr=171)   # overlaps
    _garmin(conn, 2, "2026-11-02T18:00:00", 60)                            # same day, too late
    _garmin(conn, 3, "2026-11-02T13:00:00", 60, kind="road_biking")         # not strength
    p = _payload(_day_id(client, "A"))
    client.post("/api/gym/sync", json=p)
    d = client.get(f"/api/gym/sessions/{p['sessions'][0]['id']}").json()
    assert d["garmin"] == {"activity_id": 1, "name": "Strength", "duration_min": 90, "avg_hr": 118,
                           "max_hr": 171, "calories": None}


def test_garmin_no_match_stays_empty_and_links_later(client, conn):
    p = _payload(_day_id(client, "A"))
    sid = p["sessions"][0]["id"]
    client.post("/api/gym/sync", json=p)
    assert client.get(f"/api/gym/sessions/{sid}").json()["garmin"] is None
    # Within 30 min after the end still counts; Garmin data arriving later is picked up.
    _garmin(conn, 7, "2026-11-02T14:45:00", 20)
    assert client.get("/api/gym/sessions").json()[0]["garmin"]["activity_id"] == 7


def test_sync_rejects_bad_payload(client):
    bad = _payload(_day_id(client, "A"))
    bad["sessions"][0]["sets"][0]["reps"] = -1
    assert client.post("/api/gym/sync", json=bad).status_code == 422
    bad = _payload(_day_id(client, "A"))
    del bad["sessions"][0]["updated_at"]
    assert client.post("/api/gym/sync", json=bad).status_code == 422
