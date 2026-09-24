"""Tacks, gybes and legs.

Maneuvers
  Label every sample with its tack side (sign of TWA) when sailing steadily
  (moving, turn rate below steady_turn_dps). Runs of one side held for at
  least min_steady_s are "steady sides". Two consecutive steady sides on
  opposite tacks, separated by at most maneuver_window_s with no recording
  gap, bracket a maneuver. Following the heading (unwrapped TWA) through the
  gap tells us whether the bow went through the wind (tack) or through dead
  downwind (gybe) -- this is robust to slow, wobbly or fallen-in maneuvers.

  Start/end = last/first sample within 10 deg of the steady heading before /
  after. Distance lost = what entry VMG would have covered along the wind
  axis from start until speed recovers to 90 % of entry, minus the progress
  actually made.

Legs
  Steady sailing between maneuvers and stops, further split when the point
  of sail (upwind / reach / downwind) changes for more than min_leg_s.
"""

from __future__ import annotations

import numpy as np

from app.analysis.clean import Clean
from app.analysis.geo import KN, angdiff, circ_mean, runs

ENTRY_S = 5.0
RECOVER_CAP_S = 30.0


def _seg_id(c: Clean) -> np.ndarray:
    sid = np.zeros(len(c.t), dtype=int)
    for k, (s, e) in enumerate(c.segments):
        sid[s:e] = k
    return sid


def side_runs(c: Clean, twa: np.ndarray, cfg: dict, v_side: float) -> list[tuple[int, int, int]]:
    """Steady runs as (start, end_exclusive, side) with side +1 port / -1 starboard."""
    atwa = np.abs(twa)
    side = np.where((atwa > 5) & (atwa < 175), np.sign(twa), 0).astype(int)
    steady = (c.speed >= v_side) & (np.abs(c.turn) < cfg["steady_turn_dps"])
    side = np.where(steady, side, 0)
    sid = _seg_id(c)
    out: list[tuple[int, int, int]] = []
    for sd in (1, -1):
        for s, e in runs(side == sd):
            out.append((s, e, sd))
    out.sort()
    # merge same-side runs separated by <= 3 s of unsteadiness (wobbles, chop)
    merged: list[list[int]] = []
    for s, e, sd in out:
        if merged and merged[-1][2] == sd and sid[s] == sid[merged[-1][1] - 1] \
                and c.t[s] - c.t[merged[-1][1] - 1] <= 3.0:
            merged[-1][1] = e
        else:
            merged.append([s, e, sd])
    return [(s, e, sd) for s, e, sd in merged
            if c.t[e - 1] - c.t[s] + 1 >= cfg["min_steady_s"]]


def _circ_median_heading(h: np.ndarray) -> float:
    return circ_mean(h) if len(h) else float("nan")


def _window(c: Clean, i: int, seconds: float, before: bool) -> slice:
    sid_s, sid_e = next((s, e) for s, e in c.segments if s <= i < e)
    if before:
        j = np.searchsorted(c.t, c.t[i] - seconds, side="left")
        return slice(max(j, sid_s), i)
    j = np.searchsorted(c.t, c.t[i] + seconds, side="right")
    return slice(i + 1, min(j, sid_e))


def detect(c: Clean, twa: np.ndarray, vmg: np.ndarray, state: np.ndarray, cfg: dict) -> list[dict]:
    stop = cfg["stop_kn"] / KN
    moving = c.speed > stop
    p75 = np.percentile(c.speed[moving], 75) if moving.sum() > 30 else 3.0
    v_side = max(1.5 * stop, 0.3 * p75)
    sruns = side_runs(c, twa, cfg, v_side)
    sid = _seg_id(c)
    out: list[dict] = []
    for (a0, a1, sa), (b0, b1, sb) in zip(sruns, sruns[1:]):
        if sa == sb or sid[a1 - 1] != sid[b0]:
            continue
        if c.t[b0] - c.t[a1 - 1] > cfg["maneuver_window_s"]:
            continue
        ia, ib = a1 - 1, b0
        # unwrapped TWA path through the gap
        d = angdiff(twa[ia + 1:ib + 1], twa[ia:ib])
        path = twa[ia] + np.concatenate([[0.0], np.cumsum(d)])
        net = path[-1] - path[0]
        lo, hi = sorted((path[0], path[0] + net))
        mults = [m * 180 for m in range(int(np.ceil(lo / 180.0)), int(np.floor(hi / 180.0)) + 1)
                 if lo < m * 180 < hi]
        if not mults:
            continue
        first = mults[0] if net > 0 else mults[-1]
        kind = "tack" if int(round(first / 180.0)) % 2 == 0 else "gybe"
        crossed = np.flatnonzero((path - first) * (path[0] - first) <= 0)
        ic = ia + int(crossed[0]) if len(crossed) else (ia + ib) // 2

        h_a = _circ_median_heading(c.heading[max(a0, a1 - 8):a1])
        h_b = _circ_median_heading(c.heading[b0:min(b1, b0 + 8)])
        start = ic
        lo_i = max(a0, int(np.searchsorted(c.t, c.t[ic] - 30.0)))
        while start > lo_i and abs(angdiff(c.heading[start], h_a)) > 10:
            start -= 1
        end = ic
        hi_i = min(b1 - 1, int(np.searchsorted(c.t, c.t[ic] + 30.0)))
        while end < hi_i and abs(angdiff(c.heading[end], h_b)) > 10:
            end += 1

        ent = _window(c, start, ENTRY_S, before=True)
        ext = _window(c, end, ENTRY_S, before=False)
        v_entry = float(np.mean(c.speed[ent])) if ent.stop > ent.start else float(c.speed[start])
        v_exit = float(np.mean(c.speed[ext])) if ext.stop > ext.start else float(c.speed[end])
        seg_min = slice(start, end + 1)
        imin = start + int(np.argmin(c.speed[seg_min]))

        rec = end
        seg_end = next(e for s, e in c.segments if s <= end < e)
        rec_cap = c.t[start] + RECOVER_CAP_S + (c.t[end] - c.t[start])
        while rec + 1 < seg_end and c.speed[rec] < 0.9 * v_entry and c.t[rec + 1] <= rec_cap:
            rec += 1
        recovered = bool(c.speed[rec] >= 0.9 * v_entry)

        vmg_entry = float(np.mean(vmg[ent])) if ent.stop > ent.start else float(vmg[start])
        direction = 1.0 if vmg_entry >= 0 else -1.0
        span = float(c.t[rec] - c.t[start])
        progress = float(direction * np.sum(vmg[start + 1:rec + 1] * c.dt[start + 1:rec + 1]))
        loss_m = abs(vmg_entry) * span - progress

        on = state[start:end + 1]
        hr = c.hr[start:rec + 1]
        hr = hr[np.isfinite(hr)]
        out.append({
            "type": kind,
            "i0": int(start), "i1": int(end), "ic": int(ic), "i_rec": int(rec),
            "t0": float(c.t[start]), "t1": float(c.t[end]), "tc": float(c.t[ic]),
            "lat": float(c.lat[ic]), "lon": float(c.lon[ic]),
            "from_side": "port" if sa > 0 else "starboard",
            "direction": "upwind" if direction > 0 else "downwind",
            "turn": "right" if net < 0 else "left",
            "duration_s": round(float(c.t[end] - c.t[start]), 1),
            "entry_kn": round(v_entry * KN, 2),
            "exit_kn": round(v_exit * KN, 2),
            "min_kn": round(float(c.speed[imin]) * KN, 2),
            "t_min": float(c.t[imin]),
            "entry_twa": round(float(np.median(np.abs(twa[max(a0, a1 - 8):a1]))), 1),
            "exit_twa": round(float(np.median(np.abs(twa[b0:min(b1, b0 + 8)]))), 1),
            "turn_deg": round(float(abs(net)), 0),
            "recovery_s": round(span, 1), "recovered": recovered,
            "distance_lost_m": round(loss_m, 1),
            "success": bool(on.all()),
            "state_at_entry": bool(state[start]), "state_at_exit": bool(state[end]),
            "hr_avg": round(float(hr.mean()), 0) if len(hr) else None,
            "hr_max": round(float(hr.max()), 0) if len(hr) else None,
        })
    for k, m in enumerate(out):
        m["id"] = k + 1
    return out


def stops(c: Clean, cfg: dict, min_s: float = 10.0) -> list[tuple[int, int]]:
    slow = c.speed < cfg["stop_kn"] / KN
    return [(s, e) for s, e in runs(slow) if c.t[e - 1] - c.t[s] + 1 >= min_s]


def classify_twa(atwa: np.ndarray, cfg: dict) -> np.ndarray:
    """0 = upwind, 1 = reach, 2 = downwind."""
    cls = np.ones(len(atwa), dtype=int)
    cls[atwa <= cfg["upwind_max_twa"]] = 0
    cls[atwa >= cfg["downwind_min_twa"]] = 2
    return cls


LEG_TYPES = ("upwind", "reach", "downwind")


def legs(c: Clean, twa: np.ndarray, vmg: np.ndarray, state: np.ndarray,
         mans: list[dict], cfg: dict) -> list[dict]:
    n = len(c.t)
    excluded = np.zeros(n, dtype=bool)
    for m in mans:
        excluded[m["i0"]:m["i1"] + 1] = True
    for s, e in stops(c, cfg):
        excluded[s:e] = True
    excluded |= c.speed < cfg["stop_kn"] / KN
    atwa = np.abs(twa)
    min_leg = cfg["min_leg_s"]
    pieces: list[tuple[int, int]] = []
    for gs, ge in c.segments:
        for s, e in runs(~excluded[gs:ge]):
            s, e = s + gs, e + gs
            # split on sustained side change (e.g. a maneuver we didn't catch)
            # or point-of-sail change; short blips are absorbed
            key = classify_twa(atwa[s:e], cfg) * 2 + (twa[s:e] > 0)
            blocks = [[bs + s, be + s, key[bs]] for bs, be in _value_runs(key)]
            changed = True
            while changed and len(blocks) > 1:
                changed = False
                durs = [c.t[b[1] - 1] - c.t[b[0]] + 1 for b in blocks]
                k = int(np.argmin(durs))
                if durs[k] < min_leg:
                    # absorb into the longer neighbour
                    if k == 0:
                        j = 1
                    elif k == len(blocks) - 1:
                        j = k - 1
                    else:
                        j = k - 1 if durs[k - 1] >= durs[k + 1] else k + 1
                    lo, hi = min(k, j), max(k, j)
                    keep_key = blocks[j][2]
                    blocks[lo:hi + 1] = [[blocks[lo][0], blocks[hi][1], keep_key]]
                    changed = True
                    # merge equal neighbours
                    mb = [blocks[0]]
                    for b in blocks[1:]:
                        if b[2] == mb[-1][2]:
                            mb[-1][1] = b[1]
                        else:
                            mb.append(b)
                    blocks = mb
            pieces.extend((b[0], b[1]) for b in blocks)
    out = []
    for s, e in pieces:
        dur = float(c.t[e - 1] - c.t[s]) + 1.0
        if dur < min_leg:
            continue
        dt = c.dt[s:e].copy()
        dt[0] = 1.0 if dt[0] == 0 else dt[0]
        mean_abs = float(np.average(atwa[s:e], weights=dt))
        cls = LEG_TYPES[int(classify_twa(np.array([mean_abs]), cfg)[0])]
        port = float(np.sum(dt[twa[s:e] > 0])) >= 0.5 * dt.sum()
        dist = float(c.step[s + 1:e].sum())
        gain = float(np.sum(vmg[s:e] * dt))
        hr = c.hr[s:e]
        hr = hr[np.isfinite(hr)]
        out.append({
            "i0": int(s), "i1": int(e - 1), "t0": float(c.t[s]), "t1": float(c.t[e - 1]),
            "type": cls, "side": "port" if port else "starboard",
            "duration_s": round(dur, 1), "distance_m": round(dist, 1),
            "avg_kn": round(dist / dur * KN, 2),
            "max_kn": round(float(c.speed[s:e].max() * KN), 2),
            "avg_twa": round(mean_abs, 1),
            "avg_vmg_kn": round(gain / dur * KN, 2),
            "gain_m": round(gain, 1),
            "state_pct": round(100.0 * float(dt[state[s:e]].sum()) / float(dt.sum()), 1),
            "hr_avg": round(float(hr.mean()), 0) if len(hr) else None,
            "lat0": float(c.lat[s]), "lon0": float(c.lon[s]),
            "lat1": float(c.lat[e - 1]), "lon1": float(c.lon[e - 1]),
        })
    for k, lg in enumerate(out):
        lg["id"] = k + 1
    return out


def _value_runs(arr: np.ndarray) -> list[tuple[int, int]]:
    if len(arr) == 0:
        return []
    change = np.flatnonzero(np.diff(arr) != 0) + 1
    edges = [0, *change.tolist(), len(arr)]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
