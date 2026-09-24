import numpy as np
import pytest

from app.analysis import clean, settings, synthetic, wind
from app.analysis.geo import angdiff

T0 = 1_780_000_000.0


def _estimate(track, **ov):
    cfg = settings.resolve(ov, "windfoil")
    return wind.estimate(clean.clean(track, cfg), cfg)


@pytest.mark.parametrize("W", [0, 37, 90, 145, 230, 300, 355])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_constant_wind_within_5deg(W, seed):
    tr, truth = synthetic.course(lambda t: W, seed=seed)
    w = _estimate(tr)
    assert abs(angdiff(w["deg"], W)) <= 5, w
    assert w["source"] == "auto"
    assert w["confidence"] in ("high", "medium")


def test_wind_without_falls_or_spikes():
    tr, _ = synthetic.course(lambda t: 200, seed=9, falls=False, spikes=0)
    assert abs(angdiff(_estimate(tr)["deg"], 200)) <= 5


def test_shifting_wind_tracked_per_point():
    # 20 deg veer over the session
    tr, truth = synthetic.course(lambda t: 250 + 20 * (t - T0) / 5400, seed=3, laps=6)
    w = _estimate(tr)
    cfg = settings.resolve({}, "windfoil")
    c = clean.clean(tr, cfg)
    per_point = wind.estimate(c, cfg)["per_point"]
    err = np.abs(angdiff(per_point, truth.wind[: len(per_point)]))
    assert np.percentile(err, 95) <= 5
    assert err.max() <= 8
    assert len(w["series_deg"]) >= 5


def test_oscillating_wind():
    tr, truth = synthetic.course(lambda t: 90 + 8 * np.sin((t - T0) / 1800 * 2 * np.pi), seed=5, laps=6)
    cfg = settings.resolve({}, "windfoil")
    c = clean.clean(tr, cfg)
    pts = wind.estimate(c, cfg)["per_point"]
    err = np.abs(angdiff(pts, truth.wind[: len(pts)]))
    assert np.median(err) <= 5


def test_manual_override_wins():
    tr, _ = synthetic.course(lambda t: 100, seed=1)
    w = _estimate(tr, wind_override_deg=123)
    assert w["source"] == "manual" and w["deg"] == 123
    assert np.allclose(w["per_point"], 123)
    assert abs(angdiff(w["auto_deg"], 100)) <= 5  # auto estimate still reported


def test_keyframes_interpolate():
    tr, _ = synthetic.course(lambda t: 100, seed=1)
    frames = [{"t": tr.t[0], "deg": 350}, {"t": tr.t[-1], "deg": 10}]
    w = _estimate(tr, wind_keyframes=frames)
    pts = w["per_point"]
    assert w["source"] == "keyframes"
    assert abs(angdiff(pts[0], 350)) < 1 and abs(angdiff(pts[-1], 10)) < 1
    mid = pts[len(pts) // 2]
    assert abs(angdiff(mid, 0)) < 3  # goes through north, not the long way round


def test_beam_reaching_is_ambiguous_without_hint_and_resolved_with_one():
    tr, _ = synthetic.reaching(lambda t: 200, seed=4)
    no_hint = _estimate(tr)
    # the axis is right either way ...
    assert min(abs(angdiff(no_hint["deg"], 200)), abs(angdiff(no_hint["deg"], 20))) <= 5
    # ... but there's no evidence which end is upwind, and we say so
    assert no_hint["confidence"] == "low"
    hinted = _estimate(tr, wind_prior_deg=220, wind_prior_source="open-meteo")
    assert abs(angdiff(hinted["deg"], 200)) <= 5


def test_stale_device_hint_does_not_flip_clear_session():
    tr, _ = synthetic.course(lambda t: 185, seed=2)
    w = _estimate(tr, wind_prior_deg=20, wind_prior_source="device")
    assert abs(angdiff(w["deg"], 185)) <= 5
