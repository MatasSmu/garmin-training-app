"""Run the whole engine on one track and produce a JSON-safe result."""

from __future__ import annotations

import math

import numpy as np

from app.analysis import clean as clean_mod
from app.analysis import maneuvers as man_mod
from app.analysis import race as race_mod
from app.analysis import splits as splits_mod
from app.analysis import state as state_mod
from app.analysis import stats as stats_mod
from app.analysis import wind as wind_mod
from app.analysis.geo import KN
from app.analysis.loader import Track
from app.analysis.settings import ENGINE_VERSION, guess_sport, resolve


def _r(a, nd: int):
    """Round an array to a list with None for non-finite values."""
    arr = np.asarray(a, dtype=float)
    out = np.round(arr, nd).tolist()
    return [None if (v is None or not math.isfinite(v)) else v for v in out]


def _clean_json(obj):
    """Replace NaN/inf with None and numpy scalars with Python ones, recursively."""
    if isinstance(obj, dict):
        return {k: _clean_json(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, (list, tuple)):
        return [_clean_json(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


# The wind direction comes from the symmetry of port and starboard sailing, so
# it can't be cross-checked when only one tack was sailed upwind, and an
# upwind angle inside the no-go zone means it is simply wrong.
SUSPECT_ONE_TACK_S = (60.0, 300.0)   # one tack under 60 s while the other has over 5 min
SUSPECT_MIN_UPWIND_TWA = 30.0


def wind_suspect(pos: dict) -> str | None:
    """Why the inferred wind direction is doubtful, or None."""
    up = pos.get("upwind") or {}
    p, s = up.get("port") or {}, up.get("starboard") or {}
    tp, ts = p.get("time_s") or 0.0, s.get("time_s") or 0.0
    lo, hi = SUSPECT_ONE_TACK_S
    if min(tp, ts) < lo and max(tp, ts) > hi:
        return "only one tack sailed upwind, so the wind direction can't be cross-checked"
    for name, side, t in (("port", p, tp), ("starboard", s, ts)):
        if t >= lo and side.get("avg_twa") is not None and abs(side["avg_twa"]) < SUSPECT_MIN_UPWIND_TWA:
            return f"{name} upwind angle {abs(side['avg_twa']):.0f} deg is inside the no-go zone"
    return None


def analyze(track: Track, overrides: dict | None = None, personal_polar: dict | None = None,
            sport: str | None = None) -> dict:
    sport = sport or guess_sport(track.meta.get("sport_name"), track.meta.get("filename"),
                                 track.meta.get("fit_sport"))
    ov = dict(overrides or {})
    if ov.get("wind_prior_deg") is None and track.meta.get("device_wind_deg") is not None:
        ov["wind_prior_deg"] = track.meta["device_wind_deg"]
        ov["wind_prior_source"] = "device"
    cfg = resolve(ov, sport)
    warnings: list[str] = []

    c = clean_mod.clean(track, cfg)
    if len(c.t) < 30:
        raise ValueError("Too few usable fixes after cleaning")

    w = wind_mod.estimate(c, cfg)
    W = w.pop("per_point")
    if w["source"] == "auto" and w["confidence"] in ("low", "none"):
        warnings.append("Wind direction is uncertain -- set it manually if it looks wrong.")

    twa = np.asarray(((c.heading - W + 180.0) % 360.0) - 180.0)
    vmg = c.speed * np.cos(np.radians(twa))           # + upwind, - downwind (m/s)
    on = state_mod.hysteresis(c.speed_s, cfg["on_kn"] / KN, cfg["off_kn"] / KN, c.segments)
    st = state_mod.summary(c, on, cfg)

    mans = man_mod.detect(c, twa, vmg, on, cfg)
    legs = man_mod.legs(c, twa, vmg, on, mans, cfg)
    sailing = stats_mod.sailing_mask(c, mans, cfg)
    pos = stats_mod.point_of_sail(c, twa, vmg, on, sailing, cfg)
    phist = stats_mod.polar_hist(c, twa, sailing)
    polar = stats_mod.polar_from_hist(phist)
    targets = stats_mod.resolve_targets(cfg, polar, personal_polar)
    tspd = stats_mod.apply_targets(c, twa, vmg, legs, targets)
    spl = splits_mod.best_splits(c)

    max_hr = cfg["max_hr"] or track.meta.get("max_hr") or 190
    hr = stats_mod.heart_rate(c, mans, sailing, float(max_hr))
    race = race_mod.analyse(c, vmg, on, mans, legs, cfg, w["deg"])
    grid = stats_mod.speed_grid(c, on, cfg)

    gain_cum = np.cumsum(vmg * c.dt)
    moving = c.speed > cfg["stop_kn"] / KN
    tacks = [m for m in mans if m["type"] == "tack"]
    gybes = [m for m in mans if m["type"] == "gybe"]

    def rate(ms):
        return round(100.0 * sum(m["success"] for m in ms) / len(ms), 1) if ms else None

    def mean_of(ms, k):
        vals = [m[k] for m in ms if m.get(k) is not None]
        return round(float(np.mean(vals)), 2) if vals else None

    split_by = {s["label"]: s["kn"] for s in spl}
    total_s = float(c.t[-1] - c.t[0])
    summary = {
        "sport": cfg["sport"], "state_name": cfg["state_name"],
        "start_utc": float(c.t[0]), "end_utc": float(c.t[-1]),
        "duration_s": round(total_s, 1),
        "recorded_s": round(float(c.dt.sum()), 1),
        "moving_s": round(float(c.dt[moving].sum()), 1),
        "distance_m": round(float(c.step.sum()), 1),
        "avg_moving_kn": round(float(c.step[moving].sum()) / max(float(c.dt[moving].sum()), 1) * KN, 2),
        "max_kn": split_by.get("2 s"),
        "best_2s_kn": split_by.get("2 s"), "best_10s_kn": split_by.get("10 s"),
        "best_100m_kn": split_by.get("100 m"), "best_500m_kn": split_by.get("500 m"),
        "best_1nm_kn": split_by.get("1 nm"), "best_1h_kn": split_by.get("1 h"),
        "wind_deg": w["deg"], "wind_confidence": w["confidence"], "wind_source": w["source"],
        "wind_steadiness": w["steadiness"]["label"], "wind_shift_deg": w["steadiness"]["range_deg"],
        "wind_shift_rate_deg": w["steadiness"]["rate_deg_4min"], "wind_suspect": wind_suspect(pos),
        "tacks": len(tacks), "gybes": len(gybes),
        "tack_success_pct": rate(tacks), "gybe_success_pct": rate(gybes),
        "tack_loss_m": mean_of(tacks, "distance_lost_m"), "gybe_loss_m": mean_of(gybes, "distance_lost_m"),
        "tack_min_kn": mean_of(tacks, "min_kn"), "gybe_min_kn": mean_of(gybes, "min_kn"),
        "state_pct": st["pct_of_moving"], "state_time_s": st["time_s"],
        "flights": st["flights"], "touchdowns": st["touchdowns"],
        "upwind_vmg_kn": pos["upwind"]["all"]["avg_vmg_kn"],
        "downwind_vmg_kn": (-pos["downwind"]["all"]["avg_vmg_kn"]
                            if pos["downwind"]["all"]["avg_vmg_kn"] is not None else None),
        "upwind_twa": pos["upwind"]["all"]["avg_twa"], "downwind_twa": pos["downwind"]["all"]["avg_twa"],
        "upwind_kn": pos["upwind"]["all"]["avg_kn"], "downwind_kn": pos["downwind"]["all"]["avg_kn"],
        "legs": len(legs),
        "hr_avg": hr["avg"] if hr else None, "hr_max": hr["max"] if hr else None,
        "best_upwind": polar["best_upwind"], "best_downwind": polar["best_downwind"],
        "lat": float(np.median(c.lat)), "lon": float(np.median(c.lon)),
    }

    series = {
        "t": _r(c.t - c.t[0], 1),
        "lat": _r(c.lat, 6), "lon": _r(c.lon, 6),
        "kn": _r(c.speed * KN, 2), "hdg": _r(c.heading, 0),
        "twa": _r(twa, 1), "vmg": _r(vmg * KN, 2),
        "on": on.astype(int).tolist(),
        "hr": _r(np.where(np.isfinite(c.hr) & (c.hr > 0), c.hr, np.nan), 0),
        "wind": _r(W, 0), "gain": _r(gain_cum, 1),
        "target_kn": _r(tspd, 2),
        "gap": [0] + [int(c.t[i] - c.t[i - 1] > cfg["gap_s"]) for i in range(1, len(c.t))],
    }

    for m in mans:
        m["t_rel"] = round(m["tc"] - float(c.t[0]), 1)
    result = {
        "engine_version": ENGINE_VERSION,
        "settings": cfg,
        "meta": {k: v for k, v in track.meta.items()},
        "cleaning": c.report,
        "summary": summary,
        "wind": w,
        "state": {k: v for k, v in st.items() if k != "flight_list"},
        "flights": st["flight_list"],
        "maneuvers": mans,
        "legs": legs,
        "splits": spl,
        "point_of_sail": pos,
        "polar": polar,
        "polar_hist": phist,
        "targets": targets,
        "hr": hr,
        "race": race,
        "speed_grid": grid,
        "series": series,
        "warnings": warnings,
    }
    return _clean_json(result)
