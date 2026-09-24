"""Foiling / planing state from speed hysteresis.

On when smoothed speed rises to `on_kn`, off when it falls below `off_kn`;
the gap between the two keeps GPS noise around a single threshold from
toggling the state. Recording gaps always end a flight.
"""

from __future__ import annotations

import numpy as np

from app.analysis.clean import Clean
from app.analysis.geo import KN, runs


def hysteresis(speed: np.ndarray, on: float, off: float, segments) -> np.ndarray:
    state = np.zeros(len(speed), dtype=bool)
    for s, e in segments:
        cur = False
        for i in range(s, e):
            v = speed[i]
            if cur:
                cur = v >= off
            else:
                cur = v >= on
            state[i] = cur
    return state


def flights(c: Clean, state: np.ndarray) -> list[dict]:
    out = []
    for s, e in runs(state):
        dur = float(c.t[e - 1] - c.t[s]) + 1.0
        out.append({
            "i0": s, "i1": e - 1, "t0": float(c.t[s]), "t1": float(c.t[e - 1]),
            "duration_s": round(dur, 1),
            "distance_m": round(float(c.step[s + 1:e].sum()), 1),
            "max_kn": round(float(c.speed[s:e].max() * KN), 2),
        })
    return out


def summary(c: Clean, state: np.ndarray, cfg: dict) -> dict:
    fl = flights(c, state)
    moving = c.speed > cfg["stop_kn"] / KN
    total = float(c.dt.sum())
    moving_s = float(c.dt[moving].sum())
    on_s = float(c.dt[state].sum())
    # a touchdown is a flight that ends while the recording continues (not a gap / file end)
    seg_ends = {e - 1 for _, e in c.segments}
    touchdowns = sum(1 for f in fl if f["i1"] not in seg_ends)
    return {
        "state_name": cfg["state_name"],
        "on_kn": cfg["on_kn"], "off_kn": cfg["off_kn"],
        "time_s": round(on_s, 1),
        "pct_of_total": round(100 * on_s / total, 1) if total else 0.0,
        "pct_of_moving": round(100 * on_s / moving_s, 1) if moving_s else 0.0,
        "moving_s": round(moving_s, 1),
        "distance_m": round(float(c.step[state].sum()), 1),
        "flights": len(fl),
        "touchdowns": touchdowns,
        "longest_s": max((f["duration_s"] for f in fl), default=0.0),
        "longest_m": max((f["distance_m"] for f in fl), default=0.0),
        "avg_flight_s": round(float(np.mean([f["duration_s"] for f in fl])), 1) if fl else 0.0,
        "flight_list": fl,
    }
