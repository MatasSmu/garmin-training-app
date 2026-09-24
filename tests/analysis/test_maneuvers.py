import pytest

from app.analysis import pipeline, synthetic


def _match(truth, detected, tol_s=10.0):
    used, hits, type_ok = set(), 0, 0
    for tc, kind in truth:
        cands = [(abs(m["tc"] - tc), k) for k, m in enumerate(detected) if k not in used and abs(m["tc"] - tc) <= tol_s]
        if not cands:
            continue
        _, k = min(cands)
        used.add(k)
        hits += 1
        type_ok += detected[k]["type"] == kind
    return hits, type_ok, len(detected) - len(used)


@pytest.fixture(scope="module")
def runs():
    out = []
    for W in (0, 90, 230, 300):
        for seed in range(4):
            tr, truth = synthetic.course(lambda t, W=W: W, seed=seed)
            out.append((truth, pipeline.analyze(tr)))
    return out


def test_detection_rate_at_least_95pct(runs):
    total = hits = type_ok = extra = 0
    for truth, r in runs:
        h, ok, fp = _match(truth.maneuvers, r["maneuvers"])
        total += len(truth.maneuvers)
        hits += h
        type_ok += ok
        extra += fp
    assert hits / total >= 0.95
    assert type_ok / hits >= 0.95
    assert extra / total <= 0.05


def test_maneuver_metrics_are_sane(runs):
    for _, r in runs:
        for m in r["maneuvers"]:
            assert m["t0"] <= m["tc"] <= m["t1"]
            assert m["min_kn"] <= m["entry_kn"] + 0.01
            assert 0 < m["duration_s"] <= 60
            assert m["type"] in ("tack", "gybe")
            assert m["from_side"] in ("port", "starboard")


def test_tacks_lose_more_than_gybes_in_synthetic(runs):
    # the simulator drops tacks to 1-4.5 m/s and gybes only to 6.5-8.5 m/s
    for _, r in runs:
        s = r["summary"]
        assert s["tack_min_kn"] < s["gybe_min_kn"]
        assert s["tack_loss_m"] > s["gybe_loss_m"]
        assert s["gybe_success_pct"] > s["tack_success_pct"]


def test_legs_split_at_maneuvers_and_classified(runs):
    truth, r = runs[0]
    legs = r["legs"]
    assert len(legs) >= len(truth.maneuvers)
    types = {lg["type"] for lg in legs}
    assert {"upwind", "downwind"} <= types
    for lg in legs:
        if lg["type"] == "upwind":
            assert lg["avg_twa"] < 75 and lg["gain_m"] > 0
        if lg["type"] == "downwind":
            assert lg["avg_twa"] > 105 and lg["gain_m"] < 0
    # legs never overlap a maneuver's turn
    for m in r["maneuvers"]:
        for lg in legs:
            assert not (lg["i0"] < m["ic"] < lg["i1"])
