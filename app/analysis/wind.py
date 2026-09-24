"""Infer true wind direction (the direction it blows FROM) from the track.

GPS gives no wind, but how you sail encodes it:
  * close-hauled courses on port and starboard sit symmetrically either
    side of the wind, so the heading distribution is (roughly) mirror-
    symmetric about the wind axis -- same for broad reaches downwind;
  * nobody sails inside the no-go zone, so there's an empty arc centred
    on the wind (this breaks the 180 deg axis ambiguity: dead downwind is
    sailable, dead upwind isn't).

Global estimate: maximise  symmetry(W) - 2 * nogo_mass(W)  (+ a small
prior term when a hint exists), then refine with the bisector of the port
and starboard upwind modes (and downwind modes + 180), weighting upwind 2x
because close-hauled angles are the most consistent thing a sailor does.

Shifts: the same refinement in a rolling window, constrained to the global
estimate +- wind_max_shift_deg, smoothed and interpolated per point.
"""

from __future__ import annotations

import numpy as np

from app.analysis.clean import Clean
from app.analysis.geo import (
    KN, angdiff, circ_mean, circ_resultant, circular_convolve, gaussian_kernel, runs,
)

_DEG = np.arange(360, dtype=float)
_MIRROR = (2 * np.arange(360)[:, None] - np.arange(360)[None, :]) % 360  # [W, theta]
_NOGO_D = np.abs(angdiff(_DEG[None, :], _DEG[:, None]))                 # [W, theta]
_NOGO_K = np.clip((50.0 - _NOGO_D) / 20.0, 0.0, 1.0)                      # 1 inside 30, 0 past 50


def steady_mask(c: Clean, cfg: dict) -> np.ndarray:
    """Samples of steady sailing at speed -- what the wind is inferred from."""
    stop = cfg["stop_kn"] / KN
    moving = (c.speed > stop) & (c.dt > 0)
    if moving.sum() < 30:
        return moving & c.good
    p75 = np.percentile(c.speed[moving], 75)
    thr = max(1.5 * stop, 0.45 * p75)
    return (c.speed >= thr) & (np.abs(c.turn) < cfg["steady_turn_dps"]) & c.good & (c.dt > 0)


def heading_hist(hdg: np.ndarray, w: np.ndarray, sigma: float = 3.0) -> np.ndarray:
    b = np.floor(hdg).astype(int) % 360
    h = np.bincount(b, weights=w, minlength=360).astype(float)
    return circular_convolve(h, gaussian_kernel(sigma))


def axis_scores(H: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    tot = H.sum()
    if tot <= 0:
        return np.zeros(360), np.zeros(360)
    sym = np.minimum(H[None, :], H[_MIRROR]).sum(axis=1) / tot
    nogo = (H[None, :] * _NOGO_K).sum(axis=1) / tot
    return sym, nogo


def _mode_centroid(H: np.ndarray, W: float, lo: float, hi: float) -> tuple[float, float] | None:
    """Peak of H among headings with lo < (heading - W) < hi, refined by the
    centroid within +-10 deg of the peak. Returns (heading, mass)."""
    rel = angdiff(_DEG, W)
    sel = (rel > lo) & (rel < hi)
    if not sel.any():
        return None
    mass = H[sel].sum()
    if mass <= 0:
        return None
    peak = _DEG[sel][np.argmax(H[sel])]
    near = np.abs(angdiff(_DEG, peak)) <= 10
    return circ_mean(_DEG[near], H[near]), float(mass)


def refine(H: np.ndarray, W0: float, min_frac: float = 0.05) -> tuple[float, dict]:
    """Bisector of port/starboard upwind modes (and downwind modes + 180)."""
    tot = H.sum()
    W = W0
    info: dict = {}
    for _ in range(3):
        ests, wts = [], []
        up_p, up_s = _mode_centroid(H, W, 15, 85), _mode_centroid(H, W, -85, -15)
        if up_p and up_s and min(up_p[1], up_s[1]) >= min_frac * tot:
            ests.append(circ_mean([up_p[0], up_s[0]]))
            wts.append(2.0 * min(up_p[1], up_s[1]))
            info["upwind_modes"] = [round(up_p[0], 1), round(up_s[0], 1)]
        dn_p, dn_s = _mode_centroid(H, W, 95, 172), _mode_centroid(H, W, -172, -95)
        if dn_p and dn_s and min(dn_p[1], dn_s[1]) >= min_frac * tot:
            ests.append((circ_mean([dn_p[0], dn_s[0]]) + 180.0) % 360.0)
            wts.append(min(dn_p[1], dn_s[1]))
            info["downwind_modes"] = [round(dn_p[0], 1), round(dn_s[0], 1)]
        ests_ok = [(e, w) for e, w in zip(ests, wts) if abs(angdiff(e, W0)) <= 25]
        if not ests_ok:
            break
        W_new = circ_mean([e for e, _ in ests_ok], [w for _, w in ests_ok])
        if abs(angdiff(W_new, W)) < 0.2:
            W = W_new
            break
        W = W_new
    info["refined"] = bool(info.get("upwind_modes") or info.get("downwind_modes"))
    return float(W % 360.0), info


def drift_hint(c: Clean, cfg: dict) -> tuple[float | None, float]:
    """When you're in the water, you drift downwind. Returns (wind_from, weight)."""
    slow = (c.speed < 0.8) & c.good
    vecs = []
    for s, e in runs(slow):
        if e - s < 15 or any(s < a < e for a, _ in c.segments):
            continue
        vecs.append((c.x[e - 1] - c.x[s], c.y[e - 1] - c.y[s]))
    if not vecs:
        return None, 0.0
    v = np.array(vecs)
    L = np.hypot(v[:, 0], v[:, 1])
    if L.sum() < 30:
        return None, 0.0
    dirs = np.degrees(np.arctan2(v[:, 0], v[:, 1])) % 360.0
    R = circ_resultant(dirs, L)
    if R < 0.5:
        return None, 0.0
    return (circ_mean(dirs, L) + 180.0) % 360.0, 0.08 * R


def estimate_global(hdg: np.ndarray, w: np.ndarray, prior: float | None = None,
                    prior_weight: float = 0.2, drift: tuple[float | None, float] = (None, 0.0)) -> dict:
    if w.sum() < 60:
        base = prior if prior is not None else 0.0
        return {"deg": float(base), "confidence": "none", "symmetry": 0.0, "margin": 0.0,
                "raw_deg": float(base), "refine": {}}
    H = heading_hist(hdg, w)
    sym, nogo = axis_scores(H)
    score = sym - 2.0 * nogo
    if prior is not None:
        score = score + prior_weight * np.cos(np.radians(_DEG - prior))
    if drift[0] is not None:
        score = score + drift[1] * np.cos(np.radians(_DEG - drift[0]))
    W0 = float(np.argmax(score))
    W, info = refine(H, W0)
    flip = int(round(W0 + 180)) % 360
    margin = float(score[int(round(W0)) % 360] - score[flip])
    s = float(sym[int(round(W)) % 360])
    conf = "high" if (margin > 0.3 and s > 0.55 and info["refined"]) else "medium" if margin > 0.1 else "low"
    return {"deg": W, "raw_deg": W0, "confidence": conf, "symmetry": round(s, 3),
            "margin": round(margin, 3), "refine": info}


def rolling(t: np.ndarray, hdg: np.ndarray, w: np.ndarray, W_global: float, cfg: dict) -> tuple[list, list]:
    """Wind shifts, the way a sailor reads them off the compass.

    Each tack's typical sailing angle (close-hauled port/starboard, broad
    reach port/starboard) is calibrated against the global estimate. In
    each rolling window, every one of those modes found near where it's
    expected gives its own wind estimate (mode heading - calibrated angle);
    the window's wind is their weighted mean. One tack is enough, a reach
    that isn't at a calibrated angle is ignored, and asymmetric pointing
    doesn't bias the result. Windows are tracked in time order around the
    previous estimate, so slow rotations beyond the search width are
    followed."""
    win = cfg["wind_window_min"] * 60.0
    step = cfg["wind_step_min"] * 60.0
    lim = cfg["wind_max_shift_deg"]
    if len(t) == 0 or t[-1] - t[0] < win * 0.75 or w.sum() <= 0:
        return [], []
    sel = w > 0
    H = heading_hist(hdg[sel], w[sel])
    refs = []
    for lo, hi, wt in ((15, 85, 2.0), (-85, -15, 2.0), (95, 172, 1.0), (-172, -95, 1.0)):
        m = _mode_centroid(H, W_global, lo, hi)
        if m and m[1] >= 0.05 * H.sum():
            refs.append((float(angdiff(m[0], W_global)), wt))
    if not refs:
        return [], []
    centers = np.arange(t[0] + win / 2, t[-1] - win / 2 + step, step)
    ts, ws = [], []
    W_prev = W_global
    for tc in centers:
        m = (t >= tc - win / 2) & (t < tc + win / 2) & sel
        if w[m].sum() < 120:
            continue
        Hw = heading_hist(hdg[m], w[m])
        tot = Hw.sum()
        ests = []
        for p, wt in refs:
            expected = W_prev + p
            near_exp = np.abs(angdiff(_DEG, expected)) <= 20
            if Hw[near_exp].max() <= 0:
                continue
            peak = _DEG[near_exp][np.argmax(Hw[near_exp])]
            if abs(angdiff(peak, expected)) >= 19:  # slope of some other mode, not a peak
                continue
            near = np.abs(angdiff(_DEG, peak)) <= 10
            mass = Hw[near].sum()
            if mass < max(0.08 * tot, 60.0):
                continue
            ests.append(((circ_mean(_DEG[near], Hw[near]) - p) % 360.0, wt * mass))
        if not ests:
            continue
        Wk = circ_mean([e for e, _ in ests], [x for _, x in ests])
        rel = float(angdiff(Wk, W_global))
        if abs(rel) > lim:
            continue
        ts.append(float(np.average(t[m], weights=w[m])))
        ws.append(rel)
        W_prev = Wk
    if len(ws) >= 3:  # 3-point median kills single-window outliers
        arr = np.array(ws)
        med = arr.copy()
        med[1:-1] = np.median(np.vstack([arr[:-2], arr[1:-1], arr[2:]]), axis=0)
        ws = med.tolist()
    return ts, [(W_global + r) % 360.0 for r in ws]


def per_point(t: np.ndarray, W_global: float, ts: list, ws: list) -> np.ndarray:
    if len(ts) < 2:
        return np.full(len(t), W_global)
    rel = angdiff(np.array(ws), W_global)
    return (W_global + np.interp(t, np.array(ts), rel)) % 360.0


def keyframe_wind(t: np.ndarray, frames: list[dict]) -> np.ndarray:
    fr = sorted(frames, key=lambda f: f["t"])
    ft = np.array([float(f["t"]) for f in fr])
    fd = np.degrees(np.unwrap(np.radians([float(f["deg"]) for f in fr])))
    return np.interp(t, ft, fd) % 360.0


PRIOR_WEIGHTS = {"manual": 3.0, "open-meteo": 1.0, "device": 0.35}


def direction_votes(c: Clean, cfg: dict, pts: np.ndarray, mask: np.ndarray, g: dict,
                    drift: tuple[float | None, float], prior: float | None, prior_src: str | None) -> dict:
    """The axis is known; decide which end is upwind. Each vote is in [-1, 1]
    (+ = current orientation is right) times a weight:
      time    you spend longer going upwind than down (you come back to the
              launch, and upwind VMG is lower)
      speed   upwind is slower than broad reaching/downwind
      turns   turning through head-to-wind costs more speed than through
              dead downwind
      nogo    the empty arc is wider upwind (weak for foils: their downwind
              gap is nearly as wide)
      drift   in the water you drift downwind
      prior   Open-Meteo / device setting, if any
    """
    from app.analysis.maneuvers import detect  # local: maneuvers is heavier

    twa = ((c.heading - pts + 180.0) % 360.0) - 180.0
    atwa = np.abs(twa)
    dt = np.where(mask, c.dt, 0.0)
    votes: dict[str, tuple[float, float]] = {}
    up, dn = mask & (atwa < 80), mask & (atwa > 100)  # beam reaches say nothing
    t_up, t_dn = dt[up].sum(), dt[dn].sum()
    # only as strong as the share of sailing that's clearly up- or downwind
    coverage = float(min(1.0, (t_up + t_dn) / max(0.4 * dt.sum(), 1.0)))
    if t_up + t_dn > 120:
        votes["time"] = (float(np.clip(3 * (t_up - t_dn) / (t_up + t_dn), -1, 1)), 1.0 * coverage)
    if t_up > 30 and t_dn > 30:
        s_up = np.average(c.speed[up], weights=dt[up])
        s_dn = np.average(c.speed[dn], weights=dt[dn])
        votes["speed"] = (float(np.clip(5 * (s_dn - s_up) / (0.5 * (s_up + s_dn)), -1, 1)), 0.7 * coverage)
    vmg = c.speed * np.cos(np.radians(twa))
    on = np.zeros(len(c.t), dtype=bool)
    mans = detect(c, twa, vmg, on, cfg)
    ratio = {k: [m["min_kn"] / m["entry_kn"] for m in mans if m["type"] == k and m["entry_kn"] > 1]
             for k in ("tack", "gybe")}
    if len(ratio["tack"]) >= 3 and len(ratio["gybe"]) >= 3:
        d = float(np.median(ratio["gybe"]) - np.median(ratio["tack"]))
        votes["turns"] = (float(np.clip(5 * d, -1, 1)), 0.7)
    votes["nogo"] = (float(np.clip(3 * g["margin"], -1, 1)), 0.4)
    if drift[0] is not None:
        votes["drift"] = (float(np.cos(np.radians(angdiff(drift[0], g["deg"])))), 5 * drift[1])
    if prior is not None:
        votes["prior"] = (float(np.cos(np.radians(angdiff(prior, g["deg"])))),
                          PRIOR_WEIGHTS.get(prior_src or "device", 0.35))
    score = sum(v * w for v, w in votes.values())
    total_w = sum(w for _, w in votes.values()) or 1.0
    return {"score": round(score, 3), "strength": round(abs(score) / total_w, 3),
            "votes": {k: [round(v, 2), round(w, 2)] for k, (v, w) in votes.items()}}


def estimate(c: Clean, cfg: dict) -> dict:
    """Returns wind info plus `per_point` (deg from, one per sample)."""
    mask = steady_mask(c, cfg)
    w = np.where(mask, c.dt, 0.0)
    prior = cfg.get("wind_prior_deg")
    prior_src = cfg.get("wind_prior_source") or ("device" if prior is not None else None)
    drift = drift_hint(c, cfg)
    g = estimate_global(c.heading[mask], w[mask])
    out = {
        "confidence": g["confidence"], "symmetry": g["symmetry"],
        "margin": g["margin"], "modes": g["refine"], "prior_deg": prior, "prior_source": prior_src,
        "drift_hint_deg": None if drift[0] is None else round(drift[0], 1),
        "steady_s": round(float(w.sum()), 1),
    }
    ts, ws = rolling(c.t, c.heading, w, g["deg"], cfg)
    pts = per_point(c.t, g["deg"], ts, ws)
    if g["confidence"] != "none":
        dv = direction_votes(c, cfg, pts, mask, g, drift, prior, prior_src)
        out["direction"] = dv
        if dv["score"] < 0:
            g["deg"] = (g["deg"] + 180.0) % 360.0
            pts = (pts + 180.0) % 360.0
            ws = [(x + 180.0) % 360.0 for x in ws]
            out["modes"] = {"flipped": True, **g["refine"]}
        s, score = dv["strength"], abs(dv["score"])
        axis_ok = g["symmetry"] > 0.45 and g["refine"].get("refined")
        out["confidence"] = ("high" if (score >= 1.0 and s >= 0.4 and axis_ok)
                             else "medium" if score >= 0.45 else "low")
    elif prior is not None:
        g["deg"] = float(prior)
        pts = np.full(len(c.t), float(prior))
        ts, ws = [], []
    out["auto_deg"] = round(g["deg"], 1)
    frames = cfg.get("wind_keyframes") or []
    override = cfg.get("wind_override_deg")
    if len(frames) >= 1:
        pts = keyframe_wind(c.t, frames) if len(frames) > 1 else np.full(len(c.t), float(frames[0]["deg"]) % 360)
        out.update(source="keyframes", deg=round(circ_mean(pts), 1), series_t=[], series_deg=[])
    elif override is not None:
        pts = np.full(len(c.t), float(override) % 360.0)
        out.update(source="manual", deg=round(float(override) % 360.0, 1), series_t=[], series_deg=[])
    else:
        out.update(source="auto", deg=round(g["deg"], 1), series_t=ts, series_deg=[round(x, 1) for x in ws])
    # heading rose (5 deg bins, minutes) for the UI
    rose = np.bincount((np.floor(c.heading[mask] / 5).astype(int)) % 72, weights=w[mask] / 60.0, minlength=72)
    out["rose_min"] = [round(float(v), 2) for v in rose]
    out["per_point"] = pts
    out["shift_range_deg"] = round(float(np.ptp(angdiff(pts, out["deg"])))) if len(pts) else 0
    return out
