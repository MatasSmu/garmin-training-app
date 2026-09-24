"""The compact JSON the model sees for one session: headline numbers,
wind, gear, the athlete's note, the matching baseline with % differences,
port vs starboard and same-day recovery. Never any track points."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta

from app.sailing import baselines
from app.sailing.config import MATCH_PARTS, NOTE_MAX_CHARS, PACKET_MAX_TOKENS, PARTS, SPORTS


class NotEligible(ValueError):
    """The session can't be analysed (wrong sport, no wind, below 6 kn, ...)."""


def estimate_tokens(obj) -> int:
    # JSON full of short keys and numbers tokenises at roughly 3 chars/token
    # for DeepSeek's tokenizer; err on the high side.
    return math.ceil(len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False)) / 3)


def _r(v, nd=1):
    if v is None:
        return None
    return int(round(float(v))) if nd == 0 else round(float(v), nd)


def drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != {} and v != []}


def session_stats(s: dict) -> dict:
    return drop_none({
        "duration_min": _r((s.get("duration_s") or 0) / 60, 0),
        "moving_min": _r((s.get("moving_s") or 0) / 60, 0),
        "distance_km": _r((s.get("distance_m") or 0) / 1000, 1),
        "foiling_pct": _r(s.get("state_pct"), 0),
        "flights": s.get("flights"), "touchdowns": s.get("touchdowns"),
        "max_kn": _r(s.get("max_kn")), "best_10s_kn": _r(s.get("best_10s_kn")),
        "best_500m_kn": _r(s.get("best_500m_kn")), "avg_moving_kn": _r(s.get("avg_moving_kn")),
        "vmg_up_kn": _r(s.get("upwind_vmg_kn"), 2), "vmg_down_kn": _r(s.get("downwind_vmg_kn"), 2),
        "angle_up_deg": _r(s.get("upwind_twa"), 0), "angle_down_deg": _r(s.get("downwind_twa"), 0),
        "speed_up_kn": _r(s.get("upwind_kn")), "speed_down_kn": _r(s.get("downwind_kn")),
        "tacks": s.get("tacks"), "tack_foiling_pct": _r(s.get("tack_success_pct"), 0),
        "tack_loss_m": _r(s.get("tack_loss_m"), 0),
        "gybes": s.get("gybes"), "gybe_foiling_pct": _r(s.get("gybe_success_pct"), 0),
        "gybe_loss_m": _r(s.get("gybe_loss_m"), 0),
        "hr_avg": _r(s.get("hr_avg"), 0), "hr_max": _r(s.get("hr_max"), 0),
    })


def port_starboard(pos: dict | None) -> dict | None:
    """Upwind / downwind split by tack, only where both tacks have at least
    30 s of steady sailing. Port = wind over the left side."""
    if not pos:
        return None
    out = {}
    for leg, sign in (("upwind", 1), ("downwind", -1)):
        sides = pos.get(leg) or {}
        p, s = sides.get("port") or {}, sides.get("starboard") or {}
        if (p.get("time_s") or 0) < 30 or (s.get("time_s") or 0) < 30:
            continue

        def side(b):
            return drop_none({"vmg_kn": _r(sign * b["avg_vmg_kn"], 2) if b.get("avg_vmg_kn") is not None else None,
                               "angle_deg": _r(b.get("avg_twa"), 0), "speed_kn": _r(b.get("avg_kn")),
                               "foiling_pct": _r(b.get("state_pct"), 0), "min": _r(b["time_s"] / 60, 0)})
        ps, ss = side(p), side(s)
        diff = drop_none({k: _r(ps[k] - ss[k], 2) for k in ("vmg_kn", "angle_deg", "speed_kn") if k in ps and k in ss})
        out[leg] = {"port": ps, "starboard": ss, "port_minus_starboard": diff}
    return out or None


def recovery(conn: sqlite3.Connection, day: str) -> dict | None:
    d = date.fromisoformat(day)
    rows = {r["date"]: r for r in conn.execute(
        "SELECT date, hrv_last_night_avg, hrv_status, sleep_score, sleep_duration_s, body_battery_max "
        "FROM daily_metrics WHERE date BETWEEN ? AND ?", ((d - timedelta(days=6)).isoformat(), day))}
    today = rows.get(day)
    week = [r["hrv_last_night_avg"] for r in rows.values() if r["hrv_last_night_avg"] is not None]
    out = drop_none({
        "hrv": _r(today["hrv_last_night_avg"], 0) if today else None,
        "hrv_7d_avg": _r(sum(week) / len(week), 1) if week else None,
        "hrv_status": today["hrv_status"] if today else None,
        "sleep_score": today["sleep_score"] if today else None,
        "sleep_h": _r(today["sleep_duration_s"] / 3600, 1) if today and today["sleep_duration_s"] else None,
        "body_battery_max": today["body_battery_max"] if today else None,
    })
    return out or None


def track_gear_map(conn: sqlite3.Connection, track_id: int) -> dict[str, str]:
    """{part kind: code as entered (or name)} for the iQFOIL slots."""
    return {r["kind"]: r["code"] or r["name"] for r in conn.execute(
        "SELECT g.kind, g.code, g.name FROM track_gear tg JOIN gear g ON g.id = tg.gear_id WHERE tg.track_id = ?",
        (track_id,)) if r["kind"] in PARTS}


def eligibility(t: sqlite3.Row, point: dict | None) -> str | None:
    """Why a track can't be analysed, or None if it can."""
    if t["sport"] not in SPORTS:
        return "Only windfoil (iQFOIL) sessions are analysed."
    if point is None or not t["summary_json"]:
        return "This session hasn't been analysed yet."
    if point["wind_kn"] is None:
        return "No wind speed for this session -- fetch conditions or enter the wind manually."
    if baselines.wind_band(point["wind_kn"]) is None:
        return f"Average wind {point['wind_kn']:.1f} kn is below the lowest band -- not analysed."
    return None


def session_view(conn: sqlite3.Connection, track_id: int, points: list[dict] | None = None) -> dict:
    """Band, baseline and per-metric comparison for the session page (and
    the packet). Raises KeyError for an unknown track."""
    t = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if t is None:
        raise KeyError(track_id)
    points = points if points is not None else baselines.load_points(conn)
    point = baselines.point_for(conn, track_id, points)
    reason = eligibility(t, point)
    if reason:
        return {"eligible": False, "reason": reason, "band": None, "baseline": None, "comparison": None}
    base = baselines.baseline_for(points, point)
    return {"eligible": True, "reason": None, "band": base["band"], "wind_kn": point["wind_kn"],
            "match_parts": list(MATCH_PARTS), "baseline": base, "comparison": baselines.compare(point, base)}


def build(conn: sqlite3.Connection, track_id: int, *, point_of_sail: dict | None = None) -> dict:
    """The packet for one session. Raises NotEligible when it can't be
    analysed. point_of_sail comes from the cached engine result."""
    view = session_view(conn, track_id)
    if not view["eligible"]:
        raise NotEligible(view["reason"])
    t = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    summary = json.loads(t["summary_json"])
    cond = json.loads(t["conditions_json"]) if t["conditions_json"] else {}
    base = view["baseline"]
    note = (t["notes"] or "").strip()
    if len(note) > NOTE_MAX_CHARS:
        note = note[:NOTE_MAX_CHARS - 1].rstrip() + "…"
    packet = drop_none({
        "date": t["start_date"],
        "spot": t["spot"],
        "session": session_stats(summary),
        "wind": drop_none({
            "avg_kn": _r(cond.get("wind_kn")), "gust_kn": _r(cond.get("gust_kn")),
            "from_deg": _r(summary.get("wind_deg") if summary.get("wind_deg") is not None else cond.get("wind_dir"), 0),
            "band_kn": view["band"],
            "source": "manual" if cond.get("source") == "manual" else "forecast model",
        }),
        "gear": {k: " ".join(v.split()) for k, v in track_gear_map(conn, track_id).items()} or None,
        "note": note or None,
        "baseline": drop_none({
            "level": base["level"], "gear_matched_on": base["gear_label"] if base["level"] == "band+gear" else None,
            "fallback": base["fallback"], "fallback_reason": base["fallback_reason"],
            "sessions": base["sessions"], "rolling_avg_of_last": base["rolling_n"],
            "best": drop_none(base["best"]), "rolling_avg": drop_none(base["rolling_avg"]),
        }),
        "vs_rolling_avg_pct": drop_none({m: c["pct_vs_avg"] for m, c in view["comparison"].items()}),
        "new_personal_bests": [m for m, c in view["comparison"].items() if c["new_best"]],
        "port_vs_starboard": port_starboard(point_of_sail),
        "recovery": recovery(conn, t["start_date"]),
    })
    return fit(packet)


# Least important first: dropped one by one while the packet is over budget.
_TRIM_ORDER = (("session", "hr_max"), ("session", "flights"), ("session", "best_10s_kn"),
               ("session", "speed_up_kn"), ("session", "speed_down_kn"), ("recovery", "body_battery_max"),
               ("recovery", "sleep_h"), ("port_vs_starboard", None))


def fit(packet: dict, max_tokens: int = PACKET_MAX_TOKENS) -> dict:
    """Shrink the packet until it fits the token budget: shorten the note
    first, then drop the least useful fields."""
    for n in (400, 200):
        note = packet.get("note") or ""
        if estimate_tokens(packet) > max_tokens and len(note) > n:
            packet["note"] = note[:n - 1].rstrip() + "…"
    for section, key in _TRIM_ORDER:
        if estimate_tokens(packet) <= max_tokens:
            break
        if key is None:
            packet.pop(section, None)
        elif isinstance(packet.get(section), dict):
            packet[section].pop(key, None)
    return packet
