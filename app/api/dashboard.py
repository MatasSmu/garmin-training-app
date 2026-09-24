from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app.api.deps import get_db
from app.coach.metrics import (
    current_streak,
    days_since_rest,
    recent_journal,
    sleep_trend,
    training_load,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary")
def get_summary(conn: sqlite3.Connection = Depends(get_db)):
    return {
        "load_7d": training_load(conn, 7),
        "load_28d": training_load(conn, 28),
        "sleep_trend": sleep_trend(conn),
        "days_since_rest": days_since_rest(conn),
        "current_streak": current_streak(conn),
        "recent_journal": recent_journal(conn, 7),
    }
