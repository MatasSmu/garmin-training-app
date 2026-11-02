"""Weight & nutrition data access: targets from the coach's plan, idempotent
sync from the phone page, day totals, the weight trend analysis, weekly
summaries, the expenditure estimate and the compact coach/dashboard views.

Dates are the athlete's local dates ('YYYY-MM-DD'); updated_at timestamps are
ISO UTC with milliseconds (last write wins, as in the gym tracker)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone

from app.gym.plan_import import DEFAULT_PLAN_PATH
from app.gym.store import current_plan, norm_ts_ms
from app.nutrition import calc

PHONE_LOG_DAYS = 35
EXPENDITURE_WINDOW_DAYS = 28
EXPENDITURE_MIN_DAYS = 14
FOOD_DAY_SHARE = 0.5       # a day counts as "food logged" at >= 50 % of the kcal minimum
SUMMARY_WEEKS = 12
SLOTS = ("morning", "evening")


def _now_ms() -> str:
    return norm_ts_ms(datetime.now(timezone.utc).isoformat())


def _today() -> date:
    return date.today()


# --------------------------------------------------------------------- targets


def _plan_json(conn: sqlite3.Connection) -> dict:
    plan = current_plan(conn)
    if plan is not None:
        return json.loads(plan["source_json"])
    try:
        return json.loads(DEFAULT_PLAN_PATH.read_text())
    except (OSError, ValueError):
        return {}


def targets(conn: sqlite3.Connection) -> dict:
    """Goal weight, rate and daily targets, all from the coach's plan file."""
    data = _plan_json(conn)
    plan = data.get("plan") or {}
    athlete = plan.get("athlete") or {}
    nutrition = data.get("nutrition") or {}
    start_date = date.fromisoformat(plan["start_date"]) if plan.get("start_date") else None
    weeks = plan.get("weeks")
    start_w, goal_w = athlete.get("bodyweight_kg"), athlete.get("target_bodyweight_kg")
    rate = (goal_w - start_w) / weeks if None not in (start_w, goal_w) and weeks else None
    return {
        "start_weight_kg": start_w,
        "goal_weight_kg": goal_w,
        "start_date": start_date.isoformat() if start_date else None,
        "goal_date": (start_date + timedelta(weeks=weeks)).isoformat() if start_date and weeks else None,
        "weeks": weeks,
        "target_rate_kg_week": round(rate, 3) if rate is not None else 0.0,
        "kcal_min": nutrition.get("kcal_per_day_min"),
        "kcal_max": nutrition.get("kcal_per_day_max"),
        "protein_g": nutrition.get("protein_g_per_day"),
    }


# --------------------------------------------------------------------- weights


KEEP = object()   # upsert_weight: leave this field as it is


def upsert_weight(conn: sqlite3.Connection, day: str, slot: str, weight_kg: float | None,
                  updated_at: str | None = None, note=KEEP, score=KEEP, time=KEEP) -> str:
    """One entry per date + slot; re-entering updates it, None deletes it.
    note / score / time left as KEEP keep what is stored (the weight grid only
    sends weights). Returns 'created' | 'updated' | 'deleted' | 'unchanged' | 'stale'."""
    if slot not in SLOTS:
        raise ValueError(f"slot must be one of {SLOTS}")
    day = date.fromisoformat(day).isoformat()
    incoming = norm_ts_ms(updated_at) if updated_at else _now_ms()
    row = conn.execute("SELECT * FROM weight_entries WHERE date = ? AND slot = ?", (day, slot)).fetchone()
    if row is not None and row["client_updated_at"] > incoming:
        return "stale"
    if weight_kg is None:
        if row is None:
            return "unchanged"
        conn.execute("DELETE FROM weight_entries WHERE id = ?", (row["id"],))
        return "deleted"
    weight_kg = round(float(weight_kg), 2)
    note = (row["note"] if row is not None else None) if note is KEEP else ((note or "").strip() or None)
    score = (row["score"] if row is not None else None) if score is KEEP else (round(float(score), 2) if score is not None else None)
    time = (row["time"] if row is not None else None) if time is KEEP else (time or None)
    if row is not None and (row["weight_kg"], row["note"], row["score"], row["time"]) == (weight_kg, note, score, time):
        return "unchanged"
    now = _now_ms()
    conn.execute(
        """INSERT INTO weight_entries (id, date, slot, weight_kg, note, score, time, client_updated_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date, slot) DO UPDATE SET weight_kg = excluded.weight_kg, note = excluded.note,
             score = excluded.score, time = excluded.time,
             client_updated_at = excluded.client_updated_at, updated_at = excluded.updated_at""",
        (str(uuid.uuid4()), day, slot, weight_kg, note, score, time, incoming, now, now),
    )
    return "updated" if row is not None else "created"


def _upsert_weight_dict(conn: sqlite3.Connection, e: dict) -> str:
    extra = {k: e[k] for k in ("note", "score", "time") if k in e}
    return upsert_weight(conn, e["date"], e["slot"], e.get("weight_kg"), e.get("updated_at"), **extra)


def save_weights(conn: sqlite3.Connection, entries: list[dict]) -> dict:
    """Batch save (the week grid). Each entry: {date, slot, weight_kg|None, updated_at?, note?, score?, time?};
    a key that is missing keeps the stored value."""
    results = {}
    for e in entries:
        results[f"{e['date']}|{e['slot']}"] = _upsert_weight_dict(conn, e)
    conn.commit()
    return results


def list_weights(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> list[dict]:
    q, params = "SELECT date, slot, weight_kg, note, score, time, client_updated_at AS updated_at FROM weight_entries", []
    clauses = []
    if start:
        clauses.append("date >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    return [dict(r) for r in conn.execute(q + " ORDER BY date, slot", params)]


def _weights_by_slot(conn: sqlite3.Connection) -> tuple[dict[date, float], dict[date, float]]:
    morning, evening = {}, {}
    for r in conn.execute("SELECT date, slot, weight_kg FROM weight_entries"):
        (morning if r["slot"] == "morning" else evening)[date.fromisoformat(r["date"])] = r["weight_kg"]
    return morning, evening


def weight_analysis(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Daily morning/evening weights with the trend, the weekly rate, status
    against the plan and the projected date for the goal weight."""
    today = today or _today()
    t = targets(conn)
    morning, evening = _weights_by_slot(conn)
    series = calc.trend_series(morning)
    trend_by_day = {r["date"]: r["trend"] for r in series}
    rate = calc.weekly_rate(series)
    latest = series[-1] if series else None
    trend = latest["trend"] if latest else None
    goal = t["goal_weight_kg"]
    st = calc.status(rate, t["target_rate_kg_week"], trend, goal)
    proj = calc.projected_date(trend, goal, rate, today) if st != "reached" else None
    diff = calc.evening_difference(morning, evening)
    days = sorted(set(trend_by_day) | set(evening))
    return {
        "days": [
            {"date": d.isoformat(), "morning": morning.get(d), "evening": evening.get(d),
             "trend": round(trend_by_day[d], 2) if d in trend_by_day else None}
            for d in days
        ],
        "trend_kg": round(trend, 2) if trend is not None else None,
        "trend_date": latest["date"].isoformat() if latest else None,
        "rate_kg_week": round(rate, 2) if rate is not None else None,
        "target_rate_kg_week": t["target_rate_kg_week"],
        "status": st,
        "status_label": calc.STATUS_LABEL[st],
        "projected_date": proj.isoformat() if proj else None,
        "goal_weight_kg": goal,
        "goal_date": t["goal_date"],
        "start_weight_kg": t["start_weight_kg"],
        "start_date": t["start_date"],
        "evening_diff_kg": round(diff, 2) if diff is not None else None,
        "to_goal_kg": round(goal - trend, 2) if goal is not None and trend is not None else None,
    }


# --------------------------------------------------------------------- foods


FOOD_FIELDS = ("name", "brand", "kcal_100g", "protein_100g", "carbs_100g", "fat_100g", "serving_g", "barcode", "source")


def _lww(conn: sqlite3.Connection, table: str, row_id: str, incoming: str) -> tuple[sqlite3.Row | None, str | None]:
    """Existing row and, if the incoming copy shouldn't be applied, why."""
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        return None, None
    if row["client_updated_at"] > incoming:
        return row, "stale"
    if row["client_updated_at"] == incoming:
        return row, "unchanged"
    return row, None


def upsert_food(conn: sqlite3.Connection, f: dict) -> str:
    incoming = norm_ts_ms(f.get("updated_at")) if f.get("updated_at") else _now_ms()
    row, skip = _lww(conn, "foods", f["id"], incoming)
    if skip:
        return skip
    barcode = (f.get("barcode") or "").strip() or None
    if barcode:
        # The same product entered twice (e.g. once on each device): keep the
        # barcode on the newest copy only, the older one stays as a plain food.
        conn.execute("UPDATE foods SET barcode = NULL WHERE barcode = ? AND id != ?", (barcode, f["id"]))
    now = _now_ms()
    vals = {k: f.get(k) for k in FOOD_FIELDS}
    vals["barcode"] = barcode
    vals["name"] = vals["name"].strip()
    vals["source"] = vals.get("source") or "manual"
    cols = [*FOOD_FIELDS, "deleted", "client_updated_at"]
    conn.execute(
        f"""INSERT INTO foods (id, {', '.join(cols)}, created_at, updated_at)
            VALUES (?, {', '.join('?' for _ in cols)}, ?, ?)
            ON CONFLICT(id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)},
              updated_at = excluded.updated_at""",
        (f["id"], *(vals[k] for k in FOOD_FIELDS), int(bool(f.get("deleted"))), incoming, now, now),
    )
    return "updated" if row is not None else "created"


def get_food(conn: sqlite3.Connection, food_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM foods WHERE id = ?", (food_id,)).fetchone()
    return _food_out(r) if r else None


def food_by_barcode(conn: sqlite3.Connection, barcode: str) -> dict | None:
    r = conn.execute("SELECT * FROM foods WHERE barcode = ? AND deleted = 0", (barcode,)).fetchone()
    return _food_out(r) if r else None


def _food_out(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in ("id", *FOOD_FIELDS)}
    d["deleted"] = bool(r["deleted"])
    d["updated_at"] = r["client_updated_at"]
    return d


def list_foods(conn: sqlite3.Connection, q: str | None = None) -> list[dict]:
    """Foods with the grams last used and when (for defaults and 'recent')."""
    sql = """SELECT f.*, (SELECT l.grams FROM food_logs l WHERE l.food_id = f.id AND l.deleted = 0 AND l.grams
                          IS NOT NULL ORDER BY l.date DESC, l.time DESC, l.client_updated_at DESC LIMIT 1) AS last_grams,
                         (SELECT MAX(l.date) FROM food_logs l WHERE l.food_id = f.id AND l.deleted = 0) AS last_used
             FROM foods f WHERE f.deleted = 0"""
    params: list = []
    if q:
        sql += " AND (f.name LIKE ? OR f.brand LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    rows = conn.execute(sql + " ORDER BY last_used IS NULL, last_used DESC, f.name", params).fetchall()
    return [{**_food_out(r), "last_grams": r["last_grams"], "last_used": r["last_used"]} for r in rows]


# --------------------------------------------------------------------- saved meals


def upsert_saved_meal(conn: sqlite3.Connection, m: dict) -> str:
    """A saved meal with its full item list (replaced on every update)."""
    incoming = norm_ts_ms(m.get("updated_at")) if m.get("updated_at") else _now_ms()
    row, skip = _lww(conn, "saved_meals", m["id"], incoming)
    if skip:
        return skip
    now = _now_ms()
    conn.execute(
        """INSERT INTO saved_meals (id, name, deleted, client_updated_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET name = excluded.name, deleted = excluded.deleted,
             client_updated_at = excluded.client_updated_at, updated_at = excluded.updated_at""",
        (m["id"], m["name"].strip(), int(bool(m.get("deleted"))), incoming, now, now),
    )
    conn.execute("DELETE FROM saved_meal_items WHERE saved_meal_id = ?", (m["id"],))
    for pos, it in enumerate(m.get("items") or []):
        food_id = it.get("food_id") if it.get("food_id") and get_food(conn, it["food_id"]) else None
        conn.execute(
            """INSERT INTO saved_meal_items (saved_meal_id, position, food_id, grams, description,
                                             kcal, protein_g, carbs_g, fat_g)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (m["id"], pos, food_id, it.get("grams"), it.get("description"),
             it.get("kcal"), it.get("protein_g"), it.get("carbs_g"), it.get("fat_g")),
        )
    return "updated" if row is not None else "created"


def delete_saved_meal(conn: sqlite3.Connection, meal_id: str) -> bool:
    """Tombstone, so the phone drops it on its next refresh; past logs keep their values."""
    now = _now_ms()
    cur = conn.execute("UPDATE saved_meals SET deleted = 1, client_updated_at = ?, updated_at = ? WHERE id = ?",
                       (now, now, meal_id))
    conn.commit()
    return cur.rowcount > 0


def _meal_items(conn: sqlite3.Connection, meal_id: str) -> list[dict]:
    out = []
    for r in conn.execute(
        """SELECT i.*, f.name AS food_name, f.kcal_100g, f.protein_100g, f.carbs_100g, f.fat_100g
           FROM saved_meal_items i LEFT JOIN foods f ON f.id = i.food_id
           WHERE i.saved_meal_id = ? ORDER BY i.position""",
        (meal_id,),
    ):
        if r["food_id"] and r["grams"] is not None:
            macros = calc.scale_food(dict(r), r["grams"])
            name = r["food_name"]
        else:
            macros = {k: r[k] for k in calc.MACROS}
            name = r["description"]
        out.append({"food_id": r["food_id"], "grams": r["grams"], "description": r["description"],
                    "name": name or "Item", **macros})
    return out


def saved_meal_totals(conn: sqlite3.Connection, meal_id: str) -> dict:
    return calc.totals(_meal_items(conn, meal_id))


def list_saved_meals(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    """Saved meals, most used in the last 30 days first."""
    since = ((today or _today()) - timedelta(days=30)).isoformat()
    rows = conn.execute(
        """SELECT m.*, (SELECT COUNT(*) FROM food_logs l WHERE l.saved_meal_id = m.id AND l.deleted = 0
                        AND l.date >= ?) AS uses
           FROM saved_meals m WHERE m.deleted = 0 ORDER BY uses DESC, m.name COLLATE NOCASE""",
        (since,),
    ).fetchall()
    out = []
    for r in rows:
        items = _meal_items(conn, r["id"])
        out.append({"id": r["id"], "name": r["name"], "uses_30d": r["uses"], "updated_at": r["client_updated_at"],
                    "items": items, "totals": calc.totals(items)})
    return out


def log_saved_meal(conn: sqlite3.Connection, meal_id: str, day: str, time: str | None = None,
                   meal: str | None = None, log_id: str | None = None) -> dict | None:
    """One food log with the saved meal's totals (one tap = one entry)."""
    m = conn.execute("SELECT * FROM saved_meals WHERE id = ? AND deleted = 0", (meal_id,)).fetchone()
    if m is None:
        return None
    t = saved_meal_totals(conn, meal_id)
    time = time or datetime.now().strftime("%H:%M")
    entry = {"id": log_id or str(uuid.uuid4()), "date": day, "time": time, "meal": meal or calc.default_meal(time),
             "saved_meal_id": meal_id, "description": m["name"], "source": "saved_meal", **t}
    upsert_log(conn, entry)
    conn.commit()
    return get_log(conn, entry["id"])


# --------------------------------------------------------------------- food logs


LOG_FIELDS = ("date", "time", "meal", "kcal", "protein_g", "carbs_g", "fat_g", "food_id", "grams",
              "saved_meal_id", "description", "source")


def upsert_log(conn: sqlite3.Connection, entry: dict) -> str:
    incoming = norm_ts_ms(entry.get("updated_at")) if entry.get("updated_at") else _now_ms()
    row, skip = _lww(conn, "food_logs", entry["id"], incoming)
    if skip:
        return skip
    vals = {k: entry.get(k) for k in LOG_FIELDS}
    vals["date"] = date.fromisoformat(vals["date"]).isoformat()
    vals["meal"] = vals["meal"] or calc.default_meal(vals["time"] or "12:00")
    if vals["food_id"] and get_food(conn, vals["food_id"]) is None:
        vals["food_id"] = None
    if vals["saved_meal_id"] and not conn.execute(
            "SELECT 1 FROM saved_meals WHERE id = ?", (vals["saved_meal_id"],)).fetchone():
        vals["saved_meal_id"] = None
    vals["protein_g"] = vals["protein_g"] or 0
    now = _now_ms()
    cols = [*LOG_FIELDS, "deleted", "client_updated_at"]
    conn.execute(
        f"""INSERT INTO food_logs (id, {', '.join(cols)}, created_at, updated_at)
            VALUES (?, {', '.join('?' for _ in cols)}, ?, ?)
            ON CONFLICT(id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)},
              updated_at = excluded.updated_at""",
        (entry["id"], *(vals[k] for k in LOG_FIELDS), int(bool(entry.get("deleted"))), incoming, now, now),
    )
    return "updated" if row is not None else "created"


def _log_out(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in ("id", *LOG_FIELDS)}
    d["deleted"] = bool(r["deleted"])
    d["updated_at"] = r["client_updated_at"]
    return d


def get_log(conn: sqlite3.Connection, log_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM food_logs WHERE id = ?", (log_id,)).fetchone()
    return _log_out(r) if r else None


def day_logs(conn: sqlite3.Connection, day: str) -> list[dict]:
    return [_log_out(r) for r in conn.execute(
        "SELECT * FROM food_logs WHERE date = ? AND deleted = 0 ORDER BY time, created_at", (day,))]


def _day_summary(logs: list[dict]) -> dict:
    """kcal / protein / carbs / fat, meals + snacks and the energy split of a day's logs."""
    t = calc.totals(logs)
    return {**t, **calc.macro_split(t), **calc.meal_count(logs), "entries": len(logs)}


EMPTY_DAY = {"kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "protein_pct": None, "carbs_pct": None,
             "fat_pct": None, "meals": 0, "snacks": 0, "eating_times": 0, "entries": 0}


def day_totals(conn: sqlite3.Connection, day: str) -> dict:
    return _day_summary(day_logs(conn, day))


def recent_logs(conn: sqlite3.Connection, days: int = PHONE_LOG_DAYS, today: date | None = None) -> list[dict]:
    """Logs of the last `days` days including deletions, for the phone to merge."""
    since = ((today or _today()) - timedelta(days=days)).isoformat()
    return [_log_out(r) for r in conn.execute(
        "SELECT * FROM food_logs WHERE date >= ? ORDER BY date, time", (since,))]


def daily_food(conn: sqlite3.Connection, start: date, end: date) -> dict[date, dict]:
    """Per-day totals, meal count and energy split between start and end (only days with entries)."""
    by_day: dict[date, list[dict]] = {}
    for r in conn.execute(
        "SELECT date, meal, time, kcal, protein_g, carbs_g, fat_g FROM food_logs "
        "WHERE deleted = 0 AND date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())):
        by_day.setdefault(date.fromisoformat(r["date"]), []).append(dict(r))
    return {d: _day_summary(logs) for d, logs in by_day.items()}


# --------------------------------------------------------------------- sync


def sync(conn: sqlite3.Connection, payload: dict) -> dict:
    """Everything the phone changed, in dependency order. Idempotent."""
    results = {"foods": {}, "saved_meals": {}, "logs": {}, "weights": {}}
    for f in payload.get("foods", []):
        results["foods"][f["id"]] = upsert_food(conn, f)
    for m in payload.get("saved_meals", []):
        results["saved_meals"][m["id"]] = upsert_saved_meal(conn, m)
    for entry in payload.get("logs", []):
        results["logs"][entry["id"]] = upsert_log(conn, entry)
    for w in payload.get("weights", []):
        results["weights"][f"{w['date']}|{w['slot']}"] = _upsert_weight_dict(conn, w)
    conn.commit()
    return {"results": results}


def bootstrap(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Everything the phone caches to log and view offline."""
    today = today or _today()
    return {
        "targets": targets(conn),
        "foods": list_foods(conn),
        "saved_meals": list_saved_meals(conn, today),
        "logs": recent_logs(conn, today=today),
        "weights": list_weights(conn),
        "weight": weight_analysis(conn, today),
        "weeks": weekly_summary(conn, today=today),
        "expenditure": expenditure_estimate(conn, today),
        "server_today": today.isoformat(),
    }


# --------------------------------------------------------------------- summaries


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def weekly_summary(conn: sqlite3.Connection, weeks: int = SUMMARY_WEEKS, today: date | None = None) -> list[dict]:
    """Mon–Sun weeks, newest first, from the first week with any data."""
    today = today or _today()
    t = targets(conn)
    first = conn.execute(
        "SELECT MIN(d) FROM (SELECT MIN(date) AS d FROM weight_entries UNION ALL "
        "SELECT MIN(date) FROM food_logs WHERE deleted = 0)"
    ).fetchone()[0]
    if not first:
        return []
    morning, _ = _weights_by_slot(conn)
    series = calc.trend_series(morning)
    start = max(_monday(date.fromisoformat(first)), _monday(today) - timedelta(weeks=weeks - 1))
    food = daily_food(conn, start, today)
    protein_target = t["protein_g"] or 0
    out = []
    wk = _monday(today)
    while wk >= start:
        end = wk + timedelta(days=6)
        days = [wk + timedelta(days=i) for i in range(7)]
        logged = [food[d] for d in days if d in food]
        # Trend change from the end of last week to this week's last weighing,
        # scaled to 7 days for a partial week (this week, or data starting mid-week).
        change = rate = None
        if series:
            first_day = max(wk - timedelta(days=1), series[0]["date"])
            last_day = min(end, series[-1]["date"])
            span = (last_day - first_day).days
            if span >= 3:
                change = calc.trend_change(series, first_day, last_day)
                rate = change / span * 7
        st = calc.status(rate, t["target_rate_kg_week"], None, None)
        out.append({
            "week_start": wk.isoformat(),
            "week_end": end.isoformat(),
            "food_days": len(logged),
            "avg_kcal": round(sum(x["kcal"] for x in logged) / len(logged)) if logged else None,
            "avg_protein_g": round(sum(x["protein_g"] for x in logged) / len(logged)) if logged else None,
            "avg_carbs_g": round(sum(x["carbs_g"] for x in logged) / len(logged)) if logged else None,
            "avg_fat_g": round(sum(x["fat_g"] for x in logged) / len(logged)) if logged else None,
            "avg_eating_times": round(sum(x["eating_times"] for x in logged) / len(logged), 1) if logged else None,
            "protein_days": sum(1 for x in logged if protein_target and x["protein_g"] >= protein_target),
            "weigh_ins": sum(1 for d in days if d in morning),
            "trend_change_kg": round(change, 2) if change is not None else None,
            "rate_kg_week": round(rate, 2) if rate is not None else None,
            "status": st,
            "status_label": calc.STATUS_LABEL[st],
        })
        wk -= timedelta(weeks=1)
    return out


def expenditure_estimate(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Energy expenditure from intake and the trend over the last 28 complete
    days -- only with >= 14 days that have both a morning weight and a fully
    logged food day. Otherwise advice from the weight trend alone."""
    today = today or _today()
    t = targets(conn)
    target_rate = t["target_rate_kg_week"]
    end = today - timedelta(days=1)
    start = end - timedelta(days=EXPENDITURE_WINDOW_DAYS - 1)
    morning, _ = _weights_by_slot(conn)
    series = calc.trend_series(morning)
    food = daily_food(conn, start, end)
    threshold = FOOD_DAY_SHARE * (t["kcal_min"] or 0)
    food_days = {d: v for d, v in food.items() if v["kcal"] >= threshold}
    both = sorted(d for d in food_days if d in morning)
    out = {"days_with_both": len(both), "days_needed": EXPENDITURE_MIN_DAYS, "window_days": EXPENDITURE_WINDOW_DAYS,
           "food_day_min_kcal": round(threshold), "basis": None, "expenditure_kcal": None,
           "avg_intake_kcal": None, "rate_kg_week": None, "suggested_change_kcal": None,
           "suggested_intake_kcal": None, "advice": None}
    if len(both) >= EXPENDITURE_MIN_DAYS:
        first, last = both[0], both[-1]
        days = (last - first).days
        change = calc.trend_change(series, first, last)
        if days > 0 and change is not None:
            intake = [food_days[d]["kcal"] for d in food_days if first <= d <= last]
            avg = sum(intake) / len(intake)
            exp = calc.expenditure(avg, change, days)
            rate = change / days * 7
            adj = calc.kcal_adjustment(target_rate, rate)
            out.update(basis="intake_and_weight", expenditure_kcal=round(exp), avg_intake_kcal=round(avg),
                       rate_kg_week=round(rate, 2), suggested_change_kcal=round(adj),
                       suggested_intake_kcal=round(exp + target_rate * calc.KCAL_PER_KG / 7),
                       advice=calc.advice_text(rate, target_rate))
            return out
    rate = calc.weekly_rate(series)
    if rate is not None:
        out.update(basis="weight_only", rate_kg_week=round(rate, 2),
                   suggested_change_kcal=round(calc.kcal_adjustment(target_rate, rate)),
                   advice=calc.advice_text(rate, target_rate))
    return out


def last_days(conn: sqlite3.Connection, n: int = 7, today: date | None = None) -> list[dict]:
    today = today or _today()
    start = today - timedelta(days=n - 1)
    food = daily_food(conn, start, today)
    return [{"date": (start + timedelta(days=i)).isoformat(),
             **(food.get(start + timedelta(days=i)) or EMPTY_DAY)}
            for i in range(n)]


def summary(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Everything the Nutrition tab shows."""
    today = today or _today()
    return {
        "targets": targets(conn),
        "weight": weight_analysis(conn, today),
        "today": {"date": today.isoformat(), **day_totals(conn, today.isoformat())},
        "last_days": last_days(conn, 14, today),
        "weeks": weekly_summary(conn, today=today),
        "expenditure": expenditure_estimate(conn, today),
    }


def dashboard_summary(conn: sqlite3.Connection, today: date | None = None) -> dict:
    today = today or _today()
    w = weight_analysis(conn, today)
    t = targets(conn)
    d = day_totals(conn, today.isoformat())
    return {
        "trend_kg": w["trend_kg"], "rate_kg_week": w["rate_kg_week"], "status": w["status"],
        "status_label": w["status_label"], "protein_today_g": d["protein_g"], "kcal_today": d["kcal"],
        "protein_target_g": t["protein_g"], "kcal_min": t["kcal_min"], "kcal_max": t["kcal_max"],
        "carbs_today_g": d["carbs_g"], "fat_today_g": d["fat_g"],
        "meals_today": d["meals"], "snacks_today": d["snacks"],
        "checkin": latest_checkin(conn, today),
    }


def latest_checkin(conn: sqlite3.Connection, today: date | None = None) -> dict | None:
    """The newest weigh-in with its score / note, plus the average score of the
    last 7 days -- the dashboard's "How you feel" tile."""
    today = today or _today()
    row = conn.execute(
        "SELECT date, slot, weight_kg, score, note, time FROM weight_entries "
        "ORDER BY date DESC, CASE slot WHEN 'evening' THEN 0 ELSE 1 END LIMIT 1").fetchone()
    if row is None:
        return None
    last_score = conn.execute(
        "SELECT date, slot, score FROM weight_entries WHERE score IS NOT NULL "
        "ORDER BY date DESC, CASE slot WHEN 'evening' THEN 0 ELSE 1 END LIMIT 1").fetchone()
    avg = conn.execute("SELECT AVG(score), COUNT(score) FROM weight_entries WHERE score IS NOT NULL AND date > ?",
                       ((today - timedelta(days=7)).isoformat(),)).fetchone()
    return {
        **dict(row),
        "last_score": dict(last_score) if last_score else None,
        "avg_score_7d": round(avg[0], 1) if avg[0] is not None else None,
        "scores_7d": avg[1],
    }


def coach_summary(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Compact view for the coach briefing: weight trend, rate, goal status and
    the last 7 days of nutrition."""
    today = today or _today()
    w = weight_analysis(conn, today)
    has_food = conn.execute("SELECT 1 FROM food_logs WHERE deleted = 0 LIMIT 1").fetchone() is not None
    if w["trend_kg"] is None and not has_food:
        return {}
    exp = expenditure_estimate(conn, today)
    return {
        "targets": targets(conn),
        "trend_kg": w["trend_kg"], "trend_date": w["trend_date"], "rate_kg_week": w["rate_kg_week"],
        "status": w["status_label"], "projected_date": w["projected_date"],
        "evening_diff_kg": w["evening_diff_kg"],
        "last_7_days": last_days(conn, 7, today) if has_food else [],
        "expenditure_kcal": exp["expenditure_kcal"], "advice": exp["advice"],
    }
