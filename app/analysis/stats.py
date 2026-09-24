"""Aggregate statistics: point-of-sail x tack, polars, targets, gain/loss, HR,
speed map.
"""

from __future__ import annotations

import numpy as np

from app.analysis.clean import Clean
from app.analysis.geo import KN
from app.analysis.maneuvers import LEG_TYPES, classify_twa

TWA_BIN = 5.0
N_TWA = int(180 / TWA_BIN)
SPD_BIN_KN = 0.25
N_SPD = 240  # up to 60 kn
MIN_BIN_S = 15.0
HR_ZONES = [(1, 0.50, 0.60), (2, 0.60, 0.70), (3, 0.70, 0.80), (4, 0.80, 0.90), (5, 0.90, 9.99)]
GRID_M = 40.0


def sailing_mask(c: Clean, mans: list[dict], cfg: dict) -> np.ndarray:
    """Steady sailing: moving, not turning, not inside a maneuver, good fix."""
    m = (c.speed > cfg["stop_kn"] / KN) & (np.abs(c.turn) < cfg["steady_turn_dps"]) & c.good & (c.dt > 0)
    for mv in mans:
        m[mv["i0"]:mv["i_rec"] + 1] = False
    return m


def _block(c, sel, twa, vmg, state) -> dict:
    dt = c.dt[sel]
    tot = float(dt.sum())
    if tot <= 0:
        return {"time_s": 0.0, "distance_m": 0.0, "avg_kn": None, "max_kn": None,
                "avg_vmg_kn": None, "avg_twa": None, "state_pct": None}
    return {
        "time_s": round(tot, 1),
        "distance_m": round(float(c.step[sel].sum()), 1),
        "avg_kn": round(float(np.average(c.speed[sel], weights=dt)) * KN, 2),
        "max_kn": round(float(c.speed[sel].max()) * KN, 2),
        "avg_vmg_kn": round(float(np.average(vmg[sel], weights=dt)) * KN, 2),
        "avg_twa": round(float(np.average(np.abs(twa[sel]), weights=dt)), 1),
        "state_pct": round(100 * float(dt[state[sel]].sum()) / tot, 1),
    }


def point_of_sail(c: Clean, twa, vmg, state, sailing, cfg) -> dict:
    cls = classify_twa(np.abs(twa), cfg)
    port = twa > 0
    out: dict = {}
    for k, name in enumerate(LEG_TYPES):
        out[name] = {
            "port": _block(c, sailing & (cls == k) & port, twa, vmg, state),
            "starboard": _block(c, sailing & (cls == k) & ~port, twa, vmg, state),
            "all": _block(c, sailing & (cls == k), twa, vmg, state),
        }
    out["all"] = {
        "port": _block(c, sailing & port, twa, vmg, state),
        "starboard": _block(c, sailing & ~port, twa, vmg, state),
        "all": _block(c, sailing, twa, vmg, state),
    }
    return out


# ---------------------------------------------------------------- polars
def polar_hist(c: Clean, twa, sailing) -> list[list[float]]:
    """Sparse [twa_bin, speed_bin, seconds] -- mergeable across sessions."""
    tb = np.clip((np.abs(twa[sailing]) // TWA_BIN).astype(int), 0, N_TWA - 1)
    sb = np.clip((c.speed[sailing] * KN // SPD_BIN_KN).astype(int), 0, N_SPD - 1)
    flat = np.bincount(tb * N_SPD + sb, weights=c.dt[sailing], minlength=N_TWA * N_SPD)
    nz = np.flatnonzero(flat > 0)
    return [[int(k // N_SPD), int(k % N_SPD), round(float(flat[k]), 1)] for k in nz]


def polar_from_hist(entries: list[list[float]], min_s: float = MIN_BIN_S) -> dict:
    H = np.zeros((N_TWA, N_SPD))
    for tb, sb, s in entries:
        H[int(tb), int(sb)] += s
    centers = (np.arange(N_SPD) + 0.5) * SPD_BIN_KN
    bins = []
    for k in range(N_TWA):
        row = H[k]
        tot = row.sum()
        if tot < min_s:
            bins.append({"twa": k * TWA_BIN + TWA_BIN / 2, "seconds": round(float(tot), 1),
                         "mean": None, "p50": None, "p90": None, "max": None})
            continue
        cdf = np.cumsum(row) / tot
        q = lambda p: float(centers[min(int(np.searchsorted(cdf, p)), N_SPD - 1)])  # noqa: E731
        nzr = np.flatnonzero(row)
        bins.append({"twa": k * TWA_BIN + TWA_BIN / 2, "seconds": round(float(tot), 1),
                     "mean": round(float(np.sum(row * centers) / tot), 2),
                     "p50": round(q(0.5), 2), "p90": round(q(0.9), 2),
                     "max": round(float(centers[nzr[-1]]), 2)})
    return {"bins": bins, **best_vmg(bins)}


def best_vmg(bins: list[dict], key: str = "p90") -> dict:
    up = dn = None
    for b in bins:
        v = b.get(key)
        if v is None:
            continue
        vmg = v * np.cos(np.radians(b["twa"]))
        if b["twa"] < 90 and (up is None or vmg > up["vmg_kn"]):
            up = {"twa": b["twa"], "kn": v, "vmg_kn": round(float(vmg), 2)}
        if b["twa"] > 90 and (dn is None or -vmg > dn["vmg_kn"]):
            dn = {"twa": b["twa"], "kn": v, "vmg_kn": round(float(-vmg), 2)}
    return {"best_upwind": up, "best_downwind": dn}


def target_speed_fn(polar: dict | None):
    """Per-|TWA| target speed (kn) interpolated from a polar's p90 curve."""
    if not polar:
        return None
    pts = [(b["twa"], b["p90"]) for b in polar["bins"] if b.get("p90") is not None]
    if len(pts) < 3:
        return None
    x, y = np.array(pts).T
    return lambda atwa: np.interp(atwa, x, y, left=np.nan, right=np.nan)


def resolve_targets(cfg: dict, session_polar: dict, personal_polar: dict | None) -> dict:
    tg = cfg["targets"]
    polar = personal_polar if (personal_polar and personal_polar.get("best_upwind")) else session_polar
    src = "personal polar" if polar is personal_polar else "session polar"
    up, dn = polar.get("best_upwind") or {}, polar.get("best_downwind") or {}
    out = {
        "mode": tg.get("mode", "polar"),
        "upwind_kn": up.get("kn"), "upwind_twa": up.get("twa"),
        "downwind_kn": dn.get("kn"), "downwind_twa": dn.get("twa"),
        "source": src,
    }
    if tg.get("mode") == "manual":
        for k in ("upwind_kn", "upwind_twa", "downwind_kn", "downwind_twa"):
            if tg.get(k) is not None:
                out[k] = float(tg[k])
        out["source"] = "manual"
    for side in ("upwind", "downwind"):
        kn, a = out[f"{side}_kn"], out[f"{side}_twa"]
        out[f"{side}_vmg_kn"] = round(abs(kn * np.cos(np.radians(a))), 2) if kn and a else None
    out["_speed_fn"] = target_speed_fn(polar) if out["source"] != "manual" else None
    return out


def apply_targets(c: Clean, twa, vmg, legs: list[dict], targets: dict) -> np.ndarray:
    """Adds % of target, gain vs target and angle vs best-pointing to each leg.
    Returns per-point target speed (kn, NaN where unknown)."""
    fn = targets.get("_speed_fn")
    atwa = np.abs(twa)
    tspd = fn(atwa) if fn else np.full(len(twa), np.nan)
    if not fn:  # manual targets: flat upwind / downwind speeds
        cls = classify_twa(atwa, {"upwind_max_twa": 90, "downwind_min_twa": 90.001})
        if targets.get("upwind_kn"):
            tspd = np.where(cls == 0, targets["upwind_kn"], tspd)
        if targets.get("downwind_kn"):
            tspd = np.where(cls == 2, targets["downwind_kn"], tspd)
    for lg in legs:
        s, e = lg["i0"], lg["i1"] + 1
        dt = c.dt[s:e].copy()
        dt[0] = dt[0] or 1.0
        ts = tspd[s:e]
        ok = np.isfinite(ts)
        lg["target_kn"] = round(float(np.average(ts[ok], weights=dt[ok])), 2) if ok.any() else None
        lg["pct_target_speed"] = (round(100 * float(np.average(c.speed[s:e][ok] * KN, weights=dt[ok]))
                                        / lg["target_kn"], 1) if ok.any() and lg["target_kn"] else None)
        lg["pct_target_vmg"] = lg["gain_vs_target_m"] = lg["angle_vs_ref"] = None
        side = lg["type"] if lg["type"] != "reach" else None
        if side and targets.get(f"{side}_vmg_kn"):
            tv = targets[f"{side}_vmg_kn"] / KN
            sign = 1.0 if side == "upwind" else -1.0
            leg_vmg = sign * float(np.sum(vmg[s:e] * dt)) / float(dt.sum())
            lg["pct_target_vmg"] = round(100 * leg_vmg / tv, 1)
            lg["gain_vs_target_m"] = round((leg_vmg - tv) * float(dt.sum()), 1)
            lg["angle_vs_ref"] = round(lg["avg_twa"] - targets[f"{side}_twa"], 1)
    return tspd


# ---------------------------------------------------------------- HR
def heart_rate(c: Clean, mans: list[dict], sailing: np.ndarray, max_hr: float) -> dict | None:
    hr = c.hr
    ok = np.isfinite(hr) & (hr > 30) & (c.dt > 0)
    if ok.sum() < 30:
        return None
    dt = c.dt
    zones = []
    for z, lo, hi in HR_ZONES:
        sel = ok & (hr >= lo * max_hr) & (hr < hi * max_hr)
        zones.append({"zone": z, "lo_bpm": round(lo * max_hr), "hi_bpm": round(min(hi, 1.0) * max_hr),
                      "time_s": round(float(dt[sel].sum()), 1)})
    below = ok & (hr < 0.5 * max_hr)
    zones.insert(0, {"zone": 0, "lo_bpm": 0, "hi_bpm": round(0.5 * max_hr), "time_s": round(float(dt[below].sum()), 1)})
    by_speed = []
    kn = c.speed * KN
    for lo in range(0, int(np.nanmax(kn)) + 2, 2):
        sel = ok & (kn >= lo) & (kn < lo + 2)
        if dt[sel].sum() >= 10:
            by_speed.append({"kn": lo + 1, "hr": round(float(np.average(hr[sel], weights=dt[sel])), 1),
                             "time_s": round(float(dt[sel].sum()), 1)})
    in_man = np.zeros(len(hr), dtype=bool)
    for m in mans:
        in_man[m["i0"]:m["i_rec"] + 1] = True

    def avg(sel):
        sel = sel & ok
        return round(float(np.average(hr[sel], weights=dt[sel])), 1) if dt[sel].sum() > 0 else None

    def mans_of(kind):
        vals = [m["hr_max"] for m in mans if m["type"] == kind and m["hr_max"] is not None]
        return round(float(np.mean(vals)), 1) if vals else None

    return {
        "max_hr_setting": max_hr,
        "avg": avg(np.ones(len(hr), dtype=bool)), "max": round(float(np.nanmax(hr[ok])), 0),
        "zones": zones, "by_speed": by_speed,
        "sailing_avg": avg(sailing), "maneuver_avg": avg(in_man),
        "tack_peak_avg": mans_of("tack"), "gybe_peak_avg": mans_of("gybe"),
    }


# ---------------------------------------------------------------- speed map
def speed_grid(c: Clean, state: np.ndarray, cfg: dict) -> dict:
    """Cells on a spot-stable grid (keyed by absolute lat/lon) so sessions at
    the same spot can be merged: [lat_idx, lon_idx, seconds, sum_kn, max_kn, on_s]."""
    lat_ref = round(c.lat0)
    dlat = GRID_M / 111_195.0
    dlon = GRID_M / (111_195.0 * np.cos(np.radians(lat_ref)))
    m = (c.speed > cfg["stop_kn"] / KN) & (c.dt > 0) & c.good
    iy = np.floor(c.lat[m] / dlat).astype(np.int64)
    ix = np.floor(c.lon[m] / dlon).astype(np.int64)
    kn = c.speed[m] * KN
    dt = c.dt[m]
    on = state[m]
    cells: dict[tuple[int, int], list[float]] = {}
    for a, b, v, d, o in zip(iy.tolist(), ix.tolist(), kn.tolist(), dt.tolist(), on.tolist()):
        cell = cells.setdefault((a, b), [0.0, 0.0, 0.0, 0.0])
        cell[0] += d
        cell[1] += v * d
        cell[2] = max(cell[2], v)
        cell[3] += d if o else 0.0
    return {"dlat": dlat, "dlon": dlon, "lat_ref": lat_ref,
            "cells": [[a, b, round(v[0], 1), round(v[1], 1), round(v[2], 2), round(v[3], 1)]
                      for (a, b), v in cells.items()]}
