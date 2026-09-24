"""Parse FIT / GPX files into a plain columnar Track.

Only what the engine needs is kept: time, position, device (Doppler) speed,
heart rate, temperature and -- when a file carries it -- HDOP. File metadata
the rest of the app cares about (sport name, local UTC offset, max HR, the
wind direction a Connect IQ field was set to) goes into `meta`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SEMI = 180.0 / 2**31


@dataclass
class Track:
    t: np.ndarray                 # epoch seconds (UTC), strictly increasing
    lat: np.ndarray
    lon: np.ndarray
    speed: np.ndarray             # device speed m/s (NaN where missing)
    hr: np.ndarray
    temp: np.ndarray
    hdop: np.ndarray
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.t)


def _epoch(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _finish(rows: dict[str, list], meta: dict) -> Track:
    t = np.asarray(rows["t"], dtype=float)
    order = np.argsort(t, kind="stable")
    cols = {k: np.asarray(v, dtype=float)[order] for k, v in rows.items()}
    # Duplicate timestamps: keep the last record for each second.
    t = cols["t"]
    keep = np.ones(len(t), dtype=bool)
    if len(t) > 1:
        keep[:-1] = np.diff(t) > 0
    cols = {k: v[keep] for k, v in cols.items()}
    # Positions are required for everything downstream.
    ok = np.isfinite(cols["lat"]) & np.isfinite(cols["lon"])
    ok &= (np.abs(cols["lat"]) > 1e-6) | (np.abs(cols["lon"]) > 1e-6)
    meta["records_total"] = int(len(cols["t"]))
    meta["records_without_fix"] = int((~ok).sum())
    cols = {k: v[ok] for k, v in cols.items()}
    return Track(
        t=cols["t"], lat=cols["lat"], lon=cols["lon"], speed=cols["speed"],
        hr=cols["hr"], temp=cols["temp"], hdop=cols["hdop"], meta=meta,
    )


def load_fit(data: bytes) -> Track:
    import fitparse

    ff = fitparse.FitFile(io.BytesIO(data), check_crc=False)
    rows: dict[str, list] = {k: [] for k in ("t", "lat", "lon", "speed", "hr", "temp", "hdop")}
    meta: dict = {"format": "fit"}
    dev_wind: list[float] = []
    for msg in ff.get_messages():
        name = msg.name
        if name == "record":
            d = {f.name: f.value for f in msg.fields}
            ts = d.get("timestamp")
            if ts is None:
                continue
            lat, lon = d.get("position_lat"), d.get("position_long")
            spd = d.get("enhanced_speed", d.get("speed"))
            rows["t"].append(_epoch(ts))
            rows["lat"].append(lat * SEMI if lat is not None else np.nan)
            rows["lon"].append(lon * SEMI if lon is not None else np.nan)
            rows["speed"].append(float(spd) if spd is not None else np.nan)
            hr = d.get("heart_rate")
            rows["hr"].append(float(hr) if hr else np.nan)
            tmp = d.get("temperature")
            rows["temp"].append(float(tmp) if tmp is not None else np.nan)
            acc = d.get("gps_accuracy")
            # FIT has no HDOP; gps_accuracy (m) / ~5 m per HDOP unit is the usual proxy.
            rows["hdop"].append(float(acc) / 5.0 if acc is not None else np.nan)
            wd = d.get("WindDirection")
            if wd is not None:
                dev_wind.append(float(wd))
        elif name == "sport":
            d = {f.name: f.value for f in msg.fields}
            meta["sport_name"] = d.get("name") or meta.get("sport_name")
            meta["fit_sport"] = str(d.get("sport"))
        elif name == "session":
            d = {f.name: f.value for f in msg.fields}
            meta.setdefault("fit_sport", str(d.get("sport")))
            if d.get("start_time"):
                meta["start_utc"] = _epoch(d["start_time"])
        elif name == "activity":
            d = {f.name: f.value for f in msg.fields}
            if d.get("local_timestamp") and d.get("timestamp"):
                off = (d["local_timestamp"] - d["timestamp"]).total_seconds()
                meta["utc_offset_s"] = int(round(off / 900.0) * 900)
        elif name == "zones_target":
            d = {f.name: f.value for f in msg.fields}
            if d.get("max_heart_rate"):
                meta["max_hr"] = int(d["max_heart_rate"])
        elif name == "user_profile":
            d = {f.name: f.value for f in msg.fields}
            if d.get("resting_heart_rate"):
                meta["rest_hr"] = int(d["resting_heart_rate"])
    if dev_wind:
        vals, counts = np.unique(np.round(dev_wind), return_counts=True)
        meta["device_wind_deg"] = float(vals[np.argmax(counts)] % 360)
    return _finish(rows, meta)


def load_gpx(data: bytes) -> Track:
    import gpxpy

    g = gpxpy.parse(data.decode("utf-8", errors="replace"))
    rows: dict[str, list] = {k: [] for k in ("t", "lat", "lon", "speed", "hr", "temp", "hdop")}
    meta: dict = {"format": "gpx"}
    for trk in g.tracks:
        if trk.name and "sport_name" not in meta:
            meta["sport_name"] = trk.name
        for seg in trk.segments:
            for p in seg.points:
                if p.time is None:
                    continue
                hr = temp = np.nan
                for ext in p.extensions:
                    for child in ext.iter():
                        tag = child.tag.rsplit("}", 1)[-1]
                        if tag == "hr" and child.text:
                            hr = float(child.text)
                        elif tag in ("atemp", "wtemp") and child.text:
                            temp = float(child.text)
                rows["t"].append(_epoch(p.time))
                rows["lat"].append(p.latitude)
                rows["lon"].append(p.longitude)
                rows["speed"].append(float(p.speed) if p.speed is not None else np.nan)
                rows["hr"].append(hr)
                rows["temp"].append(temp)
                rows["hdop"].append(float(p.horizontal_dilution) if p.horizontal_dilution is not None else np.nan)
    return _finish(rows, meta)


def load_bytes(data: bytes, filename: str) -> Track:
    ext = Path(filename).suffix.lower()
    if ext == ".fit":
        tr = load_fit(data)
    elif ext == ".gpx":
        tr = load_gpx(data)
    else:
        raise ValueError(f"Unsupported file type {ext!r} (use .fit or .gpx)")
    tr.meta["filename"] = Path(filename).name
    if len(tr) < 30:
        raise ValueError("Track has fewer than 30 GPS fixes -- nothing to analyse")
    return tr


def load_file(path: str | Path) -> Track:
    p = Path(path)
    return load_bytes(p.read_bytes(), p.name)
