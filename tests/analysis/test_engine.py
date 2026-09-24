import json
from pathlib import Path

import numpy as np
import pytest

from app.analysis import clean, pipeline, settings, splits, state, synthetic
from app.analysis.geo import KN, unproject
from app.analysis.loader import Track, load_file

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


def straight_track(speeds, heading=90.0, t0=1_780_000_000.0, spikes=()):
    speeds = np.asarray(speeds, dtype=float)
    n = len(speeds)
    t = t0 + np.arange(n, dtype=float)
    d = np.concatenate([[0.0], np.cumsum(speeds[1:])])
    x, y = d * np.sin(np.radians(heading)), d * np.cos(np.radians(heading))
    lat, lon = unproject(x, y, 54.7, 18.4)
    sp = speeds.copy()
    for i, v in spikes:
        sp[i] = v
    return Track(t=t, lat=lat, lon=lon, speed=sp, hr=np.full(n, 150.0), temp=np.full(n, 18.0),
                 hdop=np.full(n, np.nan), meta={"sport_name": "Windfoiling"})


CFG = settings.resolve({}, "windfoil")


def test_speed_spikes_removed_but_real_crash_kept():
    v = np.full(200, 10.0)
    v[150:] = 3.0  # a crash: hard deceleration, no bounce back
    tr = straight_track(v, spikes=[(50, 22.0), (80, 1.0), (100, 20.0)])
    c = clean.clean(tr, CFG)
    assert c.report["speed_spikes_removed"] >= 3
    assert abs(c.speed[50] - 10) < 0.5 and abs(c.speed[80] - 10) < 0.5
    assert c.speed[160] == pytest.approx(3.0, abs=0.01)


def test_two_sample_spike_removed():
    v = np.full(100, 8.0)
    tr = straight_track(v, spikes=[(40, 18.0), (41, 17.0)])
    c = clean.clean(tr, CFG)
    assert c.speed[40] < 9 and c.speed[41] < 9


def test_position_jump_dropped_and_gap_splits():
    tr = straight_track(np.full(120, 10.0))
    tr.lat[60] += 0.01  # ~1.1 km teleport for one fix
    tr.t[90:] += 60.0   # a 60 s recording gap
    c = clean.clean(tr, CFG)
    assert c.report["position_jumps_dropped"] == 1
    assert len(c.segments) == 2
    assert c.dt[np.searchsorted(c.t, tr.t[90])] == 0  # no time credited across the gap


def test_heading_straight_line():
    c = clean.clean(straight_track(np.full(100, 9.0), heading=37.0), CFG)
    assert np.all(np.abs(((c.heading[5:-5] - 37 + 180) % 360) - 180) < 1.0)


def test_hdop_flags_bad_fixes():
    tr = straight_track(np.full(100, 9.0))
    tr.hdop[:] = 1.0
    tr.hdop[20:30] = 12.0
    c = clean.clean(tr, CFG)
    assert c.report["bad_hdop_fixes"] == 10
    assert not c.good[20:30].any()


def test_hysteresis():
    kn = np.array([5, 9, 10.5, 9, 8.5, 7.9, 9, 9.9, 10.1, 12, 7], dtype=float)
    on = state.hysteresis(kn / KN, 10 / KN, 8 / KN, [(0, len(kn))])
    assert on.tolist() == [False, False, True, True, True, False, False, False, True, True, False]


def test_foiling_summary_counts_touchdowns():
    v = np.r_[np.full(30, 3.0), np.full(60, 7.0), np.full(20, 3.0), np.full(60, 7.0), np.full(30, 3.0)]
    c = clean.clean(straight_track(v), CFG)
    on = state.hysteresis(c.speed_s, CFG["on_kn"] / KN, CFG["off_kn"] / KN, c.segments)
    s = state.summary(c, on, CFG)
    assert s["flights"] == 2 and s["touchdowns"] == 2
    assert s["time_s"] == pytest.approx(120, abs=4)


def test_splits_on_known_profile():
    v = np.r_[np.full(300, 8.0), np.full(20, 15.0), np.full(3700, 8.0)]
    c = clean.clean(straight_track(v), CFG)
    sp = {s["label"]: s for s in splits.best_splits(c)}
    assert sp["2 s"]["kn"] == pytest.approx(15 * KN, rel=0.02)
    assert sp["10 s"]["kn"] == pytest.approx(15 * KN, rel=0.02)
    assert sp["100 m"]["kn"] == pytest.approx(15 * KN, rel=0.03)
    assert sp["500 m"]["kn"] < sp["100 m"]["kn"]
    assert sp["1 h"]["kn"] == pytest.approx((8 * 3580 + 15 * 20) / 3600 * KN, rel=0.02)
    assert sp["1 nm"]["kn"] > 8 * KN


def test_pipeline_result_is_json_safe_and_consistent():
    tr, truth = synthetic.course(lambda t: 210, seed=7)
    r = pipeline.analyze(tr)
    json.dumps(r, allow_nan=False)
    s = r["summary"]
    assert s["tacks"] + s["gybes"] == len(r["maneuvers"])
    assert len(r["legs"]) == s["legs"]
    n = len(r["series"]["t"])
    assert all(len(v) == n for v in r["series"].values())
    # polar: upwind best VMG angle near the simulated 48 deg, downwind near 140
    assert 35 <= r["polar"]["best_upwind"]["twa"] <= 55
    assert 130 <= r["polar"]["best_downwind"]["twa"] <= 155
    # cumulative gain is the integral of VMG; legs' gains are pieces of it
    assert r["series"]["gain"][-1] == pytest.approx(sum(np.array(r["series"]["vmg"]) / KN), rel=0.05, abs=50)
    # HR zones cover the recorded time
    zt = sum(z["time_s"] for z in r["hr"]["zones"])
    assert zt == pytest.approx(s["recorded_s"], rel=0.02)
    # targets on legs
    up = [lg for lg in r["legs"] if lg["type"] == "upwind"]
    assert all(lg["pct_target_vmg"] is not None for lg in up)


def test_race_with_user_marks():
    tr, _ = synthetic.course(lambda t: 0, seed=2, laps=2)
    r0 = pipeline.analyze(tr)
    rounds = r0["race"]["roundings"]
    assert len(rounds) >= 3  # windward, leeward, windward ... detected automatically
    assert rounds[0]["name"] == "Windward"
    marks = [{"lat": rounds[0]["lat"], "lon": rounds[0]["lon"], "name": "W"},
             {"lat": rounds[1]["lat"], "lon": rounds[1]["lon"], "name": "L"}]
    start = tr.t[0] + 30
    r = pipeline.analyze(tr, {"race_start": start, "marks": marks})
    race = r["race"]
    assert race["pre_start"]["duration_s"] == pytest.approx(30, abs=1)
    names = [x["name"] for x in race["roundings"]]
    assert names[:2] == ["W", "L"]
    assert race["legs"][0]["from"] == "Start" and race["legs"][0]["to"] == "W"


def test_manual_targets():
    tr, _ = synthetic.course(lambda t: 90, seed=1)
    r = pipeline.analyze(tr, {"targets": {"mode": "manual", "upwind_kn": 20, "upwind_twa": 45,
                                          "downwind_kn": 25, "downwind_twa": 140}})
    assert r["targets"]["source"] == "manual"
    assert r["targets"]["upwind_vmg_kn"] == pytest.approx(20 * np.cos(np.radians(45)), abs=0.01)


@pytest.mark.skipif(not SAMPLES.exists(), reason="no samples dir")
@pytest.mark.parametrize("path", sorted(SAMPLES.glob("*.fit"))[:1] + sorted(SAMPLES.glob("*.gpx"))[:1],
                         ids=lambda p: p.name)
def test_loader_reads_real_files(path):
    tr = load_file(path)
    assert len(tr) > 1000
    assert np.all(np.diff(tr.t) > 0)
    assert np.isfinite(tr.lat).all() and np.isfinite(tr.lon).all()
    assert np.isfinite(tr.hr).mean() > 0.9
    assert tr.meta.get("sport_name")
