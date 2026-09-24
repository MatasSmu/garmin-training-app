"""Synthetic sailing tracks with known wind and maneuvers.

Used by the unit tests (and handy for poking at the engine): a kinematic
1 Hz simulation of a rider sailing upwind/downwind legs, tacking, gybing,
falling in, with GPS position noise and occasional Doppler speed spikes.
Ground truth (wind per second, maneuver crossing times/types) comes back
alongside the Track.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.analysis.geo import unproject
from app.analysis.loader import Track


@dataclass
class Truth:
    wind: np.ndarray                                   # deg from, per sample
    maneuvers: list[tuple[float, str]] = field(default_factory=list)  # (t_cross, tack|gybe)
    stops: list[tuple[float, float]] = field(default_factory=list)


class Sim:
    def __init__(self, wind_fn, seed: int = 0, t0: float = 1_780_000_000.0,
                 up_twa: float = 48.0, dn_twa: float = 140.0,
                 up_speed: float = 10.0, dn_speed: float = 12.5):
        self.rng = np.random.default_rng(seed)
        self.wind_fn = wind_fn
        self.t = t0
        self.x = self.y = 0.0
        self.v = up_speed
        self.twa = up_twa   # signed: + port, - starboard
        self.up_twa, self.dn_twa = up_twa, dn_twa
        self.up_speed, self.dn_speed = up_speed, dn_speed
        self.rows: list[tuple] = []
        self.truth = Truth(wind=np.array([]))
        self._h_noise = 0.0
        self._v_noise = 0.0
        self.winds: list[float] = []

    # ---- low level
    def _emit(self, heading: float, speed: float, drift: tuple[float, float] = (0.0, 0.0)):
        self._h_noise = 0.8 * self._h_noise + self.rng.normal(0, 1.2)
        self._v_noise = 0.85 * self._v_noise + self.rng.normal(0, 0.15)
        h = np.radians(heading + self._h_noise)
        v = max(0.0, speed + self._v_noise)
        self.x += v * np.sin(h) + drift[0]
        self.y += v * np.cos(h) + drift[1]
        self.rows.append((self.t, self.x, self.y, v))
        self.winds.append(self.wind_fn(self.t))
        self.t += 1.0

    def _heading(self, twa: float) -> float:
        return (self.wind_fn(self.t) + twa) % 360.0

    # ---- phases
    def sail(self, seconds: float, twa: float, speed: float):
        n = int(seconds)
        v0 = self.v
        for i in range(n):
            self.v = speed + (v0 - speed) * np.exp(-i / 4.0)
            self._emit(self._heading(twa), self.v)
        self.twa = twa

    def turn(self, to_twa: float, through: str, turn_s: float, v_min: float, recover_s: float):
        """Tack (through 0) or gybe (through 180) from current TWA to to_twa."""
        a0 = self.twa
        if through == "tack":
            path = np.linspace(a0, to_twa, int(turn_s) + 1)[1:]       # crosses 0
        else:
            end = to_twa + (360.0 if a0 > 0 else -360.0)                 # crosses +-180
            path = np.linspace(a0, end, int(turn_s) + 1)[1:]
        v_entry = self.v
        n = len(path)
        cross = None
        for i, a in enumerate(path):
            prog = (i + 1) / n
            v = v_entry - (v_entry - v_min) * np.sin(np.pi * min(prog, 1.0) / 2) ** 1.0
            if cross is None and ((through == "tack" and np.sign(a) != np.sign(a0))
                                  or (through == "gybe" and abs(a) >= 180)):
                cross = self.t
            self._emit(self._heading(float(a)), v)
            self.v = v
        self.twa = to_twa
        self.truth.maneuvers.append((cross if cross is not None else self.t, through))
        target = self.up_speed if abs(to_twa) < 90 else self.dn_speed
        for i in range(int(recover_s)):
            self.v = v_min + (target - v_min) * (i + 1) / recover_s
            self._emit(self._heading(to_twa), self.v)

    def bear_away(self, to_twa: float, seconds: float = 10):
        path = np.linspace(self.twa, to_twa, int(seconds) + 1)[1:]
        target = self.dn_speed if abs(to_twa) > 90 else self.up_speed
        for a in path:
            self.v += (target - self.v) * 0.3
            self._emit(self._heading(float(a)), self.v)
        self.twa = to_twa

    def fall(self, seconds: float):
        t_start = self.t
        wd = np.radians(self.wind_fn(self.t) + 180.0)
        drift = (0.3 * np.sin(wd), 0.3 * np.cos(wd))
        h = self._heading(self.twa)
        for _ in range(int(seconds)):
            h += self.rng.normal(0, 15)
            self._emit(h, 0.1, drift)
        self.v = 0.5
        self.truth.stops.append((t_start, self.t))

    # ---- output
    def track(self, lat0: float = 54.72, lon0: float = 18.41, spikes: int = 6,
              pos_noise: float = 1.0) -> tuple[Track, Truth]:
        arr = np.array(self.rows)
        t, x, y, v = arr.T
        # correlated GPS position error
        ex = np.cumsum(self.rng.normal(0, 0.3, len(t)))
        ey = np.cumsum(self.rng.normal(0, 0.3, len(t)))
        ex = (ex - np.convolve(ex, np.ones(30) / 30, mode="same")) * pos_noise
        ey = (ey - np.convolve(ey, np.ones(30) / 30, mode="same")) * pos_noise
        lat, lon = unproject(x + ex, y + ey, lat0, lon0)
        speed = v + self.rng.normal(0, 0.08, len(t))
        for i in self.rng.choice(np.arange(50, len(t) - 50), size=spikes, replace=False):
            speed[i] += self.rng.choice([-1, 1]) * self.rng.uniform(6, 12)
        speed = np.clip(speed, 0, None)
        hr = 140 + 25 * (v / max(v.max(), 1)) + self.rng.normal(0, 2, len(t))
        n = len(t)
        tr = Track(t=t, lat=lat, lon=lon, speed=speed, hr=hr, temp=np.full(n, 18.0),
                   hdop=np.full(n, np.nan), meta={"format": "synthetic", "sport_name": "Windfoiling",
                                                   "max_hr": 200})
        self.truth.wind = np.array(self.winds)
        return tr, self.truth


def course(wind_fn, seed: int = 0, laps: int = 4, tacks_per_beat: int = 4,
           gybes_per_run: int = 3, falls: bool = True, **kw) -> tuple[Track, Truth]:
    """A windward/leeward session: beats with tacks, runs with gybes."""
    sim = Sim(wind_fn, seed=seed, **{k: v for k, v in kw.items() if k not in ("spikes", "pos_noise")})
    rng = sim.rng
    up, dn = sim.up_twa, sim.dn_twa
    sim.sail(60, up, sim.up_speed)
    for lap in range(laps):
        side = 1 if sim.twa > 0 else -1
        for k in range(tacks_per_beat):
            sim.sail(rng.uniform(45, 110), side * up + rng.normal(0, 1.5), sim.up_speed)
            fail = falls and rng.random() < 0.25
            sim.turn(-side * up, "tack", turn_s=rng.uniform(5, 9),
                     v_min=rng.uniform(0.8, 2.0) if fail else rng.uniform(3.0, 4.5),
                     recover_s=12 if fail else 7)
            side = -side
        sim.sail(rng.uniform(45, 90), side * up, sim.up_speed)
        sim.bear_away(side * dn, 10)
        for k in range(gybes_per_run):
            sim.sail(rng.uniform(40, 90), side * dn + rng.normal(0, 2), sim.dn_speed)
            sim.turn(-side * dn, "gybe", turn_s=rng.uniform(4, 7),
                     v_min=rng.uniform(6.5, 8.5), recover_s=5)
            side = -side
        sim.sail(rng.uniform(40, 80), side * dn, sim.dn_speed)
        if falls and lap % 2 == 1:
            sim.fall(rng.uniform(25, 45))
            sim.sail(20, side * dn, sim.dn_speed)
        sim.bear_away(side * up, 10)  # head up onto the next beat
    sim.sail(60, sim.twa, sim.up_speed)
    return sim.track(**{k: v for k, v in kw.items() if k in ("spikes", "pos_noise")})


def reaching(wind_fn, seed: int = 0, legs: int = 12) -> tuple[Track, Truth]:
    """Back-and-forth beam reaching with gybes at each end (windsurf-style)."""
    sim = Sim(wind_fn, seed=seed, up_twa=95, dn_twa=100, up_speed=13, dn_speed=13)
    side = 1
    sim.twa = 95
    for _ in range(legs):
        sim.sail(sim.rng.uniform(60, 120), side * (95 + sim.rng.normal(0, 3)), 13)
        sim.turn(-side * 100, "gybe", turn_s=6, v_min=7, recover_s=6)
        side = -side
    sim.sail(60, sim.twa, 13)
    return sim.track()
