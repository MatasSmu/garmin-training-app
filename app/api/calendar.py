from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_db
from app.calendar_schema import CalendarEvent
from app.db import (
    delete_calendar_event,
    get_calendar_event,
    insert_calendar_event,
    list_calendar_events,
    update_calendar_event,
)

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


@router.get("")
def get_events(
    start: str | None = None, end: str | None = None, conn: sqlite3.Connection = Depends(get_db)
):
    return [dict(r) for r in list_calendar_events(conn, start=start, end=end)]


@router.post("")
def create_event(event: CalendarEvent, conn: sqlite3.Connection = Depends(get_db)):
    event_id = insert_calendar_event(conn, event.model_dump())
    conn.commit()
    return dict(get_calendar_event(conn, event_id))


@router.put("/{event_id}")
def edit_event(event_id: int, event: CalendarEvent, conn: sqlite3.Connection = Depends(get_db)):
    if not update_calendar_event(conn, event_id, event.model_dump()):
        raise HTTPException(status_code=404, detail="Event not found")
    conn.commit()
    return dict(get_calendar_event(conn, event_id))


@router.delete("/{event_id}")
def remove_event(event_id: int, conn: sqlite3.Connection = Depends(get_db)):
    if not delete_calendar_event(conn, event_id):
        raise HTTPException(status_code=404, detail="Event not found")
    conn.commit()
    return {"deleted": event_id}
