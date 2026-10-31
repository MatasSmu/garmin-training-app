"""Glue between the engine and the database: importing files, caching
analyses, and the cross-session queries (personal polar, trends, compare,
speed map, gear usage)."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.analysis import conditions as conditions_mod
from app.analysis.geo import angdiff
from app.analysis.loader import load_bytes, load_file
from app.analysis.pipeline import analyze
from app.analysis.settings import SPORTS, guess_sport, settings_hash
from app.analysis.stats import polar_from_hist
from app.config import PROJECT_ROOT
from app.sailing import baselines as sail_baselines
from app.sailing.config import PARTS as SAIL_PARTS
from app.sailing.gear import part_identity

log = logging.getLogger(__name__)

TRACKS_DIR = PROJECT_ROOT / "data" / "tracks"
GEAR_KINDS = ("board", "front_wing", "back_wing", "foil_mast", "fuselage", "sail", "mast", "boom",
              "fin", "foil", "wing", "kite", "other")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(s: str | None, default=None):
    return json.loads(s) if s else default


# ------------------------------------------------------------------ import
def _match_activity(conn: sqlite3.Connection, start_utc: float) -> sqlite3.Row | None:
    day = datetime.fromtimestamp(start_utc, timezone.utc).date()
    rows = conn.execute(
        "SELECT activity_id, activity_name, location, raw_json FROM sessions WHERE start_date BETWEEN ? AND ?",
        ((day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()),
    ).fetchall()
    best = None
    for r in rows:
        try:
            begin = json.loads(r["raw_json"]).get("beginTimestamp")
        except ValueError:
            continue
        if begin is None:
            continue
        d = abs(begin / 1000.0 - start_utc)
        if d <= 180 and (best is None or d < best[0]):
            best = (d, r)
    return best[1] if best else None


def _offset_from_activity(row: sqlite3.Row) -> int | None:
    try:
        raw = json.loads(row["raw_json"])
        loc = datetime.fromisoformat(raw["startTimeLocal"])
        gmt = datetime.fromisoformat(raw["startTimeGMT"])
        return int(round((loc - gmt).total_seconds() / 900) * 900)
    except (KeyError, ValueError, TypeError):
        return None


def _nearest_spot(conn, lat: float, lon: float, max_km: float = 5.0) -> str | None:
    best = None
    for r in conn.execute("SELECT spot, lat, lon FROM tracks WHERE spot IS NOT NULL AND spot != '' AND lat IS NOT NULL"):
        dy = (r["lat"] - lat) * 111.2
        dx = (r["lon"] - lon) * 111.2 * np.cos(np.radians(lat))
        d = float(np.hypot(dx, dy))
        if d <= max_km and (best is None or d < best[0]):
            best = (d, r["spot"])
    return best[1] if best else None


def import_bytes(conn: sqlite3.Connection, data: bytes, filename: str, *,
                 activity_id: int | None = None, fetch_conditions: bool = True) -> tuple[int, bool]:
    """Store + analyse a FIT/GPX file. Returns (track_id, created)."""
    sha = hashlib.sha1(data).hexdigest()
    row = conn.execute("SELECT id FROM tracks WHERE file_sha1 = ?", (sha,)).fetchone()
    if row:
        return row["id"], False
    track = load_bytes(data, filename)  # raises ValueError on junk
    ext = Path(filename).suffix.lower()
    TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    rel = f"{sha}{ext}"
    (TRACKS_DIR / rel).write_bytes(data)

    start, end = float(track.t[0]), float(track.t[-1])
    act = None
    if activity_id is not None:
        act = conn.execute("SELECT activity_id, activity_name, location, raw_json FROM sessions WHERE activity_id = ?",
                           (activity_id,)).fetchone()
    if act is None:
        act = _match_activity(conn, start)
    offset = track.meta.get("utc_offset_s")
    if offset is None and act is not None:
        offset = _offset_from_activity(act)
    offset = int(offset or 0)
    lat, lon = float(np.median(track.lat)), float(np.median(track.lon))
    spot = None
    if act is not None and act["location"] and act["location"] != "Unspecified":
        spot = act["location"]
    spot = spot or _nearest_spot(conn, lat, lon)
    if spot is None and fetch_conditions:
        spot = conditions_mod.place_name(lat, lon)
    sport = guess_sport(act["activity_name"] if act else None, track.meta.get("sport_name"),
                        filename, track.meta.get("fit_sport"))
    title = (act["activity_name"] if act else None) or track.meta.get("sport_name") or Path(filename).stem
    local_date = datetime.fromtimestamp(start + offset, timezone.utc).date().isoformat()
    now = _now()
    cur = conn.execute(
        """INSERT INTO tracks (activity_id, file_name, file_sha1, file_path, sport, title, start_utc, end_utc,
                               utc_offset_s, start_date, lat, lon, spot, settings_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)""",
        (act["activity_id"] if act else None, Path(filename).name, sha, rel, sport, title, start, end,
         offset, local_date, lat, lon, spot, now, now),
    )
    tid = cur.lastrowid
    conn.commit()
    if fetch_conditions:
        refresh_conditions(conn, tid, analyse=False)
    get_analysis(conn, tid, track=track)
    return tid, True


WATER_WORDS = ("windsurf", "kiteboard", "kitesurf", "wing", "foil", "sailing")


def is_water_session(activity_type: str | None, name: str | None) -> bool:
    text = f"{activity_type or ''} {name or ''}".lower()
    return any(w in text for w in WATER_WORDS)


def import_garmin_water(conn, since: str, *, client=None, fetch_conditions: bool = True,
                        on_item=None) -> dict:
    """Download + analyse every synced water session since `since` that has
    no track yet. Raises GarminUnavailable if Garmin can't be used at all."""
    from app.ingest.garmin_client import fetch_original_file, login_saved_tokens

    rows = [r for r in conn.execute(
        "SELECT s.activity_id, s.activity_type, s.activity_name, s.start_date FROM sessions s "
        "LEFT JOIN tracks t ON t.activity_id = s.activity_id "
        "WHERE s.start_date >= ? AND t.id IS NULL ORDER BY s.start_date", (since,)).fetchall()
        if is_water_session(r["activity_type"], r["activity_name"])]
    out = {"imported": 0, "failed": 0, "errors": [], "ids": []}
    if not rows:
        return out
    client = client or login_saved_tokens()
    for k, r in enumerate(rows):
        if on_item:
            on_item(k + 1, len(rows), r)
        data, name = fetch_original_file(r["activity_id"], client=client)
        try:
            tid, created = import_bytes(conn, data, name, activity_id=r["activity_id"],
                                        fetch_conditions=fetch_conditions)
            out["imported"] += int(created)
            out["ids"].append(tid)
        except ValueError as e:  # e.g. an indoor session with no GPS
            out["failed"] += 1
            out["errors"].append(f"{r['start_date']} {r['activity_name']}: {e}")
    return out


def import_path(conn, path: str | Path, **kw) -> tuple[int, bool]:
    p = Path(path)
    return import_bytes(conn, p.read_bytes(), p.name, **kw)


def refresh_conditions(conn, track_id: int, analyse: bool = True) -> dict | None:
    t = get_track_row(conn, track_id)
    if t is None:
        return None
    cond = conditions_mod.fetch(t["lat"], t["lon"], t["start_utc"], t["end_utc"])
    if cond is None:
        return None
    conn.execute("UPDATE tracks SET conditions_json = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(cond), _now(), track_id))
    conn.commit()
    sail_baselines.rebuild(conn)  # wind speed decides the band
    if analyse:
        get_analysis(conn, track_id)
    return cond


# ------------------------------------------------------------------ analysis
def get_track_row(conn, track_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()


def _engine_overrides(t: sqlite3.Row) -> dict:
    ov = _loads(t["settings_json"], {}) or {}
    cond = _loads(t["conditions_json"])
    if cond and cond.get("wind_dir") is not None and ov.get("wind_prior_deg") is None:
        ov["wind_prior_deg"] = float(cond["wind_dir"])
        ov["wind_prior_source"] = "manual" if cond.get("source") == "manual" else "open-meteo"
    ov["sport"] = t["sport"]
    return ov


def personal_polar(conn, sport: str, *, gear_id: int | None = None, spot: str | None = None,
                   wind_min: float | None = None, wind_max: float | None = None,
                   track_ids: list[int] | None = None) -> tuple[dict | None, str, int]:
    """Aggregated polar (p90 per TWA bin = the personal polar) from stored
    per-session histograms. Returns (polar, signature, n_sessions)."""
    rows = filter_tracks(conn, sport=sport, gear_id=gear_id, spot=spot, wind_min=wind_min,
                         wind_max=wind_max, ids=track_ids, need_polar=True)
    entries: list = []
    sigs = []
    for r in rows:
        entries.extend(_loads(r["polar_hist_json"], []))
        sigs.append(r["polar_sig"] or "")
    sig = hashlib.sha1("|".join(sorted(sigs)).encode()).hexdigest()[:12]
    if not entries:
        return None, sig, 0
    return polar_from_hist(entries, min_s=30.0), sig, len(rows)


def get_analysis(conn, track_id: int, *, force: bool = False, track=None) -> dict:
    t = get_track_row(conn, track_id)
    if t is None:
        raise KeyError(track_id)
    ov = _engine_overrides(t)
    ppolar, psig, _ = personal_polar(conn, t["sport"])
    h = settings_hash(ov, psig)
    if not force:
        cached = conn.execute("SELECT hash, result_gz FROM track_analysis WHERE track_id = ?", (track_id,)).fetchone()
        if cached and cached["hash"] == h:
            return json.loads(gzip.decompress(cached["result_gz"]))
    if track is None:
        track = load_file(TRACKS_DIR / t["file_path"])
    result = analyze(track, ov, personal_polar=ppolar, sport=t["sport"])
    result["personal_polar"] = ppolar
    blob = gzip.compress(json.dumps(result, allow_nan=False).encode(), compresslevel=5)
    hist = json.dumps(result["polar_hist"])
    conn.execute(
        "INSERT INTO track_analysis (track_id, hash, result_gz, computed_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(track_id) DO UPDATE SET hash = excluded.hash, result_gz = excluded.result_gz, "
        "computed_at = excluded.computed_at",
        (track_id, h, blob, _now()),
    )
    conn.execute(
        """UPDATE tracks SET summary_json = ?, polar_hist_json = ?, grid_json = ?, polar_sig = ?,
                  analysis_hash = ?, end_utc = ? WHERE id = ?""",
        (json.dumps(result["summary"]), hist, json.dumps(result["speed_grid"]),
         hashlib.sha1(hist.encode()).hexdigest()[:12], h, result["summary"]["end_utc"], track_id),
    )
    conn.commit()
    sail_baselines.rebuild(conn)
    return result


def reanalyze_all(conn) -> int:
    # sessions imported before their neighbours had a spot pick one up now
    for r in conn.execute("SELECT id, lat, lon FROM tracks WHERE spot IS NULL OR spot = ''").fetchall():
        spot = _nearest_spot(conn, r["lat"], r["lon"])
        if spot:
            conn.execute("UPDATE tracks SET spot = ? WHERE id = ?", (spot, r["id"]))
    conn.commit()
    ids = [r["id"] for r in conn.execute("SELECT id FROM tracks ORDER BY start_utc")]
    for _ in range(2):  # second pass: personal polars now include every session
        for i in ids:
            get_analysis(conn, i)
    return len(ids)


# ------------------------------------------------------------------ track CRUD
TRACK_FIELDS = ("title", "spot", "notes", "sport")


def update_track(conn, track_id: int, patch: dict) -> None:
    t = get_track_row(conn, track_id)
    if t is None:
        raise KeyError(track_id)
    sets, params = [], []
    for k in TRACK_FIELDS:
        if k in patch:
            v = patch[k]
            if k == "sport" and v not in SPORTS:
                raise ValueError(f"unknown sport {v!r}")
            sets.append(f"{k} = ?")
            params.append(v.strip() if isinstance(v, str) else v)
    if "settings" in patch:
        cur = _loads(t["settings_json"], {}) or {}
        for k, v in (patch["settings"] or {}).items():
            if v is None:
                cur.pop(k, None)
            elif k == "targets" and isinstance(v, dict):
                cur["targets"] = {**cur.get("targets", {}), **v}
            else:
                cur[k] = v
        sets.append("settings_json = ?")
        params.append(json.dumps(cur))
    if "conditions" in patch:
        c = patch["conditions"]
        if c is not None:
            c = {**c, "source": "manual", "fetched_at": _now()}
        sets.append("conditions_json = ?")
        params.append(json.dumps(c) if c is not None else None)
    if sets:
        sets.append("updated_at = ?")
        params.append(_now())
        conn.execute(f"UPDATE tracks SET {', '.join(sets)} WHERE id = ?", [*params, track_id])
    if "gear_ids" in patch:
        ids = sorted({int(g) for g in patch["gear_ids"] or []})
        kinds = {r["id"]: r["kind"] for r in conn.execute(
            f"SELECT id, kind FROM gear WHERE id IN ({','.join('?' for _ in ids)})", ids)} if ids else {}
        if len(kinds) != len(ids):
            conn.rollback()
            raise ValueError("unknown gear id")
        slots = [k for k in kinds.values() if k in SAIL_PARTS]
        if len(slots) != len(set(slots)):
            conn.rollback()
            raise ValueError("only one part of each kind (board, front wing, sail, ...) per session")
        conn.execute("DELETE FROM track_gear WHERE track_id = ?", (track_id,))
        for gid in ids:
            conn.execute("INSERT INTO track_gear (track_id, gear_id) VALUES (?, ?)", (track_id, gid))
    conn.commit()
    sail_baselines.rebuild(conn)


def delete_track(conn, track_id: int) -> bool:
    t = get_track_row(conn, track_id)
    if t is None:
        return False
    conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
    conn.commit()
    sail_baselines.rebuild(conn)
    if not conn.execute("SELECT 1 FROM tracks WHERE file_path = ?", (t["file_path"],)).fetchone():
        (TRACKS_DIR / t["file_path"]).unlink(missing_ok=True)
    return True


def track_gear(conn, track_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT g.* FROM gear g JOIN track_gear tg ON tg.gear_id = g.id WHERE tg.track_id = ? ORDER BY g.kind, g.name",
        (track_id,))]


def track_meta(conn, t: sqlite3.Row) -> dict:
    return {
        "id": t["id"], "activity_id": t["activity_id"], "file_name": t["file_name"], "sport": t["sport"],
        "title": t["title"], "start_utc": t["start_utc"], "end_utc": t["end_utc"],
        "utc_offset_s": t["utc_offset_s"], "start_date": t["start_date"], "lat": t["lat"], "lon": t["lon"],
        "spot": t["spot"], "notes": t["notes"], "settings": _loads(t["settings_json"], {}),
        "conditions": _loads(t["conditions_json"]), "summary": _loads(t["summary_json"]),
        "gear": track_gear(conn, t["id"]),
    }


# ------------------------------------------------------------------ queries
def filter_tracks(conn, *, sport=None, spot=None, gear_id=None, wind_min=None, wind_max=None,
                  wind_dir=None, dir_tol=30.0, start=None, end=None, ids=None, need_polar=False) -> list[sqlite3.Row]:
    q = "SELECT DISTINCT t.* FROM tracks t"
    clauses, params = [], []
    if gear_id is not None:
        q += " JOIN track_gear tg ON tg.track_id = t.id"
        clauses.append("tg.gear_id = ?")
        params.append(gear_id)
    if sport:
        clauses.append("t.sport = ?")
        params.append(sport)
    if spot:
        clauses.append("t.spot = ?")
        params.append(spot)
    if start:
        clauses.append("t.start_date >= ?")
        params.append(start)
    if end:
        clauses.append("t.start_date <= ?")
        params.append(end)
    if ids:
        clauses.append(f"t.id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    if need_polar:
        clauses.append("t.polar_hist_json IS NOT NULL")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY t.start_utc DESC"
    rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        cond = _loads(r["conditions_json"]) or {}
        summ = _loads(r["summary_json"]) or {}
        ws = cond.get("wind_kn")
        if wind_min is not None and (ws is None or ws < wind_min):
            continue
        if wind_max is not None and (ws is None or ws > wind_max):
            continue
        if wind_dir is not None:
            wd = summ.get("wind_deg", cond.get("wind_dir"))
            if wd is None or abs(float(angdiff(wd, wind_dir))) > dir_tol:
                continue
        out.append(r)
    return out


LIST_KEYS = ("duration_s", "distance_m", "max_kn", "best_10s_kn", "best_500m_kn", "best_1nm_kn", "avg_moving_kn",
             "wind_deg", "wind_confidence", "tacks", "gybes", "tack_success_pct", "gybe_success_pct",
             "state_pct", "state_name", "upwind_vmg_kn", "downwind_vmg_kn", "upwind_twa", "downwind_twa",
             "tack_loss_m", "gybe_loss_m", "hr_avg", "touchdowns", "wind_steadiness", "wind_shift_deg",
             "wind_shift_rate_deg", "wind_suspect")


def list_rows(conn, **filters) -> list[dict]:
    rows = filter_tracks(conn, **filters)
    gear = {}
    for g in conn.execute("SELECT tg.track_id, g.id, g.kind, g.code, g.name FROM track_gear tg JOIN gear g ON g.id = tg.gear_id"):
        gear.setdefault(g["track_id"], []).append({"id": g["id"], "kind": g["kind"], "code": g["code"], "name": g["name"]})
    out = []
    for r in rows:
        s = _loads(r["summary_json"]) or {}
        cond = _loads(r["conditions_json"]) or {}
        out.append({
            "id": r["id"], "title": r["title"], "sport": r["sport"], "spot": r["spot"],
            "start_utc": r["start_utc"], "utc_offset_s": r["utc_offset_s"], "start_date": r["start_date"],
            "activity_id": r["activity_id"], "notes": r["notes"],
            "wind_kn": cond.get("wind_kn"), "gust_kn": cond.get("gust_kn"), "cond_dir": cond.get("wind_dir"),
            "gear": gear.get(r["id"], []),
            **{k: s.get(k) for k in LIST_KEYS},
        })
    return out


TREND_KEYS = ("max_kn", "best_10s_kn", "best_500m_kn", "best_1nm_kn", "avg_moving_kn", "upwind_vmg_kn",
              "downwind_vmg_kn", "upwind_twa", "downwind_twa", "tack_success_pct", "gybe_success_pct",
              "tack_loss_m", "gybe_loss_m", "state_pct", "hr_avg", "distance_m", "duration_s")


def trends(conn, **filters) -> list[dict]:
    return [{"id": r["id"], "date": r["start_date"], "start_utc": r["start_utc"], "title": r["title"],
             "spot": r["spot"], "wind_kn": r["wind_kn"], **{k: r.get(k) for k in TREND_KEYS}}
            for r in sorted(list_rows(conn, **filters), key=lambda x: x["start_utc"])]


def compare(conn, ids: list[int]) -> list[dict]:
    out = []
    for i in ids:
        t = get_track_row(conn, i)
        if t is None:
            continue
        r = get_analysis(conn, i)
        out.append({"track": track_meta(conn, t), "summary": r["summary"], "polar": r["polar"],
                    "point_of_sail": r["point_of_sail"], "splits": r["splits"], "state": r["state"],
                    "wind": {k: r["wind"][k] for k in ("deg", "confidence", "source")},
                    "maneuvers": [{k: m[k] for k in ("type", "entry_kn", "min_kn", "exit_kn", "duration_s",
                                                      "distance_lost_m", "success")} for m in r["maneuvers"]]})
    return out


def speed_map(conn, *, spot: str | None = None, sport: str | None = None, wind_dir: float | None = None,
              dir_tol: float = 30.0, ids: list[int] | None = None, metric: str = "mean") -> dict:
    rows = filter_tracks(conn, spot=spot, sport=sport, wind_dir=wind_dir, dir_tol=dir_tol, ids=ids)
    cells: dict[tuple[int, int], list[float]] = {}
    dlat = dlon = None
    used = []
    for r in rows:
        g = _loads(r["grid_json"])
        if not g:
            continue
        if dlat is None:
            dlat, dlon = g["dlat"], g["dlon"]
        elif abs(g["dlon"] - dlon) > 1e-12:
            continue  # different latitude band -> different grid
        used.append(r["id"])
        for a, b, secs, sum_kn, mx, on in g["cells"]:
            c = cells.setdefault((a, b), [0.0, 0.0, 0.0, 0.0, 0])
            c[0] += secs
            c[1] += sum_kn
            c[2] = max(c[2], mx)
            c[3] += on
            c[4] += 1
    out = []
    for (a, b), (secs, sum_kn, mx, on, n) in cells.items():
        if secs < 3:
            continue
        out.append({"lat": (a + 0.5) * dlat, "lon": (b + 0.5) * dlon, "seconds": round(secs, 1),
                    "mean_kn": round(sum_kn / secs, 2), "max_kn": mx, "on_pct": round(100 * on / secs, 1),
                    "sessions": n})
    return {"cells": out, "dlat": dlat, "dlon": dlon, "track_ids": used}


def spots(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT spot, COUNT(*) AS sessions, AVG(lat) AS lat, AVG(lon) AS lon, MAX(start_date) AS last "
        "FROM tracks WHERE spot IS NOT NULL AND spot != '' GROUP BY spot ORDER BY sessions DESC")]


# ------------------------------------------------------------------ gear
def list_gear(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM gear ORDER BY retired, kind, name").fetchall()
    usage: dict[int, dict] = {}
    for r in conn.execute(
            "SELECT tg.gear_id, t.summary_json, t.start_date FROM track_gear tg JOIN tracks t ON t.id = tg.track_id"):
        s = _loads(r["summary_json"]) or {}
        u = usage.setdefault(r["gear_id"], {"sessions": 0, "hours": 0.0, "km": 0.0, "max_kn": 0.0, "last_used": None})
        u["sessions"] += 1
        u["hours"] += (s.get("moving_s") or 0) / 3600
        u["km"] += (s.get("distance_m") or 0) / 1000
        u["max_kn"] = max(u["max_kn"], s.get("max_kn") or 0)
        u["last_used"] = max(u["last_used"] or "", r["start_date"])
    out = []
    for g in rows:
        u = usage.get(g["id"], {"sessions": 0, "hours": 0.0, "km": 0.0, "max_kn": 0.0, "last_used": None})
        out.append({**dict(g), **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in u.items()},
                    "nm": round(u["km"] / 1.852, 1)})
    return out


GEAR_FIELDS = ("kind", "code", "name", "brand", "size", "notes", "retired")


def _check_gear(conn, values: dict, gear_id: int | None) -> dict:
    """A part needs a code or a name (the name defaults to the code). A code
    may appear only once per kind, compared ignoring case and spaces."""
    cur = dict(conn.execute("SELECT * FROM gear WHERE id = ?", (gear_id,)).fetchone()) if gear_id else {}
    merged = {**cur, **values}
    for k in ("code", "name", "brand", "size", "notes"):
        if isinstance(values.get(k), str):
            values[k] = values[k].strip() or None
            merged[k] = values[k]
    kind = merged.get("kind")
    if kind not in GEAR_KINDS:
        raise ValueError(f"kind must be one of {', '.join(GEAR_KINDS)}")
    if not merged.get("name"):
        if not merged.get("code"):
            raise ValueError("a code (e.g. SC 392 899) or a name is required")
        values["name"] = merged["code"]
    ident = part_identity(merged.get("code"))
    if ident:
        for r in conn.execute("SELECT id, code FROM gear WHERE kind = ? AND code IS NOT NULL", (kind,)):
            if r["id"] != gear_id and part_identity(r["code"]) == ident:
                raise ValueError(f"{kind.replace('_', ' ')} {r['code']} is already in the inventory")
    return values


def upsert_gear(conn, values: dict, gear_id: int | None = None) -> int:
    values = _check_gear(conn, dict(values), gear_id)
    now = _now()
    cols = [c for c in GEAR_FIELDS if c in values]
    if gear_id is None:
        cur = conn.execute(
            f"INSERT INTO gear ({', '.join(cols)}, created_at, updated_at) VALUES ({', '.join('?' for _ in cols)}, ?, ?)",
            [*(values[c] for c in cols), now, now])
        conn.commit()
        return cur.lastrowid
    if cols:
        conn.execute(f"UPDATE gear SET {', '.join(f'{c} = ?' for c in cols)}, updated_at = ? WHERE id = ?",
                     [*(values[c] for c in cols), now, gear_id])
        conn.commit()
        sail_baselines.rebuild(conn)  # a changed code or kind can move sessions between combos
    return gear_id


def delete_gear(conn, gear_id: int) -> bool:
    cur = conn.execute("DELETE FROM gear WHERE id = ?", (gear_id,))
    conn.commit()
    sail_baselines.rebuild(conn)
    return cur.rowcount > 0


# ------------------------------------------------------------------ gear setups
def list_setups(conn) -> list[dict]:
    parts: dict[int, list[dict]] = {}
    for r in conn.execute("SELECT sp.setup_id, g.id, g.kind, g.code, g.name, g.retired FROM gear_setup_parts sp "
                          "JOIN gear g ON g.id = sp.gear_id ORDER BY g.kind"):
        parts.setdefault(r["setup_id"], []).append(
            {"id": r["id"], "kind": r["kind"], "code": r["code"], "name": r["name"], "retired": r["retired"]})
    return [{**dict(s), "parts": parts.get(s["id"], [])}
            for s in conn.execute("SELECT * FROM gear_setups ORDER BY name")]


def save_setup(conn, values: dict, setup_id: int | None = None) -> int:
    """Create or update a setup; gear_ids, when given, replaces its parts
    (at most one part of each iQFOIL kind)."""
    now = _now()
    name = values.get("name")
    if name is not None:
        name = name.strip()
    if setup_id is None and not name:
        raise ValueError("a setup needs a name")
    ids = None
    if values.get("gear_ids") is not None:
        ids = sorted({int(g) for g in values["gear_ids"]})
        kinds = [r["kind"] for r in conn.execute(
            f"SELECT kind FROM gear WHERE id IN ({','.join('?' for _ in ids)})", ids)] if ids else []
        if len(kinds) != len(ids):
            raise ValueError("unknown gear id")
        slots = [k for k in kinds if k in SAIL_PARTS]
        if len(slots) != len(set(slots)):
            raise ValueError("only one part of each kind per setup")
    if setup_id is None:
        setup_id = conn.execute("INSERT INTO gear_setups (name, notes, created_at, updated_at) VALUES (?, ?, ?, ?)",
                                (name, values.get("notes"), now, now)).lastrowid
    else:
        sets, params = ["updated_at = ?"], [now]
        if name:
            sets.append("name = ?")
            params.append(name)
        if "notes" in values:
            sets.append("notes = ?")
            params.append(values["notes"])
        conn.execute(f"UPDATE gear_setups SET {', '.join(sets)} WHERE id = ?", [*params, setup_id])
    if ids is not None:
        conn.execute("DELETE FROM gear_setup_parts WHERE setup_id = ?", (setup_id,))
        conn.executemany("INSERT INTO gear_setup_parts (setup_id, gear_id) VALUES (?, ?)", [(setup_id, g) for g in ids])
    conn.commit()
    return setup_id


def delete_setup(conn, setup_id: int) -> bool:
    cur = conn.execute("DELETE FROM gear_setups WHERE id = ?", (setup_id,))
    conn.commit()
    return cur.rowcount > 0
