"""Wind-band baselines: personal bests and rolling averages per wind band
and per band + gear combo. Pure, deterministic functions over "points"
(one per session) plus rebuild(), which recomputes the stored table.

A point is {"id", "start_utc", "wind_kn", "gear": {kind: code}, "metrics": {...}}.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.sailing.config import MATCH_PARTS, METRICS, MIN_COMBO_SESSIONS, ROLLING_N, SPORTS, WIND_BANDS
from app.sailing.gear import gear_key, gear_label


def band_label(lo: float, hi: float | None) -> str:
    return f"{lo:g}+" if hi is None else f"{lo:g}-{hi:g}"


BAND_LABELS = [band_label(lo, hi) for lo, hi in WIND_BANDS]


def wind_band(wind_kn: float | None, bands=WIND_BANDS) -> str | None:
    """Band label for an average wind, or None if it's unknown or below the
    lowest band (excluded from baselines and analysis)."""
    if wind_kn is None:
        return None
    for lo, hi in bands:
        if wind_kn >= lo and (hi is None or wind_kn < hi):
            return band_label(lo, hi)
    return None


def session_metrics(summary: dict | None) -> dict:
    s = summary or {}
    out = {}
    for m, (field, _, convert) in METRICS.items():
        v = float(s[field]) if s.get(field) is not None else None
        out[m] = convert(v) if convert and v is not None else v
    return out


def pct_diff(value: float | None, ref: float | None) -> float | None:
    if value is None or ref is None or ref == 0:
        return None
    return round((value - ref) / abs(ref) * 100, 1)


def group_stats(points: list[dict], rolling_n: int = ROLLING_N) -> dict:
    """Bests over every session in the group, averages over the latest
    rolling_n sessions (a metric a session lacks is skipped, not zero)."""
    pts = sorted(points, key=lambda p: p["start_utc"])
    recent = pts[-rolling_n:]
    best, rolling = {}, {}
    for m, (_, higher, _) in METRICS.items():
        vals = [p["metrics"][m] for p in pts if p["metrics"].get(m) is not None]
        best[m] = (max(vals) if higher else min(vals)) if vals else None
        rv = [p["metrics"][m] for p in recent if p["metrics"].get(m) is not None]
        rolling[m] = round(sum(rv) / len(rv), 2) if rv else None
    return {"sessions": len(pts), "rolling_n": len(recent), "best": best, "rolling_avg": rolling}


def _in_band(points: list[dict], band: str) -> list[dict]:
    return [p for p in points if wind_band(p["wind_kn"]) == band]


def compute_all(points: list[dict], parts: tuple[str, ...] | None = None) -> list[dict]:
    """Every band baseline (gear_key '') and every band + gear combo, in a
    stable order. Combos below MIN_COMBO_SESSIONS are kept with
    enough_data=False so the gear page can show them growing."""
    parts = parts or MATCH_PARTS
    out = []
    for band in BAND_LABELS:
        pts = _in_band(points, band)
        if not pts:
            continue
        out.append({"band": band, "gear_key": "", "gear_label": None, "enough_data": True, **group_stats(pts)})
        combos: dict[str, list[dict]] = {}
        for p in pts:
            k = gear_key(p["gear"], parts)
            if k:
                combos.setdefault(k, []).append(p)
        for k in sorted(combos):
            grp = combos[k]
            latest = max(grp, key=lambda p: p["start_utc"])
            out.append({"band": band, "gear_key": k, "gear_label": gear_label(latest["gear"], parts),
                        "enough_data": len(grp) >= MIN_COMBO_SESSIONS, **group_stats(grp)})
    return out


def baseline_for(points: list[dict], session: dict, parts: tuple[str, ...] | None = None) -> dict | None:
    """The baseline a session is judged against: its band + gear combo when
    there are at least MIN_COMBO_SESSIONS earlier sessions with that gear,
    otherwise the band baseline, flagged as a fallback. Only sessions that
    started before this one count, so re-analysing an old session gives the
    same answer it would have got at the time. None if the session itself
    is outside every band."""
    band = wind_band(session["wind_kn"])
    if band is None:
        return None
    parts = parts or MATCH_PARTS
    pool = [p for p in _in_band(points, band)
            if p["id"] != session["id"] and p["start_utc"] < session["start_utc"]]
    key = gear_key(session["gear"], parts)
    combo = [p for p in pool if key and gear_key(p["gear"], parts) == key]
    if key and len(combo) >= MIN_COMBO_SESSIONS:
        return {"band": band, "level": "band+gear", "gear_key": key, "gear_label": gear_label(session["gear"], parts),
                "fallback": False, "fallback_reason": None, "combo_sessions": len(combo), **group_stats(combo)}
    if not key:
        reason = "gear not tagged (" + " + ".join(p.replace("_", " ") for p in parts) + " needed)"
    else:
        reason = f"only {len(combo)} earlier session(s) with this gear in {band} kn (need {MIN_COMBO_SESSIONS})"
    return {"band": band, "level": "band", "gear_key": key, "gear_label": gear_label(session["gear"], parts) if key else None,
            "fallback": True, "fallback_reason": reason, "combo_sessions": len(combo), **group_stats(pool)}


def compare(session: dict, baseline: dict | None) -> dict:
    """Per metric: this session's value, the baseline's rolling average and
    best, the % difference vs the rolling average, whether that difference is
    an improvement, and whether it's a new personal best."""
    out = {}
    for m, (_, higher, _) in METRICS.items():
        v = session["metrics"].get(m)
        avg = baseline["rolling_avg"].get(m) if baseline else None
        best = baseline["best"].get(m) if baseline else None
        d = pct_diff(v, avg)
        out[m] = {
            "value": v, "rolling_avg": avg, "best": best, "pct_vs_avg": d,
            "better": None if d is None or d == 0 else (d > 0) == higher,
            "new_best": v is not None and best is not None and (v > best if higher else v < best),
        }
    return out


# ------------------------------------------------------------------ DB
def load_points(conn: sqlite3.Connection) -> list[dict]:
    """One point per analysed session of the baseline sports, oldest first."""
    q = ",".join("?" for _ in SPORTS)
    rows = conn.execute(
        f"SELECT id, start_utc, conditions_json, summary_json FROM tracks "
        f"WHERE sport IN ({q}) AND summary_json IS NOT NULL ORDER BY start_utc", SPORTS).fetchall()
    gear: dict[int, dict[str, str]] = {}
    for g in conn.execute("SELECT tg.track_id, g.kind, g.code, g.name FROM track_gear tg JOIN gear g ON g.id = tg.gear_id"):
        gear.setdefault(g["track_id"], {})[g["kind"]] = g["code"] or g["name"]
    out = []
    for r in rows:
        cond = json.loads(r["conditions_json"]) if r["conditions_json"] else {}
        out.append({"id": r["id"], "start_utc": r["start_utc"], "wind_kn": (cond or {}).get("wind_kn"),
                    "gear": gear.get(r["id"], {}), "metrics": session_metrics(json.loads(r["summary_json"]))})
    return out


def point_for(conn: sqlite3.Connection, track_id: int, points: list[dict] | None = None) -> dict | None:
    return next((p for p in (points if points is not None else load_points(conn)) if p["id"] == track_id), None)


def rebuild(conn: sqlite3.Connection) -> int:
    """Recompute the stored baselines from scratch. Cheap (one pass over the
    summaries) and deterministic, so it simply runs after every change."""
    rows = compute_all(load_points(conn))
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM sail_baselines")
    for b in rows:
        conn.execute(
            "INSERT INTO sail_baselines (band, gear_key, gear_label, sessions, rolling_n, enough_data, best_json, "
            "rolling_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (b["band"], b["gear_key"], b["gear_label"], b["sessions"], b["rolling_n"], int(b["enough_data"]),
             json.dumps(b["best"]), json.dumps(b["rolling_avg"]), now))
    conn.commit()
    return len(rows)


def stored(conn: sqlite3.Connection) -> list[dict]:
    order = {b: i for i, b in enumerate(BAND_LABELS)}
    rows = [{"band": r["band"], "gear_key": r["gear_key"], "gear_label": r["gear_label"], "sessions": r["sessions"],
             "rolling_n": r["rolling_n"], "enough_data": bool(r["enough_data"]), "best": json.loads(r["best_json"]),
             "rolling_avg": json.loads(r["rolling_json"]), "updated_at": r["updated_at"]}
            for r in conn.execute("SELECT * FROM sail_baselines")]
    return sorted(rows, key=lambda b: (order.get(b["band"], 99), b["gear_key"]))
