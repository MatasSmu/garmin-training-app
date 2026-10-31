"""Wind steadiness rating and the doubtful-wind check."""

from app.analysis.pipeline import wind_suspect
from app.analysis.wind import steadiness


def test_steadiness_labels():
    assert steadiness([180, 182, 179, 183, 181], 180, 4.0)["label"] == "steady"
    s = steadiness([180, 190, 200, 205, 198], 180, 4.0)           # 25 deg swing, 10 deg per 4 min
    assert (s["range_deg"], s["rate_deg_4min"], s["label"]) == (25, 10, "shifty")
    assert steadiness([180, 195, 182, 196, 181], 180, 4.0)["label"] == "very_shifty"   # 15 deg jumps
    assert steadiness([170, 180, 190, 200, 205], 180, 4.0)["label"] == "very_shifty"   # 35 deg swing
    assert steadiness([180, 181], 180, 4.0)["label"] is None                          # too short to tell
    # rate is per 4 minutes whatever the window step
    assert steadiness([180, 184, 188, 192], 180, 2.0)["rate_deg_4min"] == 8


def test_steadiness_across_north():
    s = steadiness([355, 2, 358, 4], 0, 4.0)
    assert s["range_deg"] == 9 and s["label"] == "steady"


def _pos(pt, pa, st, sa):
    return {"upwind": {"port": {"time_s": pt, "avg_twa": pa}, "starboard": {"time_s": st, "avg_twa": sa}}}


def test_wind_suspect():
    assert wind_suspect(_pos(900, 55, 1000, 52)) is None
    assert wind_suspect(_pos(81, 66, 615, 58)) is None                       # short, but both tacks sailed
    assert "only one tack" in wind_suspect(_pos(689, 37, 22, 2))             # Cadiz 2026-10-17
    assert "no-go" in wind_suspect(_pos(600, 25, 700, 55))
    assert wind_suspect({}) is None
