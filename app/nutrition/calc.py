"""Weight-trend and nutrition maths -- pure functions, no database.

Trend weight follows the Hacker's Diet: an exponential moving average of the
*morning* weights with smoothing 0.1, one step per calendar day. Days without
a morning weight are filled by linear interpolation between the neighbouring
weighings, so a gap neither freezes the trend nor makes it jump."""

from __future__ import annotations

import math
from datetime import date, timedelta

SMOOTHING = 0.1
KCAL_PER_KG = 7700
RATE_WINDOW_DAYS = 14
RATE_MIN_DAYS = 7          # trend days needed before a weekly rate means anything
STATUS_TOLERANCE = 0.25    # kg/week either side of the target rate counts as on track


def trend_series(morning: dict[date, float], smoothing: float = SMOOTHING) -> list[dict]:
    """One row per calendar day from the first to the last morning weight:
    {date, weight (None if not weighed), trend, interpolated}."""
    if not morning:
        return []
    days = sorted(morning)
    out: list[dict] = []
    trend = morning[days[0]]
    for prev, nxt in zip(days, days[1:] + [None]):
        w0 = morning[prev]
        trend = trend + smoothing * (w0 - trend) if out else w0
        out.append({"date": prev, "weight": w0, "trend": trend, "interpolated": False})
        if nxt is None:
            break
        gap = (nxt - prev).days
        for i in range(1, gap):
            w = w0 + (morning[nxt] - w0) * i / gap
            trend = trend + smoothing * (w - trend)
            out.append({"date": prev + timedelta(days=i), "weight": None, "trend": trend, "interpolated": True})
    return out


def weekly_rate(series: list[dict], window: int = RATE_WINDOW_DAYS, min_days: int = RATE_MIN_DAYS) -> float | None:
    """Change in trend weight per week over the last `window` days (fewer if
    there's less history, but at least `min_days` days of trend)."""
    if len(series) < min_days:
        return None
    n = min(window, len(series) - 1)
    return (series[-1]["trend"] - series[-1 - n]["trend"]) / n * 7


def trend_change(series: list[dict], start: date, end: date) -> float | None:
    """Trend at `end` minus trend at `start` (each the nearest day on or before)."""
    a, b = trend_on(series, start), trend_on(series, end)
    return None if a is None or b is None else b - a


def trend_on(series: list[dict], day: date) -> float | None:
    best = None
    for row in series:
        if row["date"] > day:
            break
        best = row["trend"]
    return best


def status(rate: float | None, target_rate: float, trend: float | None, goal: float | None,
           tolerance: float = STATUS_TOLERANCE) -> str:
    """'reached' | 'on_track' | 'too_slow' | 'too_fast' | 'no_data'.
    Works for gaining and losing plans (target_rate < 0)."""
    if trend is not None and goal is not None:
        if (target_rate >= 0 and trend >= goal) or (target_rate < 0 and trend <= goal):
            return "reached"
    if rate is None:
        return "no_data"
    diff = rate - target_rate
    if abs(diff) <= tolerance:
        return "on_track"
    toward_goal = diff if target_rate >= 0 else -diff
    return "too_fast" if toward_goal > 0 else "too_slow"


STATUS_LABEL = {
    "reached": "Goal reached",
    "on_track": "On track",
    "too_slow": "Too slow",
    "too_fast": "Too fast",
    "no_data": "Not enough data",
}


def projected_date(trend: float | None, goal: float | None, rate: float | None, today: date) -> date | None:
    """When the trend reaches the goal at the current rate; None when it isn't
    moving toward it (or barely: under 0.05 kg/week)."""
    if trend is None or goal is None or rate is None or abs(rate) < 0.05:
        return None
    weeks = (goal - trend) / rate
    if weeks < 0:
        return None
    return today + timedelta(days=round(weeks * 7))


def target_line(start_weight: float, start: date, goal: float, end: date, day: date) -> float:
    """The plan's straight line from start weight to goal weight."""
    span = (end - start).days or 1
    return start_weight + (goal - start_weight) * (day - start).days / span


def evening_difference(morning: dict[date, float], evening: dict[date, float]) -> float | None:
    """Mean evening − morning weight over days that have both (info only)."""
    diffs = [evening[d] - morning[d] for d in morning if d in evening]
    return sum(diffs) / len(diffs) if diffs else None


def expenditure(avg_intake_kcal: float, trend_change_kg: float, days: int) -> float:
    """Estimated daily energy expenditure: what was eaten minus what was stored."""
    return avg_intake_kcal - trend_change_kg * KCAL_PER_KG / days


def kcal_adjustment(target_rate: float, actual_rate: float) -> float:
    """Daily kcal change that would move the weekly rate to the target."""
    return (target_rate - actual_rate) * KCAL_PER_KG / 7


def rounded_range(kcal: float) -> tuple[int, int]:
    """A friendly range around the size of an adjustment: 330 -> (300, 400),
    300 -> (250, 350)."""
    mag = abs(kcal)
    lo, hi = math.floor(mag / 100) * 100, math.ceil(mag / 100) * 100
    if lo == hi:
        lo, hi = lo - 50, hi + 50
    return max(lo, 0), hi


def advice_text(actual_rate: float, target_rate: float) -> str:
    """Weight-trend-only advice, e.g. 'gaining 0.3 kg/week → eat roughly
    300–400 kcal more per day'."""
    verb = "gaining" if actual_rate >= 0 else "losing"
    now = f"{verb} {abs(actual_rate):.1f} kg/week"
    if abs(actual_rate - target_rate) <= STATUS_TOLERANCE / 2:
        return f"{now} – on target ({target_rate:+.1f}), keep eating like this"
    adj = kcal_adjustment(target_rate, actual_rate)
    lo, hi = rounded_range(adj)
    return f"{now} → eat roughly {lo}–{hi} kcal {'more' if adj > 0 else 'less'} per day"


# --------------------------------------------------------------------- food


MACROS = ("kcal", "protein_g", "carbs_g", "fat_g")
PER100 = {"kcal": "kcal_100g", "protein_g": "protein_100g", "carbs_g": "carbs_100g", "fat_g": "fat_100g"}


def scale_food(food: dict, grams: float) -> dict:
    """Macros of `grams` of a food given per 100 g (missing carbs/fat stay None)."""
    out = {}
    for k, col in PER100.items():
        v = food.get(col)
        out[k] = None if v is None else round(v * grams / 100, 1)
    return out


def totals(items: list[dict]) -> dict:
    """Sum of kcal/protein/carbs/fat over entries (None counts as 0)."""
    return {k: round(sum((x.get(k) or 0) for x in items), 1) for k in MACROS}


MAIN_MEALS = ("breakfast", "lunch", "dinner")
SNACK_GAP_MIN = 60          # snacks logged at least this far apart count separately


def _minutes(hhmm: str | None) -> int | None:
    try:
        h, m = (int(x) for x in hhmm.split(":")[:2])
        return h * 60 + m
    except (AttributeError, ValueError):
        return None


def meal_count(items: list[dict]) -> dict:
    """How many times the athlete ate: each of breakfast / lunch / dinner
    counts once, snacks count once per group logged within an hour of each
    other (snacks without a time count as one). The same rule runs on the
    phone page (food.js mealCount)."""
    meals = len({x.get("meal") for x in items if x.get("meal") in MAIN_MEALS})
    times = sorted(t for t in (_minutes(x.get("time")) for x in items if x.get("meal") == "snack") if t is not None)
    snacks = sum(1 for i, t in enumerate(times) if i == 0 or t - times[i - 1] >= SNACK_GAP_MIN)
    if not times and any(x.get("meal") == "snack" for x in items):
        snacks = 1
    return {"meals": meals, "snacks": snacks, "eating_times": meals + snacks}


KCAL_PER_G = {"protein_g": 4, "carbs_g": 4, "fat_g": 9}


def macro_split(t: dict) -> dict:
    """Share of energy from protein / carbs / fat in % (of the energy the three
    macros add up to, so it sums to 100 even when kcal was rounded)."""
    kcal = {k: (t.get(k) or 0) * f for k, f in KCAL_PER_G.items()}
    total = sum(kcal.values())
    if total <= 0:
        return {"protein_pct": None, "carbs_pct": None, "fat_pct": None}
    return {k.replace("_g", "_pct"): round(v / total * 100) for k, v in kcal.items()}


def default_meal(hhmm: str) -> str:
    """Meal slot from the clock."""
    h, m = (int(x) for x in hhmm.split(":")[:2])
    t = h * 60 + m
    if t < 10 * 60 + 30:
        return "breakfast"
    if t < 15 * 60:
        return "lunch"
    if t < 17 * 60 + 30:
        return "snack"
    if t < 21 * 60 + 30:
        return "dinner"
    return "snack"
