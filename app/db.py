"""SQLite connection helper and query/upsert functions."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import DB_PATH

SESSION_COLUMNS = [
    "activity_id",
    "activity_type",
    "activity_name",
    "start_time_local",
    "start_date",
    "duration_s",
    "distance_m",
    "elevation_gain_m",
    "avg_speed_mps",
    "max_speed_mps",
    "avg_hr",
    "max_hr",
    "calories",
    "location",
    "raw_json",
    "updated_at",
]


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    schema_path = Path(__file__).with_name("schema.sql")
    conn.executescript(schema_path.read_text())
    _migrate_coach_conversations(conn)
    _migrate_gear(conn)
    _migrate_gym_sessions(conn)
    conn.commit()


def _migrate_coach_conversations(conn: sqlite3.Connection) -> None:
    """Messages saved before conversations existed get grouped into one
    'Earlier chat' so nothing is lost."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(coach_messages)")}
    if "conversation_id" not in cols:
        conn.execute(
            "ALTER TABLE coach_messages ADD COLUMN conversation_id INTEGER "
            "REFERENCES coach_conversations(id) ON DELETE CASCADE"
        )
    first, last = conn.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM coach_messages WHERE conversation_id IS NULL"
    ).fetchone()
    if first:
        cur = conn.execute(
            "INSERT INTO coach_conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            ("Earlier chat", first, last),
        )
        conn.execute(
            "UPDATE coach_messages SET conversation_id = ? WHERE conversation_id IS NULL",
            (cur.lastrowid,),
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coach_messages_conv ON coach_messages(conversation_id, id)"
    )


def _migrate_gym_sessions(conn: sqlite3.Connection) -> None:
    """Gym tables from before the Garmin time check get its column (additive only)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(gym_sessions)")}
    if "garmin_time_dismissed" not in cols:
        conn.execute("ALTER TABLE gym_sessions ADD COLUMN garmin_time_dismissed INTEGER")


def _migrate_gear(conn: sqlite3.Connection) -> None:
    """Gear tables from before iQFOIL part codes get the code column and the
    wider kind CHECK. SQLite can't alter a CHECK, so the table is rebuilt
    with foreign keys off (renaming would repoint track_gear at the copy)."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'gear'").fetchone()[0]
    if "front_wing" in sql:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        schema = Path(__file__).with_name("schema.sql").read_text()
        start = schema.index("CREATE TABLE IF NOT EXISTS gear (")
        create = schema[start:schema.index(");", start) + 2].replace("gear (", "gear_new (", 1)
        conn.execute(create)
        cols = "id, kind, name, brand, size, notes, retired, created_at, updated_at"
        conn.execute(f"INSERT INTO gear_new ({cols}) SELECT {cols} FROM gear")
        conn.execute("DROP TABLE gear")
        conn.execute("ALTER TABLE gear_new RENAME TO gear")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_session(conn: sqlite3.Connection, row: dict) -> None:
    cols = SESSION_COLUMNS
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "activity_id")
    sql = (
        f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(activity_id) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row.get(c) for c in cols])


def upsert_daily_metric_partial(conn: sqlite3.Connection, date_str: str, values: dict) -> None:
    """Insert or merge a partial set of daily_metrics columns for one date.

    Different ingestion calls (sleep, HRV, stress, ...) each populate a
    disjoint subset of columns, so this only touches the keys given.
    """
    if not values:
        return
    cols = list(values.keys())
    all_cols = ["date", "updated_at", *cols]
    placeholders = ", ".join("?" for _ in all_cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in (*cols, "updated_at"))
    sql = (
        f"INSERT INTO daily_metrics ({', '.join(all_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(date) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [date_str, _now(), *(values[c] for c in cols)])


def replace_exercise_sets(conn: sqlite3.Connection, activity_id: int, rows: list[dict]) -> None:
    conn.execute("DELETE FROM strength_exercise_sets WHERE activity_id = ?", (activity_id,))
    for row in rows:
        conn.execute(
            """INSERT INTO strength_exercise_sets
               (activity_id, set_index, exercise_name, category, reps, weight_kg, duration_s, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                activity_id,
                row["set_index"],
                row.get("exercise_name"),
                row.get("category"),
                row.get("reps"),
                row.get("weight_kg"),
                row.get("duration_s"),
                row["raw_json"],
            ),
        )


def insert_journal_entry(
    conn: sqlite3.Connection, date_str: str, text: str, activity_id: int | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO journal_entries (date, activity_id, text, created_at) VALUES (?, ?, ?, ?)",
        (date_str, activity_id, text, _now()),
    )
    return cur.lastrowid


def list_journal_entries(
    conn: sqlite3.Connection, start: str | None = None, end: str | None = None
) -> list[sqlite3.Row]:
    query = "SELECT * FROM journal_entries"
    clauses, params = [], []
    if start:
        clauses.append("date >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY date DESC, id DESC"
    return conn.execute(query, params).fetchall()


def list_sessions(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
    activity_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    query = "SELECT * FROM sessions"
    clauses, params = [], []
    if start:
        clauses.append("start_date >= ?")
        params.append(start)
    if end:
        clauses.append("start_date <= ?")
        params.append(end)
    if activity_type:
        clauses.append("activity_type = ?")
        params.append(activity_type)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY start_date DESC, start_time_local DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return conn.execute(query, params).fetchall()


def get_session(conn: sqlite3.Connection, activity_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sessions WHERE activity_id = ?", (activity_id,)
    ).fetchone()


def list_exercise_sets(conn: sqlite3.Connection, activity_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM strength_exercise_sets WHERE activity_id = ? ORDER BY set_index",
        (activity_id,),
    ).fetchall()


def create_conversation(conn: sqlite3.Connection, title: str) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO coach_conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
        (title, now, now),
    )
    return cur.lastrowid


def get_conversation(conn: sqlite3.Connection, conversation_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM coach_conversations WHERE id = ?", (conversation_id,)
    ).fetchone()


def list_conversations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.id, c.title, c.created_at, c.updated_at, COUNT(m.id) AS message_count
           FROM coach_conversations c
           LEFT JOIN coach_messages m ON m.conversation_id = c.id
           GROUP BY c.id ORDER BY c.updated_at DESC"""
    ).fetchall()


def delete_conversation(conn: sqlite3.Connection, conversation_id: int) -> None:
    conn.execute("DELETE FROM coach_messages WHERE conversation_id = ?", (conversation_id,))
    conn.execute("DELETE FROM coach_conversations WHERE id = ?", (conversation_id,))


def insert_coach_message(
    conn: sqlite3.Connection, conversation_id: int, role: str, content: str
) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO coach_messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (conversation_id, role, content, now),
    )
    conn.execute(
        "UPDATE coach_conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
    )
    return cur.lastrowid


def list_coach_messages(
    conn: sqlite3.Connection, conversation_id: int, limit: int = 40
) -> list[sqlite3.Row]:
    """Most recent messages of one conversation, oldest first -- ready to feed
    straight to the LLM or render top-to-bottom in the chat window."""
    rows = conn.execute(
        "SELECT role, content, created_at FROM coach_messages "
        "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (conversation_id, limit),
    ).fetchall()
    return list(reversed(rows))


def list_daily_metrics(
    conn: sqlite3.Connection, start: str | None = None, end: str | None = None
) -> list[sqlite3.Row]:
    query = "SELECT * FROM daily_metrics"
    clauses, params = [], []
    if start:
        clauses.append("date >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY date DESC"
    return conn.execute(query, params).fetchall()


CALENDAR_FIELDS = ["kind", "date", "end_date", "time", "title", "reason", "notes"]


def list_calendar_events(
    conn: sqlite3.Connection, start: str | None = None, end: str | None = None
) -> list[sqlite3.Row]:
    """Events overlapping [start, end]; multi-day events count if any day falls inside."""
    query = "SELECT * FROM calendar_events"
    clauses, params = [], []
    if start:
        clauses.append("COALESCE(end_date, date) >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY date, time IS NULL, time, id"
    return conn.execute(query, params).fetchall()


def get_calendar_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM calendar_events WHERE id = ?", (event_id,)).fetchone()


def insert_calendar_event(conn: sqlite3.Connection, values: dict) -> int:
    now = _now()
    cols = [*CALENDAR_FIELDS, "created_at", "updated_at"]
    cur = conn.execute(
        f"INSERT INTO calendar_events ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [*(values.get(c) for c in CALENDAR_FIELDS), now, now],
    )
    return cur.lastrowid


def update_calendar_event(conn: sqlite3.Connection, event_id: int, values: dict) -> bool:
    sets = ", ".join(f"{c} = ?" for c in CALENDAR_FIELDS)
    cur = conn.execute(
        f"UPDATE calendar_events SET {sets}, updated_at = ? WHERE id = ?",
        [*(values.get(c) for c in CALENDAR_FIELDS), _now(), event_id],
    )
    return cur.rowcount > 0


def delete_calendar_event(conn: sqlite3.Connection, event_id: int) -> bool:
    cur = conn.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
    return cur.rowcount > 0
