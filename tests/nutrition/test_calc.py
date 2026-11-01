"""Pure maths: trend EMA, weekly rate, status, projection, expenditure, advice."""

from datetime import date, timedelta

import pytest

from app.nutrition import calc

D0 = date(2026, 11, 2)


def d(i):
    return D0 + timedelta(days=i)


def test_trend_ema_starts_at_first_weight_and_smooths_by_a_tenth():
    s = calc.trend_series({d(0): 80.0, d(1): 81.0, d(2): 81.0})
    assert [r["trend"] for r in s] == pytest.approx([80.0, 80.1, 80.19])
    assert [r["weight"] for r in s] == [80.0, 81.0, 81.0]


def test_trend_ema_interpolates_missing_days():
    s = calc.trend_series({d(0): 80.0, d(2): 81.0})
    assert [r["date"] for r in s] == [d(0), d(1), d(2)]
    assert [r["interpolated"] for r in s] == [False, True, False]
    assert s[1]["weight"] is None
    # day 1 is filled with 80.5 (half way), then day 2's real 81.0
    assert s[1]["trend"] == pytest.approx(80.05)
    assert s[2]["trend"] == pytest.approx(80.05 + 0.1 * (81.0 - 80.05))


def test_trend_ema_long_gap_moves_smoothly_and_never_extrapolates():
    s = calc.trend_series({d(0): 80.0, d(10): 82.0})
    assert len(s) == 11 and s[-1]["date"] == d(10)
    trends = [r["trend"] for r in s]
    assert trends == sorted(trends)  # rises steadily through the gap
    assert trends[-1] < 82.0


def test_trend_ema_empty():
    assert calc.trend_series({}) == []


def series_with_slope(per_day, n):
    return [{"date": d(i), "trend": 80 + per_day * i} for i in range(n)]


def test_weekly_rate_over_last_14_days():
    assert calc.weekly_rate(series_with_slope(0.1, 30)) == pytest.approx(0.7)


def test_weekly_rate_uses_fewer_days_but_needs_seven():
    assert calc.weekly_rate(series_with_slope(0.1, 6)) is None
    assert calc.weekly_rate(series_with_slope(0.1, 7)) == pytest.approx(0.7)
    assert calc.weekly_rate(series_with_slope(-0.05, 9)) == pytest.approx(-0.35)


def test_weekly_rate_only_looks_at_recent_days():
    flat_then_rising = [{"date": d(i), "trend": 80 if i < 20 else 80 + 0.1 * (i - 19)} for i in range(34)]
    assert calc.weekly_rate(flat_then_rising) == pytest.approx(0.7)


@pytest.mark.parametrize("rate,expected", [
    (0.625, "on_track"), (0.4, "on_track"), (0.85, "on_track"),
    (0.3, "too_slow"), (-0.2, "too_slow"),
    (0.9, "too_fast"), (1.5, "too_fast"),
    (None, "no_data"),
])
def test_status_against_target_rate(rate, expected):
    assert calc.status(rate, 0.625, 82.0, 86.0) == expected


def test_status_goal_reached_wins():
    assert calc.status(0.1, 0.625, 86.1, 86.0) == "reached"
    assert calc.status(None, 0.625, 86.0, 86.0) == "reached"


def test_status_for_a_losing_plan():
    assert calc.status(-0.5, -0.5, 80, 75) == "on_track"
    assert calc.status(-1.2, -0.5, 80, 75) == "too_fast"
    assert calc.status(0.0, -0.5, 80, 75) == "too_slow"


def test_projected_date():
    assert calc.projected_date(83.0, 86.0, 0.6, D0) == D0 + timedelta(days=35)
    assert calc.projected_date(83.0, 86.0, 0.0, D0) is None
    assert calc.projected_date(83.0, 86.0, -0.3, D0) is None
    assert calc.projected_date(None, 86.0, 0.6, D0) is None


def test_target_line():
    start, end = date(2026, 11, 1), date(2026, 12, 27)
    assert calc.target_line(81, start, 86, end, start) == 81
    assert calc.target_line(81, start, 86, end, end) == 86
    assert calc.target_line(81, start, 86, end, start + timedelta(days=28)) == pytest.approx(83.5)


def test_evening_difference():
    m = {d(0): 80.0, d(1): 80.4, d(2): 80.2}
    e = {d(0): 81.2, d(1): 81.2, d(5): 90}
    assert calc.evening_difference(m, e) == pytest.approx(1.0)
    assert calc.evening_difference(m, {}) is None


def test_expenditure_formula():
    # ate 4000/day for 14 days and the trend rose 0.5 kg: 0.5 * 7700 / 14 = 275 stored per day
    assert calc.expenditure(4000, 0.5, 14) == pytest.approx(3725)
    assert calc.expenditure(3000, -0.7, 7) == pytest.approx(3770)


def test_kcal_adjustment_and_advice():
    assert calc.kcal_adjustment(0.6, 0.3) == pytest.approx(330)
    assert calc.kcal_adjustment(0.6, 0.9) == pytest.approx(-330)
    assert calc.rounded_range(330) == (300, 400)
    assert calc.rounded_range(300) == (250, 350)
    assert calc.advice_text(0.3, 0.6) == "gaining 0.3 kg/week → eat roughly 300–400 kcal more per day"
    assert "kcal less per day" in calc.advice_text(1.0, 0.6)
    assert "keep eating" in calc.advice_text(0.6, 0.625)


def test_food_scaling_totals_and_meal_slots():
    oats = {"kcal_100g": 380, "protein_100g": 13, "carbs_100g": 60, "fat_100g": None}
    assert calc.scale_food(oats, 80) == {"kcal": 304.0, "protein_g": 10.4, "carbs_g": 48.0, "fat_g": None}
    assert calc.totals([{"kcal": 100, "protein_g": 10}, {"kcal": 50.5, "protein_g": None, "fat_g": 3}]) == {
        "kcal": 150.5, "protein_g": 10, "carbs_g": 0, "fat_g": 3}
    assert [calc.default_meal(t) for t in ("07:00", "12:30", "16:00", "19:00", "23:00")] == [
        "breakfast", "lunch", "snack", "dinner", "snack"]
