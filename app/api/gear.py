"""Gear log: boards, sails, fins, foils, wings, kites -- with usage totals
computed from the sessions they're attached to."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.analysis import service
from app.api.deps import get_db

router = APIRouter(prefix="/api/gear", tags=["gear"])


class GearIn(BaseModel):
    kind: str | None = None
    name: str | None = Field(None, max_length=120)
    brand: str | None = Field(None, max_length=120)
    size: str | None = Field(None, max_length=60)
    notes: str | None = Field(None, max_length=2000)
    retired: bool | None = None


@router.get("")
def list_gear(conn: sqlite3.Connection = Depends(get_db)):
    return service.list_gear(conn)


@router.post("")
def create_gear(item: GearIn, conn: sqlite3.Connection = Depends(get_db)):
    try:
        gid = service.upsert_gear(conn, item.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return next(g for g in service.list_gear(conn) if g["id"] == gid)


@router.patch("/{gear_id}")
def edit_gear(gear_id: int, item: GearIn, conn: sqlite3.Connection = Depends(get_db)):
    if not conn.execute("SELECT 1 FROM gear WHERE id = ?", (gear_id,)).fetchone():
        raise HTTPException(404, "Gear not found")
    try:
        service.upsert_gear(conn, item.model_dump(exclude_unset=True), gear_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return next(g for g in service.list_gear(conn) if g["id"] == gear_id)


@router.delete("/{gear_id}")
def delete_gear(gear_id: int, conn: sqlite3.Connection = Depends(get_db)):
    if not service.delete_gear(conn, gear_id):
        raise HTTPException(404, "Gear not found")
    return {"deleted": True}
