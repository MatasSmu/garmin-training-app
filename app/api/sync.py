"""Sync button: start an on-demand Garmin sync and report its progress."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app.api.deps import get_db
from app.ingest import job

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.get("")
def sync_status(conn: sqlite3.Connection = Depends(get_db)):
    return job.status(conn)


@router.post("")
def start_sync(conn: sqlite3.Connection = Depends(get_db)):
    started = job.start()
    return {**job.status(conn), "started": started}
