"""Stored DeepSeek outputs: one analysis per session, weekly summaries built
from those, and month/season reviews built from the weekly summaries --
each level only ever reads the level below it, never raw data.

Cost rules: an analysis is stored with its session and never re-run
automatically. The only automatic call is the first analysis of a session,
triggered once when gear is first saved on it (see maybe_auto_analyze), and
the weekly summary of a finished week, generated once by the Garmin sync.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

from app import config
from app.db import get_connection
from app.sailing import baselines, deepseek, packet, prompts
from app.sailing.config import MATCH_PARTS, MIN_COMBO_SESSIONS, ROLLING_N, SPORTS, WIND_BANDS

log = logging.getLogger(__name__)

CONNECT = get_connection   # the background runner opens its own connection; tests swap this

_lock = threading.Lock()
_running: set[int] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(s: str | None):
    return json.loads(s) if s else None


# ------------------------------------------------------------------ session analyses
def inputs_sig(conn: sqlite3.Connection, track_id: int) -> str:
    """What the athlete can change after the fact that the analysis depends on."""
    t = conn.execute("SELECT notes, conditions_json FROM tracks WHERE id = ?", (track_id,)).fetchone()
    cond = _json(t["conditions_json"]) or {}
    raw = json.dumps([sorted(packet.track_gear_map(conn, track_id).items()), (t["notes"] or "").strip(),
                      cond.get("wind_kn")])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def analyze_session(conn: sqlite3.Connection, track_id: int) -> dict:
    """Build the packet, call DeepSeek and store the result, replacing any
    earlier analysis only on success. Raises packet.NotEligible or
    deepseek.LLMError."""
    from app.analysis import service  # late: service imports app.sailing

    result = service.get_analysis(conn, track_id)  # cached engine result -> port/starboard split
    pk = packet.build(conn, track_id, point_of_sail=result.get("point_of_sail"))
    data, usage = deepseek.call_json(
        conn, system=prompts.system_prompt(prompts.SESSION_TASK),
        user="Session packet (JSON):\n" + json.dumps(pk, separators=(",", ":"), ensure_ascii=False),
        schema=deepseek.SessionAnalysis, purpose="session", ref=track_id)
    conn.execute(
        "INSERT INTO sail_session_analysis (track_id, packet_json, result_json, inputs_sig, model, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(track_id) DO UPDATE SET packet_json = excluded.packet_json, "
        "result_json = excluded.result_json, inputs_sig = excluded.inputs_sig, model = excluded.model, "
        "created_at = excluded.created_at",
        (track_id, json.dumps(pk), json.dumps(data), inputs_sig(conn, track_id), usage["model"], _now()))
    conn.commit()
    return stored_analysis(conn, track_id)


def stored_analysis(conn: sqlite3.Connection, track_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM sail_session_analysis WHERE track_id = ?", (track_id,)).fetchone()
    if r is None:
        return None
    u = conn.execute("SELECT * FROM llm_usage WHERE purpose = 'session' AND ref = ? AND ok = 1 "
                     "ORDER BY id DESC LIMIT 1", (str(track_id),)).fetchone()
    return {"result": _json(r["result_json"]), "packet": _json(r["packet_json"]), "model": r["model"],
            "created_at": r["created_at"], "stale": r["inputs_sig"] != inputs_sig(conn, track_id),
            "usage": dict(u) if u else None,
            "packet_tokens": packet.estimate_tokens(_json(r["packet_json"]))}


def attempted(conn: sqlite3.Connection, track_id: int) -> bool:
    return conn.execute("SELECT 1 FROM llm_usage WHERE purpose = 'session' AND ref = ? LIMIT 1",
                        (str(track_id),)).fetchone() is not None


def is_running(track_id: int) -> bool:
    with _lock:
        return track_id in _running


def claim(track_id: int) -> bool:
    """Mark a session as being analysed; False if it already is."""
    with _lock:
        if track_id in _running:
            return False
        _running.add(track_id)
        return True


def release(track_id: int) -> None:
    with _lock:
        _running.discard(track_id)


def maybe_auto_analyze(conn: sqlite3.Connection, track_id: int) -> bool:
    """First analysis of a session, once, in the background: only when a key
    is set, the session is eligible, has its matching gear tagged, and has
    never been sent to the model before (not even a failed attempt)."""
    if not config.DEEPSEEK_API_KEY or attempted(conn, track_id) or stored_analysis(conn, track_id):
        return False
    if packet.session_view(conn, track_id)["eligible"] is False:
        return False
    gear = packet.track_gear_map(conn, track_id)
    if not all(gear.get(p) for p in MATCH_PARTS):
        return False
    if not claim(track_id):
        return False

    def run():
        c = CONNECT()
        try:
            analyze_session(c, track_id)
        except (packet.NotEligible, deepseek.LLMError) as e:
            log.warning("auto analysis of track %s failed: %s", track_id, e)
        except Exception:  # noqa: BLE001 -- a background thread must not die silently
            log.exception("auto analysis of track %s crashed", track_id)
        finally:
            c.close()
            release(track_id)

    threading.Thread(target=run, name=f"sail-analysis-{track_id}", daemon=True).start()
    return True


def session_state(conn: sqlite3.Connection, track_id: int) -> dict:
    """Everything the session page's analysis panel shows."""
    view = packet.session_view(conn, track_id)
    return {**view, "analysis": stored_analysis(conn, track_id), "running": is_running(track_id),
            "ai_enabled": bool(config.DEEPSEEK_API_KEY)}


def _track_of_activity(conn: sqlite3.Connection, activity_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tracks WHERE activity_id = ? AND summary_json IS NOT NULL "
                        "ORDER BY start_utc LIMIT 1", (activity_id,)).fetchone()


def _cached_result(conn: sqlite3.Connection, track_id: int) -> dict | None:
    """The stored engine result as is (even if settings changed since) --
    reading it never re-runs the engine."""
    r = conn.execute("SELECT result_gz FROM track_analysis WHERE track_id = ?", (track_id,)).fetchone()
    return json.loads(gzip.decompress(r["result_gz"])) if r else None


def _table(columns: list[str], rows: list[list]) -> dict:
    """Column names once, then one short array per row -- about a third of
    the tokens of a list of dicts."""
    return {"columns": columns, "rows": rows}


def track_detail(conn: sqlite3.Connection, activity_id: int) -> dict | None:
    """Everything the GPS analysis found in one session, for the coach chat:
    every tack and gybe, every leg, every flight, race legs, best splits,
    point of sail by tack, targets, polar bests, wind and HR. Times are the
    athlete's local clock. None if the activity has no analysed track."""
    t = _track_of_activity(conn, activity_id)
    r = _cached_result(conn, t["id"]) if t else None
    if r is None:
        return None
    off = t["utc_offset_s"] or 0
    rnd = packet.rnd

    def at(epoch):
        return None if epoch is None else datetime.fromtimestamp(epoch + off, timezone.utc).strftime("%H:%M:%S")

    maneuvers = _table(
        ["n", "at", "type", "sailing", "from_tack", "duration_s", "entry_kn", "min_kn", "exit_kn", "entry_twa",
         "exit_twa", "turn_deg", "lost_m", "stayed_on_foil", "recovery_s", "hr_max"],
        [[m.get("id"), at(m.get("tc")), m["type"], m.get("direction"), m.get("from_side"), rnd(m.get("duration_s"), 0),
          rnd(m.get("entry_kn")), rnd(m.get("min_kn")), rnd(m.get("exit_kn")), rnd(m.get("entry_twa"), 0),
          rnd(m.get("exit_twa"), 0), rnd(m.get("turn_deg"), 0), rnd(m.get("distance_lost_m"), 0),
          m.get("success"), rnd(m.get("recovery_s"), 0), rnd(m.get("hr_max"), 0)] for m in r["maneuvers"]])
    legs = _table(
        ["n", "start", "type", "tack", "duration_s", "distance_m", "avg_kn", "max_kn", "twa", "vmg_kn", "gain_m",
         "foiling_pct", "pct_target_speed", "pct_target_vmg", "gain_vs_target_m", "deg_off_best_vmg_angle", "hr_avg"],
        [[l.get("id"), at(l.get("t0")), l["type"], l.get("side"), rnd(l.get("duration_s"), 0), rnd(l.get("distance_m"), 0),
          rnd(l.get("avg_kn")), rnd(l.get("max_kn")), rnd(l.get("avg_twa"), 0), rnd(l.get("avg_vmg_kn"), 2),
          rnd(l.get("gain_m"), 0), rnd(l.get("state_pct"), 0), rnd(l.get("pct_target_speed"), 0),
          rnd(l.get("pct_target_vmg"), 0), rnd(l.get("gain_vs_target_m"), 0), rnd(l.get("angle_vs_ref"), 0),
          rnd(l.get("hr_avg"), 0)] for l in r["legs"]])
    st = r.get("state") or {}
    flights = {
        "summary": packet.drop_none({"count": st.get("flights"), "touchdowns": st.get("touchdowns"),
                                     "pct_of_moving": rnd(st.get("pct_of_moving"), 0), "longest_s": rnd(st.get("longest_s"), 0),
                                     "longest_m": rnd(st.get("longest_m"), 0), "avg_s": rnd(st.get("avg_flight_s"), 0),
                                     "on_kn": st.get("on_kn"), "off_kn": st.get("off_kn")}),
        **_table(["start", "duration_s", "distance_m", "max_kn"],
                 [[at(f.get("t0")), rnd(f.get("duration_s"), 0), rnd(f.get("distance_m"), 0), rnd(f.get("max_kn"))]
                  for f in r.get("flights") or []]),
    }
    pos = r.get("point_of_sail") or {}
    point_of_sail = _table(
        ["leg_type", "tack", "minutes", "avg_kn", "max_kn", "vmg_kn", "twa", "foiling_pct"],
        [[k, side, rnd((b.get("time_s") or 0) / 60, 1), rnd(b.get("avg_kn")), rnd(b.get("max_kn")),
          rnd(b.get("avg_vmg_kn"), 2), rnd(b.get("avg_twa"), 0), rnd(b.get("state_pct"), 0)]
         for k in ("upwind", "reach", "downwind", "all") for side in ("port", "starboard", "all")
         if (b := (pos.get(k) or {}).get(side)) and b.get("time_s")])
    race = r.get("race") or {}
    race_legs = _table(
        ["from", "to", "type", "duration_s", "avg_kn", "vmg_up_kn", "vmg_down_kn", "gain_m", "tacks", "gybes", "foiling_pct"],
        [[g.get("from"), g.get("to"), g.get("type"), rnd(g.get("duration_s"), 0), rnd(g.get("avg_kn")),
          rnd(g.get("avg_vmg_up_kn"), 2), rnd(g.get("avg_vmg_down_kn"), 2), rnd(g.get("gain_m"), 0),
          g.get("tacks"), g.get("gybes"), rnd(g.get("state_pct"), 0)] for g in race.get("legs") or []])
    hr = r.get("hr") or {}
    w = r.get("wind") or {}

    def best(p):
        return {k: packet.drop_none({kk: rnd(v, 2 if kk == "vmg_kn" else 1) for kk, v in (p.get(k) or {}).items()})
                for k in ("best_upwind", "best_downwind")} if p else None

    return packet.drop_none({
        "track_id": t["id"], "sport": t["sport"],
        "notes": "Times are local. TWA in degrees off the wind; VMG positive = towards the wind on upwind legs. "
                 "sailing = what the athlete was doing when turning (rounding = turning from upwind to downwind or back, "
                 "no loss figure). lost_m = metres lost to windward vs sailing straight on. stayed_on_foil = no touchdown.",
        "maneuvers": maneuvers,
        "legs": legs,
        "flights": flights,
        "best_splits": {s["label"]: [rnd(s.get("kn")), at(s.get("t0"))] for s in r.get("splits") or [] if s.get("kn")},
        "point_of_sail": point_of_sail,
        "targets": packet.drop_none({k: rnd(v, 1) if isinstance(v, float) else v for k, v in (r.get("targets") or {}).items()}),
        "session_polar_best": best(r.get("polar")),
        "personal_polar_best": best(r.get("personal_polar")),
        "wind": packet.drop_none({"from_deg": rnd(w.get("deg"), 0), "confidence": w.get("confidence"),
                                  "source": w.get("source")}),
        "race": packet.drop_none({"start": at(race.get("start")), "marks": race.get("marks_mode"),
                                  "legs": race_legs if race_legs["rows"] else None}),
        "heart_rate": packet.drop_none({
            "avg": hr.get("avg"), "max": hr.get("max"), "sailing_avg": hr.get("sailing_avg"),
            "maneuver_avg": hr.get("maneuver_avg"), "tack_peak_avg": hr.get("tack_peak_avg"),
            "gybe_peak_avg": hr.get("gybe_peak_avg"),
            "zone_minutes": {f"z{z['zone']}": rnd(z["time_s"] / 60, 1) for z in hr.get("zones") or []} or None,
            "by_speed_kn": {str(b["kn"]): rnd(b["hr"], 0) for b in hr.get("by_speed") or []} or None,
        }) or None,
        "warnings": r.get("warnings") or None,
    })


def coach_session_view(conn: sqlite3.Connection, activity_id: int) -> dict | None:
    """The GPS analysis of a Garmin activity for the coach chat: tacks and
    gybes, VMG, angles, port vs starboard, the wind-band baseline comparison
    and the stored session analysis. None if the activity has no analysed
    track. Reads the cached engine result as is -- never re-runs anything."""
    t = _track_of_activity(conn, activity_id)
    if t is None:
        return None
    summary = json.loads(t["summary_json"])
    cond = _json(t["conditions_json"])
    pos = (_cached_result(conn, t["id"]) or {}).get("point_of_sail")
    stats = packet.session_stats(summary)
    stats.update(packet.drop_none({"tack_min_kn": packet.rnd(summary.get("tack_min_kn")),
                                   "gybe_min_kn": packet.rnd(summary.get("gybe_min_kn"))}))
    out = {"track_id": t["id"], "sport": t["sport"], "stats": stats,
           "wind": packet.wind_info(summary, cond), "gear": packet.gear_info(conn, t["id"]),
           "port_vs_starboard": packet.port_starboard(pos)}
    view = packet.session_view(conn, t["id"])
    if view["eligible"]:
        base = view["baseline"]
        out["vs_baseline"] = packet.drop_none({
            "level": base["level"], "fallback_reason": base["fallback_reason"], "earlier_sessions": base["sessions"],
            "rolling_avg": packet.drop_none(base["rolling_avg"]), "best": packet.drop_none(base["best"]),
            "pct_vs_rolling_avg": packet.drop_none({m: c["pct_vs_avg"] for m, c in view["comparison"].items()}),
            "new_personal_bests": [m for m, c in view["comparison"].items() if c["new_best"]],
        })
    elif t["sport"] in SPORTS:
        out["vs_baseline"] = {"unavailable": view["reason"]}
    stored = stored_analysis(conn, t["id"])
    if stored:
        out["coach_analysis"] = {**{k: v for k, v in stored["result"].items() if k != "vs_baseline"},
                                 "written": stored["created_at"][:10], "out_of_date": stored["stale"]}
    return packet.drop_none(out)


# ------------------------------------------------------------------ weekly summaries
def week_of(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def _week_entries(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = conn.execute(
        "SELECT t.id, t.start_date, t.spot, a.packet_json, a.result_json FROM sail_session_analysis a "
        "JOIN tracks t ON t.id = a.track_id WHERE t.start_date BETWEEN ? AND ? ORDER BY t.start_utc",
        (start, end)).fetchall()
    out = []
    for r in rows:
        pk, res = _json(r["packet_json"]), _json(r["result_json"])
        gear = pk.get("gear") or {}
        out.append(packet.drop_none({
            "track_id": r["id"], "date": r["start_date"], "spot": r["spot"],
            "wind_kn": (pk.get("wind") or {}).get("avg_kn"), "band_kn": (pk.get("wind") or {}).get("band_kn"),
            "gear": {p: gear[p] for p in MATCH_PARTS if gear.get(p)} or None,
            "baseline_level": (pk.get("baseline") or {}).get("level"),
            "vs_rolling_avg_pct": pk.get("vs_rolling_avg_pct"),
            "new_personal_bests": pk.get("new_personal_bests"),
            "recovery": pk.get("recovery"),
            **{k: res.get(k) for k in ("summary", "strengths", "weaknesses", "possible_causes",
                                       "focus_next_session", "plan_adjustment")},
        }))
    return out


def generate_weekly(conn: sqlite3.Connection, week_start: date) -> dict:
    start, end = week_of(week_start)
    entries = _week_entries(conn, start.isoformat(), end.isoformat())
    if not entries:
        raise packet.NotEligible(f"No analysed sessions in the week of {start.isoformat()}.")
    user = json.dumps({"week": f"{start.isoformat()} to {end.isoformat()}",
                       "sessions": [{k: v for k, v in e.items() if k != "track_id"} for e in entries]},
                      separators=(",", ":"), ensure_ascii=False)
    data, usage = deepseek.call_json(conn, system=prompts.system_prompt(prompts.WEEKLY_TASK),
                                     user="Week of session analyses (JSON):\n" + user,
                                     schema=deepseek.WeeklySummary, purpose="weekly", ref=start.isoformat())
    conn.execute(
        "INSERT INTO sail_weekly_summaries (week_start, week_end, track_ids, result_json, model, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(week_start) DO UPDATE SET week_end = excluded.week_end, "
        "track_ids = excluded.track_ids, result_json = excluded.result_json, model = excluded.model, "
        "created_at = excluded.created_at",
        (start.isoformat(), end.isoformat(), json.dumps([e["track_id"] for e in entries]), json.dumps(data),
         usage["model"], _now()))
    conn.commit()
    return next(w for w in list_weekly(conn) if w["week_start"] == start.isoformat())


def list_weekly(conn: sqlite3.Connection) -> list[dict]:
    return [{"week_start": r["week_start"], "week_end": r["week_end"], "track_ids": _json(r["track_ids"]),
             "result": _json(r["result_json"]), "model": r["model"], "created_at": r["created_at"]}
            for r in conn.execute("SELECT * FROM sail_weekly_summaries ORDER BY week_start DESC")]


def ensure_last_week(conn: sqlite3.Connection, today: date | None = None) -> dict | None:
    """Called by the Garmin sync: summarise the last finished week once, if
    it has analysed sessions and no summary yet. Never raises."""
    if not config.DEEPSEEK_API_KEY:
        return None
    start, end = week_of((today or date.today()) - timedelta(days=7))
    if conn.execute("SELECT 1 FROM sail_weekly_summaries WHERE week_start = ?", (start.isoformat(),)).fetchone():
        return None
    if conn.execute("SELECT 1 FROM llm_usage WHERE purpose = 'weekly' AND ref = ?", (start.isoformat(),)).fetchone():
        return None  # tried before and failed: leave it to the manual button
    if not _week_entries(conn, start.isoformat(), end.isoformat()):
        return None
    try:
        return generate_weekly(conn, start)
    except (packet.NotEligible, deepseek.LLMError) as e:
        log.warning("weekly summary for %s failed: %s", start, e)
        return None


# ------------------------------------------------------------------ month / season reviews
def review_period(kind: str, day: date) -> tuple[date, date]:
    if kind == "month":
        start = day.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return start, end
    return date(day.year, 1, 1), date(day.year, 12, 31)


def generate_review(conn: sqlite3.Connection, kind: str, start: date, end: date) -> dict:
    weeks = [w for w in reversed(list_weekly(conn)) if start.isoformat() <= w["week_start"] <= end.isoformat()]
    if not weeks:
        raise packet.NotEligible("No weekly summaries in that period yet -- generate those first.")
    user = json.dumps({"period": f"{kind} {start.isoformat()} to {end.isoformat()}",
                       "weeks": [{"week": w["week_start"], "sessions": len(w["track_ids"]), **w["result"]} for w in weeks]},
                      separators=(",", ":"), ensure_ascii=False)
    data, usage = deepseek.call_json(conn, system=prompts.system_prompt(prompts.REVIEW_TASK),
                                     user="Weekly summaries (JSON):\n" + user, schema=deepseek.Review,
                                     purpose="review", ref=f"{kind}:{start.isoformat()}", max_tokens=2500)
    cur = conn.execute(
        "INSERT INTO sail_reviews (kind, start_date, end_date, weeks, result_json, model, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, start.isoformat(), end.isoformat(), json.dumps([w["week_start"] for w in weeks]), json.dumps(data),
         usage["model"], _now()))
    conn.commit()
    return next(r for r in list_reviews(conn) if r["id"] == cur.lastrowid)


def list_reviews(conn: sqlite3.Connection) -> list[dict]:
    return [{"id": r["id"], "kind": r["kind"], "start_date": r["start_date"], "end_date": r["end_date"],
             "weeks": _json(r["weeks"]), "result": _json(r["result_json"]), "model": r["model"],
             "created_at": r["created_at"]}
            for r in conn.execute("SELECT * FROM sail_reviews ORDER BY created_at DESC")]


# ------------------------------------------------------------------ for the coach / weekly planner
def planner_focus(conn: sqlite3.Connection, days: int = 14, today: date | None = None) -> dict:
    """The sailing focus points the coach plans the week around: each
    recently analysed session's focus_next_session (newest first) and the
    latest weekly summary's focus."""
    since = ((today or date.today()) - timedelta(days=days)).isoformat()
    sessions = []
    for r in conn.execute(
            "SELECT t.id, t.start_date, t.spot, a.result_json, a.packet_json FROM sail_session_analysis a "
            "JOIN tracks t ON t.id = a.track_id WHERE t.start_date >= ? ORDER BY t.start_utc DESC LIMIT 5", (since,)):
        res, pk = _json(r["result_json"]), _json(r["packet_json"])
        sessions.append({"track_id": r["id"], "date": r["start_date"], "spot": r["spot"],
                         "band_kn": (pk.get("wind") or {}).get("band_kn"),
                         "focus_next_session": res.get("focus_next_session") or [],
                         "plan_adjustment": res.get("plan_adjustment")})
    weekly = list_weekly(conn)
    latest = weekly[0] if weekly else None
    return {"sessions": sessions,
            "week": {"week_start": latest["week_start"], "focus_next_week": latest["result"].get("focus_next_week") or [],
                     "plan_adjustment": latest["result"].get("plan_adjustment")} if latest else None}


def baselines_overview(conn: sqlite3.Connection) -> dict:
    return {"bands": baselines.BAND_LABELS, "wind_bands": [list(b) for b in WIND_BANDS],
            "match_parts": list(MATCH_PARTS), "rolling_n": ROLLING_N, "min_combo_sessions": MIN_COMBO_SESSIONS,
            "baselines": baselines.stored(conn)}
