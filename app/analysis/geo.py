"""Small geometry + circular-statistics helpers shared by the engine.

Angles are compass degrees (0 = north, clockwise). Positions are projected
to a local flat east/north frame in metres -- sessions cover a few km, where
an equirectangular projection is accurate to well under a GPS error.
"""

from __future__ import annotations

import numpy as np

EARTH_R = 6_371_008.8
KN = 1.943844  # m/s -> knots
NM = 1852.0


def wrap180(a):
    """Wrap degrees to (-180, 180]."""
    a = np.asarray(a, dtype=float)
    r = (a + 180.0) % 360.0 - 180.0
    return np.where(r == -180.0, 180.0, r)


def project(lat, lon, lat0: float, lon0: float):
    """lat/lon degrees -> (x east, y north) metres around (lat0, lon0)."""
    k = np.pi / 180.0
    x = (np.asarray(lon, dtype=float) - lon0) * k * EARTH_R * np.cos(lat0 * k)
    y = (np.asarray(lat, dtype=float) - lat0) * k * EARTH_R
    return x, y


def unproject(x, y, lat0: float, lon0: float):
    k = np.pi / 180.0
    lat = lat0 + np.asarray(y) / (k * EARTH_R)
    lon = lon0 + np.asarray(x) / (k * EARTH_R * np.cos(lat0 * k))
    return lat, lon


def bearing_xy(dx, dy):
    """Compass bearing of a displacement in the local frame."""
    return np.degrees(np.arctan2(dx, dy)) % 360.0


def circ_mean(deg, weights=None) -> float:
    deg = np.asarray(deg, dtype=float)
    if deg.size == 0:
        return float("nan")
    w = np.ones_like(deg) if weights is None else np.asarray(weights, dtype=float)
    r = np.radians(deg)
    s, c = np.sum(w * np.sin(r)), np.sum(w * np.cos(r))
    if abs(s) < 1e-12 and abs(c) < 1e-12:
        return float("nan")
    return float(np.degrees(np.arctan2(s, c)) % 360.0)


def circ_resultant(deg, weights=None) -> float:
    """Mean resultant length in [0, 1] -- 1 means all angles agree."""
    deg = np.asarray(deg, dtype=float)
    if deg.size == 0:
        return 0.0
    w = np.ones_like(deg) if weights is None else np.asarray(weights, dtype=float)
    tot = np.sum(w)
    if tot <= 0:
        return 0.0
    r = np.radians(deg)
    return float(np.hypot(np.sum(w * np.sin(r)), np.sum(w * np.cos(r))) / tot)


def angdiff(a, b):
    """Signed smallest difference a - b in degrees."""
    return wrap180(np.asarray(a, dtype=float) - np.asarray(b, dtype=float))


def gaussian_kernel(sigma: float, radius: int | None = None) -> np.ndarray:
    radius = radius if radius is not None else max(1, int(round(3 * sigma)))
    x = np.arange(-radius, radius + 1, dtype=float)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    return k / k.sum()


def circular_convolve(hist: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Convolve a circular histogram with a symmetric kernel."""
    r = len(kernel) // 2
    padded = np.concatenate([hist[-r:], hist, hist[:r]])
    return np.convolve(padded, kernel, mode="valid")


def smooth_series(values, sigma: float, segments: list[tuple[int, int]]) -> np.ndarray:
    """Gaussian-smooth a series independently within each [start, end) segment
    so smoothing never bleeds across recording gaps. NaNs are ignored."""
    out = np.array(values, dtype=float, copy=True)
    if sigma <= 0:
        return out
    k = gaussian_kernel(sigma)
    for s, e in segments:
        seg = out[s:e]
        if seg.size == 0:
            continue
        m = np.isfinite(seg)
        filled = np.where(m, seg, 0.0)
        num = np.convolve(filled, k, mode="same")
        den = np.convolve(m.astype(float), k, mode="same")
        with np.errstate(invalid="ignore", divide="ignore"):
            out[s:e] = np.where(den > 1e-9, num / den, np.nan)
    return out


def smooth_angles(deg, sigma: float, segments: list[tuple[int, int]]) -> np.ndarray:
    """Smooth compass angles via their unit vectors."""
    r = np.radians(np.asarray(deg, dtype=float))
    s = smooth_series(np.sin(r), sigma, segments)
    c = smooth_series(np.cos(r), sigma, segments)
    return np.degrees(np.arctan2(s, c)) % 360.0


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index ranges where a boolean mask is True."""
    m = np.asarray(mask, dtype=bool)
    if m.size == 0:
        return []
    d = np.diff(np.concatenate([[0], m.astype(np.int8), [0]]))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return list(zip(starts.tolist(), ends.tolist()))
