"""Baseline calculation: bests, rolling averages, counts, band + gear
combos, the < 3 sessions fallback, and point-in-time comparisons."""

import pytest

from app.sailing.baselines import baseline_for, compare, compute_all, group_stats, pct_diff, session_metrics
from app.sailing.config import MIN_COMBO_SESSIONS, ROLLING_N

WING_A = {"front_wing": "SC 392 899", "sail": "IQ 9.0", "board": "B1"}
WING_B = {"front_wing": "SC 111 222", "sail": "IQ 9.0", "board": "B1"}


def pt(i, wind, up=10.0, down=14.0, mx=25.0, height=35.0, gear=None):
    return {"id": i, "start_utc": 1_000_000 + i * 86400, "wind_kn": wind, "gear": gear or {},
            "metrics": {"vmg_up_kn": up, "vmg_down_kn": down, "max_kn": mx, "height_up_deg": height}}


def test_group_stats_bests_and_counts():
    pts = [pt(1, 13, up=9.0, mx=26, height=38), pt(2, 13, up=10.5, mx=24, height=32), pt(3, 14, up=9.5, mx=27.5, height=35)]
    s = group_stats(pts)
    assert s["sessions"] == 3 and s["rolling_n"] == 3
    assert s["best"] == {"vmg_up_kn": 10.5, "vmg_down_kn": 14.0, "max_kn": 27.5, "height_up_deg": 38}
    assert s["rolling_avg"]["vmg_up_kn"] == pytest.approx(9.67, abs=0.01)
    assert s["rolling_avg"]["height_up_deg"] == pytest.approx(35.0)


def test_best_upwind_height_is_the_highest():
    # height = 90 - upwind TWA: 40.5 deg height (49.5 deg TWA) points closest to the wind
    s = group_stats([pt(1, 10, height=30), pt(2, 10, height=40.5), pt(3, 10, height=37)])
    assert s["best"]["height_up_deg"] == 40.5


def test_rolling_average_uses_only_the_latest_ten():
    # 5 old slow sessions, then 10 fast ones: the average must ignore the old ones
    pts = [pt(i, 13, up=5.0) for i in range(5)] + [pt(i, 13, up=10.0) for i in range(5, 15)]
    s = group_stats(list(reversed(pts)))  # input order doesn't matter, start time does
    assert ROLLING_N == 10
    assert s["sessions"] == 15 and s["rolling_n"] == 10
    assert s["rolling_avg"]["vmg_up_kn"] == 10.0
    assert s["best"]["vmg_up_kn"] == 10.0


def test_missing_metric_is_skipped_not_zero():
    a, b = pt(1, 13, up=8.0), pt(2, 13)
    b["metrics"]["vmg_up_kn"] = None
    s = group_stats([a, b])
    assert s["rolling_avg"]["vmg_up_kn"] == 8.0 and s["best"]["vmg_up_kn"] == 8.0
    empty = group_stats([])
    assert empty["sessions"] == 0 and empty["best"]["max_kn"] is None and empty["rolling_avg"]["max_kn"] is None


def test_compute_all_groups_by_band_and_combo_and_excludes_light_wind():
    pts = [pt(1, 7, gear=WING_A), pt(2, 8, gear=WING_A), pt(3, 13, gear=WING_A), pt(4, 13, gear=WING_B),
           pt(5, 14, gear=WING_A), pt(6, 15), pt(7, 5.5, gear=WING_A), pt(8, None, gear=WING_A)]
    rows = {(r["band"], r["gear_key"]): r for r in compute_all(pts)}
    assert rows[("6-9", "")]["sessions"] == 2
    assert rows[("12-16", "")]["sessions"] == 4
    combo_a = rows[("12-16", "front_wing=SC392899|sail=IQ9.0")]
    assert combo_a["sessions"] == 2 and combo_a["enough_data"] is False
    assert combo_a["gear_label"] == "SC 392 899 + IQ 9.0"
    assert rows[("12-16", "front_wing=SC111222|sail=IQ9.0")]["sessions"] == 1
    assert not any(b == "9-12" for b, _ in rows)  # no sessions there
    assert sum(r["sessions"] for (b, k), r in rows.items() if k == "") == 6  # 5.5 kn and no-wind excluded


def test_combo_with_enough_sessions_is_used():
    history = [pt(i, 13, up=9.0 + i * 0.1, gear=WING_A) for i in range(1, 4)] + [pt(4, 13, up=6.0, gear=WING_B)]
    session = pt(10, 14, up=10.0, gear={"front_wing": "sc392899", "sail": "iq 9.0"})
    b = baseline_for(history, session)
    assert MIN_COMBO_SESSIONS == 3
    assert b["level"] == "band+gear" and b["fallback"] is False and b["fallback_reason"] is None
    assert b["sessions"] == 3 and b["combo_sessions"] == 3
    assert b["rolling_avg"]["vmg_up_kn"] == pytest.approx(9.2)  # wing B's slow session left out


def test_fewer_than_three_combo_sessions_falls_back_to_band():
    history = [pt(1, 13, gear=WING_A), pt(2, 13, gear=WING_A), pt(3, 13, gear=WING_B), pt(4, 12.5)]
    b = baseline_for(history, pt(10, 14, gear=WING_A))
    assert b["level"] == "band" and b["fallback"] is True
    assert b["combo_sessions"] == 2 and b["sessions"] == 4
    assert "only 2 earlier session" in b["fallback_reason"]


def test_untagged_session_falls_back_and_says_why():
    b = baseline_for([pt(1, 13, gear=WING_A)] * 1, pt(10, 13, gear={"front_wing": "SC 392 899"}))
    assert b["fallback"] is True and b["gear_key"] is None and "gear not tagged" in b["fallback_reason"]


def test_baseline_only_counts_earlier_sessions_in_the_same_band():
    history = [pt(1, 13, up=8.0), pt(2, 17, up=12.0), pt(20, 13, up=11.0)]  # id 20 is later than the session
    b = baseline_for(history + [pt(10, 13, up=9.0)], pt(10, 13, up=9.0))
    assert b["band"] == "12-16" and b["sessions"] == 1
    assert b["rolling_avg"]["vmg_up_kn"] == 8.0


def test_session_below_six_knots_has_no_baseline():
    assert baseline_for([pt(1, 13)], pt(10, 5.9)) is None
    assert baseline_for([pt(1, 13)], pt(10, None)) is None


def test_compare_pct_direction_and_new_bests():
    base = group_stats([pt(1, 13, up=10.0, down=14.0, mx=25.0, height=35.0)])
    cmp_ = compare(pt(10, 13, up=10.5, down=13.3, mx=26.0, height=37.0), base)
    assert cmp_["vmg_up_kn"]["pct_vs_avg"] == 5.0 and cmp_["vmg_up_kn"]["better"] is True
    assert cmp_["vmg_down_kn"]["pct_vs_avg"] == -5.0 and cmp_["vmg_down_kn"]["better"] is False
    # higher height = pointing closer to the wind = better, like every other metric
    assert cmp_["height_up_deg"]["pct_vs_avg"] == pytest.approx(5.7) and cmp_["height_up_deg"]["better"] is True
    assert cmp_["height_up_deg"]["new_best"] is True
    lower = compare(pt(11, 13, height=33.0), base)["height_up_deg"]
    assert lower["better"] is False and lower["new_best"] is False
    assert cmp_["max_kn"]["new_best"] is True and cmp_["vmg_down_kn"]["new_best"] is False
    assert compare(pt(10, 13), None)["max_kn"]["pct_vs_avg"] is None


def test_pct_diff_and_metrics_extraction():
    assert pct_diff(11, 10) == 10.0 and pct_diff(None, 10) is None and pct_diff(5, 0) is None
    m = session_metrics({"upwind_vmg_kn": 9.33, "downwind_vmg_kn": 11.98, "max_kn": 25.52, "upwind_twa": 53.1})
    assert m == {"vmg_up_kn": 9.33, "vmg_down_kn": 11.98, "max_kn": 25.52, "height_up_deg": 36.9}  # 90 - 53.1
    assert session_metrics(None)["max_kn"] is None


def test_deterministic():
    pts = [pt(i, 6 + i % 15, up=8 + (i % 7) * 0.3, gear=WING_A if i % 2 else WING_B) for i in range(40)]
    assert compute_all(pts) == compute_all(list(reversed(pts)))
