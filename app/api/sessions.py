from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_db
from app.db import get_session, list_exercise_sets, list_sessions

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
def get_sessions(
    start: str | None = None,
    end: str | None = None,
    type: str | None = None,
    limit: int = 100,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_db),
):
    rows = list_sessions(conn, start=start, end=end, activity_type=type, limit=limit, offset=offset)
    return [dict(r) for r in rows]


@router.get("/{activity_id}")
def get_one_session(activity_id: int, conn: sqlite3.Connection = Depends(get_db)):
    row = get_session(conn, activity_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    result = dict(row)
    if result["activity_type"] == "strength_training":
        result["exercise_sets"] = [dict(r) for r in list_exercise_sets(conn, activity_id)]
    return result
