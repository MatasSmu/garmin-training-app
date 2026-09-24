"""Wind bands: edges, the 6 kn floor, open-ended top band, custom bands."""

import pytest

from app.sailing.baselines import BAND_LABELS, band_label, wind_band
from app.sailing.config import MIN_WIND_KN, WIND_BANDS


def test_default_bands():
    assert BAND_LABELS == ["6-9", "9-12", "12-16", "16-20", "20+"]
    assert MIN_WIND_KN == 6


@pytest.mark.parametrize("kn, band", [
    (6.0, "6-9"), (8.99, "6-9"), (9.0, "9-12"), (11.9, "9-12"), (12.0, "12-16"),
    (15.99, "12-16"), (16.0, "16-20"), (19.9, "16-20"), (20.0, "20+"), (35.0, "20+"),
])
def test_lower_edge_inclusive_upper_exclusive(kn, band):
    assert wind_band(kn) == band


@pytest.mark.parametrize("kn", [None, 0.0, 3.2, 5.99])
def test_below_six_or_unknown_is_excluded(kn):
    assert wind_band(kn) is None


def test_bands_are_configurable():
    bands = ((8, 14), (14, None))
    assert wind_band(7.9, bands) is None
    assert wind_band(10, bands) == "8-14"
    assert wind_band(30, bands) == "14+"
    assert band_label(12.5, 15) == "12.5-15"


def test_bands_are_contiguous_and_ordered():
    for (lo, hi), (lo2, _) in zip(WIND_BANDS, WIND_BANDS[1:]):
        assert hi == lo2 and lo < hi
    assert WIND_BANDS[-1][1] is None
