"""Gym tracker API (/api/gym) and the offline logger page (/gym/)."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from app.api.deps import get_db
from app.gym import store

router = APIRouter(tags=["gym"])

GYM_STATIC = Path(__file__).resolve().parent.parent / "static" / "gym"
MAX_SETS_PER_SESSION = 400


def _iso(v: str | None) -> str | None:
    if v in (None, ""):
        return None
    datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v


class LocationIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name is empty")
        return v.strip()


class SetIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    exercise_id: str
    set_number: int = Field(ge=1, le=50)
    reps: int | None = Field(default=None, ge=0, le=1000)
    weight_kg: float | None = Field(default=None, ge=0, le=1000)
    duration_sec: int | None = Field(default=None, ge=0, le=36000)
    rpe: int | None = Field(default=None, ge=1, le=10)
    completed_as_planned: bool = False
    done_at: str | None = None

    @field_validator("done_at")
    @classmethod
    def _ts(cls, v: str | None) -> str | None:
        return _iso(v)


class SessionIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    plan_day_id: int | None = None
    location_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    notes: str | None = Field(default=None, max_length=4000)
    updated_at: str
    sets: list[SetIn] = Field(default_factory=list, max_length=MAX_SETS_PER_SESSION)

    @field_validator("started_at", "ended_at", "updated_at")
    @classmethod
    def _ts(cls, v: str | None) -> str | None:
        return _iso(v)


class SyncIn(BaseModel):
    locations: list[LocationIn] = Field(default_factory=list)
    sessions: list[SessionIn] = Field(default_factory=list)


class SessionEdit(BaseModel):
    location_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    notes: str | None = Field(default=None, max_length=4000)
    plan_day_id: int | None = None

    @field_validator("started_at", "ended_at")
    @classmethod
    def _ts(cls, v: str | None) -> str | None:
        return _iso(v)


class NewLocation(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=80)


# ------------------------------------------------------------------ API


@router.get("/api/gym/bootstrap")
def get_bootstrap(conn: sqlite3.Connection = Depends(get_db)):
    return store.bootstrap(conn)


@router.post("/api/gym/sync")
def post_sync(payload: SyncIn, conn: sqlite3.Connection = Depends(get_db)):
    return store.sync(conn, payload.model_dump())


@router.get("/api/gym/sessions")
def get_sessions(limit: int = 100, since: str | None = None, conn: sqlite3.Connection = Depends(get_db)):
    return store.list_sessions(conn, limit=max(1, min(limit, 1000)), since=since)


@router.get("/api/gym/sessions/{session_id}")
def get_session(session_id: str, conn: sqlite3.Connection = Depends(get_db)):
    detail = store.session_detail(conn, session_id)
    if detail is None:
        raise HTTPException(404, "session not found")
    return detail


@router.patch("/api/gym/sessions/{session_id}")
def patch_session(session_id: str, payload: SessionEdit, conn: sqlite3.Connection = Depends(get_db)):
    try:
        found = store.update_session(conn, session_id, payload.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not found:
        raise HTTPException(404, "session not found")
    return store.session_detail(conn, session_id)


@router.get("/api/gym/locations")
def get_locations(conn: sqlite3.Connection = Depends(get_db)):
    return store.locations(conn)


@router.post("/api/gym/locations")
def post_location(payload: NewLocation, conn: sqlite3.Connection = Depends(get_db)):
    loc_id = payload.id or str(uuid.uuid4())
    store.upsert_location(conn, loc_id, payload.name)
    conn.commit()
    return {"id": loc_id, "name": payload.name.strip()}


@router.get("/api/gym/exercises")
def get_exercises(conn: sqlite3.Connection = Depends(get_db)):
    return store.exercise_list(conn)


@router.get("/api/gym/exercises/{exercise_id}/history")
def get_exercise_history(exercise_id: str, conn: sqlite3.Connection = Depends(get_db)):
    out = store.exercise_history(conn, exercise_id)
    if out is None:
        raise HTTPException(404, "exercise not found")
    return out


@router.get("/api/gym/volume")
def get_volume(conn: sqlite3.Connection = Depends(get_db)):
    return store.weekly_volume(conn)


# ------------------------------------------------------------------ logger page (PWA)
# The service worker is served from /gym/ so its scope covers the page; the
# page and worker are tiny and cached by the worker for offline use.


@router.get("/gym", include_in_schema=False)
def gym_redirect():
    return RedirectResponse("/gym/")


@router.get("/gym/", include_in_schema=False)
def gym_page():
    return FileResponse(GYM_STATIC / "index.html", media_type="text/html")


@router.get("/gym/sw.js", include_in_schema=False)
def gym_service_worker():
    return FileResponse(GYM_STATIC / "sw.js", media_type="text/javascript")


@router.get("/gym/manifest.webmanifest", include_in_schema=False)
def gym_manifest():
    return FileResponse(GYM_STATIC / "manifest.webmanifest", media_type="application/manifest+json")
