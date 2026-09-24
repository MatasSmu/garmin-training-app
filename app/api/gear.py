"""Gear inventory: iQFOIL parts (board, front/back wing, foil mast,
fuselage, sail, mast, boom -- identified by code) plus fins, foils, wings and
kites, with usage totals from the sessions they're attached to; and saved
setups (sets of parts applied to a session in one go)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.analysis import service
from app.api.deps import get_db

router = APIRouter(prefix="/api/gear", tags=["gear"])


class GearIn(BaseModel):
    kind: str | None = None
    code: str | None = Field(None, max_length=60)
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


class SetupIn(BaseModel):
    name: str | None = Field(None, max_length=120)
    notes: str | None = Field(None, max_length=2000)
    gear_ids: list[int] | None = None


@router.get("/setups")
def list_setups(conn: sqlite3.Connection = Depends(get_db)):
    return service.list_setups(conn)


@router.post("/setups")
def create_setup(item: SetupIn, conn: sqlite3.Connection = Depends(get_db)):
    try:
        sid = service.save_setup(conn, item.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return next(x for x in service.list_setups(conn) if x["id"] == sid)


@router.patch("/setups/{setup_id}")
def edit_setup(setup_id: int, item: SetupIn, conn: sqlite3.Connection = Depends(get_db)):
    if not conn.execute("SELECT 1 FROM gear_setups WHERE id = ?", (setup_id,)).fetchone():
        raise HTTPException(404, "Setup not found")
    try:
        service.save_setup(conn, item.model_dump(exclude_unset=True), setup_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return next(x for x in service.list_setups(conn) if x["id"] == setup_id)


@router.delete("/setups/{setup_id}")
def delete_setup(setup_id: int, conn: sqlite3.Connection = Depends(get_db)):
    if not service.delete_setup(conn, setup_id):
        raise HTTPException(404, "Setup not found")
    return {"deleted": True}


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
