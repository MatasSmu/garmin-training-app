from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_db
from app.db import insert_journal_entry, list_journal_entries

router = APIRouter(prefix="/api/journal", tags=["journal"])


class JournalCreate(BaseModel):
    date: str
    text: str
    activity_id: int | None = None


@router.get("")
def get_journal(
    start: str | None = None, end: str | None = None, conn: sqlite3.Connection = Depends(get_db)
):
    return [dict(r) for r in list_journal_entries(conn, start=start, end=end)]


@router.post("")
def create_journal_entry(entry: JournalCreate, conn: sqlite3.Connection = Depends(get_db)):
    entry_id = insert_journal_entry(conn, entry.date, entry.text, entry.activity_id)
    conn.commit()
    return {"id": entry_id, **entry.model_dump()}
