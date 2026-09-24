"""Race analysis: pre-start vs race split, and legs between marks.

Marks are either placed by the user (lat/lon, rounded in course order,
repeating for multi-lap courses; a mark counts as rounded at the closest
approach within mark_radius_m) or detected automatically: a windward mark
is where sustained upwind sailing turns into sustained downwind sailing,
a leeward mark the reverse.
"""

from __future__ import annotations

import numpy as np

from app.analysis.clean import Clean
from app.analysis.geo import KN, project, runs

MIN_PHASE_S = 60.0


def _phase_stats(c: Clean, sel: np.ndarray, vmg, state, mans, t0, t1) -> dict:
    dt = c.dt[sel]
    tot = float(dt.sum())
    mm = [m for m in mans if t0 <= m["tc"] < t1]
    up = sel & (vmg > 0)
    dn = sel & (vmg < 0)
    return {
        "t0": t0, "t1": t1, "duration_s": round(t1 - t0, 1),
        "distance_m": round(float(c.step[sel].sum()), 1),
        "avg_kn": round(float(c.step[sel].sum()) / max(t1 - t0, 1) * KN, 2),
        "max_kn": round(float(c.speed[sel].max()) * KN, 2) if sel.any() else None,
        "state_pct": round(100 * float(dt[state[sel]].sum()) / tot, 1) if tot else None,
        "tacks": sum(1 for m in mm if m["type"] == "tack"),
        "gybes": sum(1 for m in mm if m["type"] == "gybe"),
        "avg_vmg_up_kn": round(float(np.average(vmg[up], weights=c.dt[up])) * KN, 2) if c.dt[up].sum() > 0 else None,
        "avg_vmg_down_kn": round(float(-np.average(vmg[dn], weights=c.dt[dn])) * KN, 2) if c.dt[dn].sum() > 0 else None,
        "gain_m": round(float(np.sum(vmg[sel] * c.dt[sel])), 1),
    }


def user_roundings(c: Clean, marks: list[dict], t_from: float, radius: float) -> list[dict]:
    if not marks:
        return []
    mx = []
    for mk in marks:
        x, y = project(mk["lat"], mk["lon"], c.lat0, c.lon0)
        mx.append((float(x), float(y)))
    out = []
    k = 0
    i_from = int(np.searchsorted(c.t, t_from))
    misses = 0
    while i_from < len(c.t) and misses < len(marks):
        x, y = mx[k % len(marks)]
        d = np.hypot(c.x[i_from:] - x, c.y[i_from:] - y)
        near = runs(d <= radius)
        if not near:
            misses += 1
            k += 1
            continue
        s, e = near[0]
        j = s + int(np.argmin(d[s:e]))
        idx = i_from + j
        mk = marks[k % len(marks)]
        out.append({"i": idx, "t": float(c.t[idx]), "name": mk.get("name") or f"M{k % len(marks) + 1}",
                    "lat": float(c.lat[idx]), "lon": float(c.lon[idx]), "auto": False,
                    "distance_m": round(float(d[j]), 1)})
        i_from = i_from + e
        k += 1
        misses = 0
    return out


def auto_roundings(c: Clean, legs: list[dict], t_from: float, wind_deg: float) -> list[dict]:
    phases: list[list] = []
    for lg in legs:
        if lg["t1"] < t_from or lg["type"] == "reach":
            continue
        dur = lg["duration_s"]
        if phases and phases[-1][0] == lg["type"]:
            phases[-1][2] = lg["i1"]
            phases[-1][3] += dur
        else:
            phases.append([lg["type"], lg["i0"], lg["i1"], dur])
    phases = [p for p in phases if p[3] >= MIN_PHASE_S]
    # re-merge after dropping short phases
    merged: list[list] = []
    for p in phases:
        if merged and merged[-1][0] == p[0]:
            merged[-1][2] = p[2]
            merged[-1][3] += p[3]
        else:
            merged.append(list(p))
    wr = np.radians(wind_deg)
    along = c.x * np.sin(wr) + c.y * np.cos(wr)  # metres towards the wind
    out = []
    for a, b in zip(merged, merged[1:]):
        # rounding = the extreme point along the wind axis between the phases
        lo, hi = a[2], max(b[1] + 1, a[2] + 1)
        windward = a[0] == "upwind"
        seg = along[lo:hi]
        idx = lo + int(np.argmax(seg) if windward else np.argmin(seg))
        out.append({"i": int(idx), "t": float(c.t[idx]), "name": "Windward" if windward else "Leeward",
                    "auto": True, "lat": float(c.lat[idx]), "lon": float(c.lon[idx])})
    return out


def analyse(c: Clean, vmg, state, mans: list[dict], legs: list[dict], cfg: dict, wind_deg: float) -> dict:
    start = cfg.get("race_start")
    t_first, t_last = float(c.t[0]), float(c.t[-1])
    res: dict = {"start": start, "marks_mode": "user" if cfg.get("marks") else "auto"}
    t_from = t_first
    if start is not None and t_first < start < t_last:
        pre = c.t < start
        res["pre_start"] = _phase_stats(c, pre, vmg, state, mans, t_first, float(start))
        res["race"] = _phase_stats(c, ~pre, vmg, state, mans, float(start), t_last)
        t_from = float(start)
    elif start is not None:
        res["warning"] = "Race start is outside the recording"
    if cfg.get("marks"):
        rounds = user_roundings(c, cfg["marks"], t_from, cfg["mark_radius_m"])
    else:
        rounds = auto_roundings(c, legs, t_from, wind_deg)
    res["roundings"] = rounds
    bounds = [(t_from, "Start" if start is not None else "Begin")] + [(r["t"], r["name"]) for r in rounds] + [(t_last, "Finish")]
    rlegs = []
    for (ta, na), (tb, nb) in zip(bounds, bounds[1:]):
        if tb - ta < 5:
            continue
        sel = (c.t >= ta) & (c.t < tb)
        st = _phase_stats(c, sel, vmg, state, mans, ta, tb)
        st.update({"from": na, "to": nb, "i0": int(np.searchsorted(c.t, ta)),
                   "i1": int(min(np.searchsorted(c.t, tb), len(c.t) - 1))})
        st["type"] = "upwind" if st["gain_m"] > 0 else "downwind"
        rlegs.append(st)
    for k, lg in enumerate(rlegs):
        lg["id"] = k + 1
    res["legs"] = rlegs
    return res
