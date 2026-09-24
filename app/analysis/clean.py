"""Cleaning: turn raw fixes into trustworthy speed / heading series.

Steps
  1. project to a local metric frame
  2. drop position jumps (a fix that implies > max_speed both in and out)
  3. split into segments at recording gaps (> gap_s)
  4. speed: device Doppler speed when present (it's far less noisy than
     differentiated positions), else position-derived
  5. drop speed spikes: 1-3 sample excursions whose acceleration in AND out
     exceed max_accel with opposite signs -- real crashes decelerate hard
     but don't bounce straight back
  6. heading: course over ground from a +-2 s central difference, smoothed
     on the unit circle; turn rate from its unwrapped derivative
  7. flag bad fixes (HDOP above threshold, when the file carries HDOP)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.analysis.geo import KN, bearing_xy, project, runs, smooth_angles
from app.analysis.loader import Track


@dataclass
class Clean:
    t: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    x: np.ndarray
    y: np.ndarray
    speed: np.ndarray        # m/s, cleaned
    speed_s: np.ndarray      # m/s, 3-sample median (for state hysteresis)
    heading: np.ndarray      # deg, smoothed COG
    turn: np.ndarray         # deg/s, signed (+ = clockwise / to starboard)
    dt: np.ndarray           # s, duration each sample represents (0 at gaps)
    step: np.ndarray         # m, distance travelled from previous sample (0 at gaps)
    hr: np.ndarray
    temp: np.ndarray
    good: np.ndarray         # bool: usable fix (not flagged)
    segments: list[tuple[int, int]]
    lat0: float
    lon0: float
    report: dict = field(default_factory=dict)


def _segments(t: np.ndarray, gap_s: float) -> list[tuple[int, int]]:
    if len(t) == 0:
        return []
    breaks = np.flatnonzero(np.diff(t) > gap_s) + 1
    edges = [0, *breaks.tolist(), len(t)]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1) if edges[i + 1] > edges[i]]


def _drop_position_jumps(t, x, y, vmax) -> np.ndarray:
    keep = np.ones(len(t), dtype=bool)
    for _ in range(3):
        idx = np.flatnonzero(keep)
        if len(idx) < 3:
            break
        tx, xx, yx = t[idx], x[idx], y[idx]
        dt = np.maximum(np.diff(tx), 1e-3)
        v = np.hypot(np.diff(xx), np.diff(yx)) / dt
        vin = np.concatenate([[0.0], v])
        vout = np.concatenate([v, [0.0]])
        # the direct hop i-1 -> i+1 must be plausible, otherwise it's a real move
        skip = np.full(len(idx), np.inf)
        dt2 = np.maximum(tx[2:] - tx[:-2], 1e-3)
        skip[1:-1] = np.hypot(xx[2:] - xx[:-2], yx[2:] - yx[:-2]) / dt2
        bad = (vin > vmax) & (vout > vmax) & (skip < vmax)
        bad[0] = vout[0] > vmax and v[1] < vmax if len(v) > 1 else False
        bad[-1] = vin[-1] > vmax and v[-2] < vmax if len(v) > 1 else False
        if not bad.any():
            break
        keep[idx[bad]] = False
    return keep


def _despike(v: np.ndarray, t: np.ndarray, thr: float, segments) -> tuple[np.ndarray, int]:
    v = v.copy()
    n_spikes = 0
    for s, e in segments:
        for w in (1, 2, 3):
            i = s + 1
            while i + w < e:
                dt_in = max(t[i] - t[i - 1], 1e-3)
                dt_out = max(t[i + w] - t[i + w - 1], 1e-3)
                a_in = (v[i] - v[i - 1]) / dt_in
                a_out = (v[i + w] - v[i + w - 1]) / dt_out
                span = max(t[i + w] - t[i - 1], 1e-3)
                plateau = v[i:i + w]
                if (abs(a_in) > thr and abs(a_out) > thr and np.sign(a_in) != np.sign(a_out)
                        and abs(v[i + w] - v[i - 1]) < 0.5 * thr * span
                        and np.all(np.sign(plateau - v[i - 1]) == np.sign(a_in))):
                    frac = (t[i:i + w] - t[i - 1]) / span
                    v[i:i + w] = v[i - 1] + frac * (v[i + w] - v[i - 1])
                    n_spikes += w
                    i += w
                else:
                    i += 1
    return v, n_spikes


def _cog(x, y, segments, k: int = 2) -> np.ndarray:
    hdg = np.full(len(x), np.nan)
    for s, e in segments:
        n = e - s
        if n < 2:
            continue
        xs, ys = x[s:e], y[s:e]
        lo = np.clip(np.arange(n) - k, 0, n - 1)
        hi = np.clip(np.arange(n) + k, 0, n - 1)
        dx, dy = xs[hi] - xs[lo], ys[hi] - ys[lo]
        h = bearing_xy(dx, dy)
        h[np.hypot(dx, dy) < 0.5] = np.nan
        hdg[s:e] = h
    return hdg


def _fill_nan_angles(h: np.ndarray, segments) -> np.ndarray:
    out = h.copy()
    for s, e in segments:
        seg = out[s:e]
        m = np.isfinite(seg)
        if not m.any():
            out[s:e] = 0.0
            continue
        idx = np.arange(len(seg))
        un = np.degrees(np.unwrap(np.radians(seg[m])))
        seg[:] = np.interp(idx, idx[m], un) % 360.0
    return out


def clean(track: Track, cfg: dict) -> Clean:
    t = track.t.astype(float)
    lat, lon = track.lat.astype(float), track.lon.astype(float)
    lat0, lon0 = float(np.median(lat)), float(np.median(lon))
    x, y = project(lat, lon, lat0, lon0)
    report: dict = {"input_fixes": int(len(t))}

    vmax = cfg["max_speed_kn"] / KN
    keep = _drop_position_jumps(t, x, y, vmax)
    report["position_jumps_dropped"] = int((~keep).sum())
    t, lat, lon, x, y = t[keep], lat[keep], lon[keep], x[keep], y[keep]
    dev_speed = track.speed[keep]
    hr, temp, hdop = track.hr[keep], track.temp[keep], track.hdop[keep]

    segments = _segments(t, cfg["gap_s"])
    report["segments"] = len(segments)
    report["gaps_s"] = float(sum(t[b[0]] - t[a[1] - 1] for a, b in zip(segments, segments[1:])))

    # position-derived speed (central difference within segments)
    pos_speed = np.zeros(len(t))
    for s, e in segments:
        if e - s < 2:
            continue
        ts, xs, ys = t[s:e], x[s:e], y[s:e]
        n = e - s
        lo = np.clip(np.arange(n) - 1, 0, n - 1)
        hi = np.clip(np.arange(n) + 1, 0, n - 1)
        pos_speed[s:e] = np.hypot(xs[hi] - xs[lo], ys[hi] - ys[lo]) / np.maximum(ts[hi] - ts[lo], 1e-3)

    have_dev = np.isfinite(dev_speed)
    use_device = have_dev.mean() >= 0.8 if len(t) else False
    report["speed_source"] = "device" if use_device else "position"
    speed = np.where(have_dev, dev_speed, pos_speed) if use_device else pos_speed.copy()
    speed = np.clip(np.nan_to_num(speed, nan=0.0), 0.0, vmax)
    speed, n_spikes = _despike(speed, t, cfg["max_accel_ms2"], segments)
    report["speed_spikes_removed"] = int(n_spikes)

    # median-of-3 for state hysteresis so single-sample noise can't toggle it
    speed_s = speed.copy()
    for s, e in segments:
        if e - s >= 3:
            seg = speed[s:e]
            stacked = np.vstack([np.r_[seg[0], seg[:-1]], seg, np.r_[seg[1:], seg[-1]]])
            speed_s[s:e] = np.median(stacked, axis=0)

    hdg = _fill_nan_angles(_cog(x, y, segments), segments)
    hdg = smooth_angles(hdg, cfg["heading_sigma_s"], segments)

    turn = np.zeros(len(t))
    for s, e in segments:
        if e - s < 3:
            continue
        un = np.degrees(np.unwrap(np.radians(hdg[s:e])))
        turn[s:e] = np.gradient(un, t[s:e])

    dt = np.zeros(len(t))
    step = np.zeros(len(t))
    for s, e in segments:
        if e - s < 2:
            continue
        d = np.diff(t[s:e])
        dt[s + 1:e] = d
        step[s + 1:e] = 0.5 * (speed[s:e - 1] + speed[s + 1:e]) * d

    good = np.ones(len(t), dtype=bool)
    if np.isfinite(hdop).any():
        good &= ~(np.isfinite(hdop) & (hdop > cfg["max_hdop"]))
    report["bad_hdop_fixes"] = int((~good).sum())
    report["hdop_available"] = bool(np.isfinite(hdop).any())
    # Doppler vs position disagreement is the HDOP stand-in when there's no HDOP
    if use_device:
        mismatch = (np.abs(speed - pos_speed) > np.maximum(4.0, 0.5 * speed)) & (dt > 0)
        for s, e in runs(mismatch):
            if e - s >= 2:
                good[s:e] = False
        report["suspect_fixes"] = int(mismatch.sum())

    return Clean(
        t=t, lat=lat, lon=lon, x=x, y=y, speed=speed, speed_s=speed_s, heading=hdg,
        turn=turn, dt=dt, step=step, hr=hr, temp=temp, good=good, segments=segments,
        lat0=lat0, lon0=lon0, report=report,
    )
