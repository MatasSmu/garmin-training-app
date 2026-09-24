"""Best splits: highest average speed over a fixed time or distance.

Distance is integrated Doppler speed (the speed-sailing convention -- less
noisy than summed position deltas). Windows never span a recording gap.
"""

from __future__ import annotations

import numpy as np

from app.analysis.clean import Clean
from app.analysis.geo import KN, NM

TIME_SPLITS = [("2 s", 2.0), ("10 s", 10.0), ("1 min", 60.0), ("1 h", 3600.0)]
DIST_SPLITS = [("100 m", 100.0), ("500 m", 500.0), ("1 nm", NM)]


def _best_time(t, D, s, e, W):
    """Best mean speed over a window of exactly W seconds (interpolated end)."""
    ts, Ds = t[s:e], D[s:e]
    if ts[-1] - ts[0] < W:
        return None
    ends = ts + W
    j = np.searchsorted(ts, ends, side="left")
    ok = j < len(ts)
    if not ok.any():
        return None
    i = np.flatnonzero(ok)
    Dend = np.interp(ends[i], ts, Ds)
    v = (Dend - Ds[i]) / W
    k = int(np.argmax(v))
    return float(v[k]), s + int(i[k]), float(ts[i[k]]), float(ends[i[k]])


def _best_dist(t, D, s, e, L):
    ts, Ds = t[s:e], D[s:e]
    if Ds[-1] - Ds[0] < L:
        return None
    targets = Ds + L
    ok = targets <= Ds[-1]
    i = np.flatnonzero(ok)
    # Ds is non-decreasing but may be flat while stopped: interpolate time on it
    uniq, first = np.unique(Ds, return_index=True)
    t_end = np.interp(targets[i], uniq, ts[first])
    dur = t_end - ts[i]
    dur = np.where(dur <= 0, np.inf, dur)
    v = L / dur
    k = int(np.argmax(v))
    return float(v[k]), s + int(i[k]), float(ts[i[k]]), float(t_end[k])


def best_splits(c: Clean) -> list[dict]:
    D = np.cumsum(c.step)
    out = []
    for kind, specs, fn in (("time", TIME_SPLITS, _best_time), ("distance", DIST_SPLITS, _best_dist)):
        for label, size in specs:
            best = None
            for s, e in c.segments:
                if e - s < 2:
                    continue
                r = fn(c.t, D, s, e, size)
                if r and (best is None or r[0] > best[0]):
                    best = r
            if best is None:
                out.append({"label": label, "kind": kind, "size": size, "kn": None})
                continue
            v, i0, t0, t1 = best
            i1 = int(np.searchsorted(c.t, t1, side="left"))
            out.append({"label": label, "kind": kind, "size": size, "kn": round(v * KN, 2),
                        "t0": t0, "t1": t1, "i0": i0, "i1": min(i1, len(c.t) - 1),
                        "duration_s": round(t1 - t0, 1)})
    return out
