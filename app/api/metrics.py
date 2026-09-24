from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app.api.deps import get_db
from app.db import list_daily_metrics

router = APIRouter(prefix="/api/daily-metrics", tags=["metrics"])


@router.get("")
def get_daily_metrics(
    start: str | None = None, end: str | None = None, conn: sqlite3.Connection = Depends(get_db)
):
    return [dict(r) for r in list_daily_metrics(conn, start=start, end=end)]
