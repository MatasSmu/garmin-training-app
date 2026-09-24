"""Session analysis API: GPS tracks, their analyses, and cross-session views."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.analysis import service
from app.analysis.settings import DEFAULTS, SPORTS
from app.api.deps import get_db
from app.sailing import reports as sail_reports
from app.sailing.config import PARTS as SAIL_PARTS
from app.sailing.config import SPORTS as SAIL_SPORTS

router = APIRouter(prefix="/api/tracks", tags=["tracks"])

MAX_UPLOAD = 25 * 1024 * 1024


class TrackPatch(BaseModel):
    title: str | None = None
    spot: str | None = None
    notes: str | None = None
    sport: str | None = None
    settings: dict[str, Any] | None = None     # merged; a null value resets that key
    conditions: dict[str, Any] | None = None   # manual conditions
    clear_conditions: bool = False
    gear_ids: list[int] | None = None


class Conditions(BaseModel):
    wind_kn: float | None = Field(None, ge=0, le=100)
    gust_kn: float | None = Field(None, ge=0, le=150)
    wind_dir: float | None = Field(None, ge=0, lt=360)
    air_c: float | None = Field(None, ge=-40, le=50)
    water_c: float | None = Field(None, ge=-5, le=40)


def _filters(sport, spot, gear_id, wind_min, wind_max, wind_dir, dir_tol, start, end) -> dict:
    return {"sport": sport or None, "spot": spot or None, "gear_id": gear_id, "wind_min": wind_min,
            "wind_max": wind_max, "wind_dir": wind_dir, "dir_tol": dir_tol, "start": start or None,
            "end": end or None}


@router.get("")
def list_tracks(sport: str | None = None, spot: str | None = None, gear_id: int | None = None,
                wind_min: float | None = None, wind_max: float | None = None, wind_dir: float | None = None,
                dir_tol: float = 30.0, start: str | None = None, end: str | None = None,
                conn: sqlite3.Connection = Depends(get_db)):
    return service.list_rows(conn, **_filters(sport, spot, gear_id, wind_min, wind_max, wind_dir, dir_tol, start, end))


@router.post("")
async def upload_track(file: UploadFile = File(...), conn: sqlite3.Connection = Depends(get_db)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (25 MB max)")
    try:
        tid, created = service.import_bytes(conn, data, file.filename or "upload.fit")
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"id": tid, "created": created}


@router.post("/garmin/{activity_id}")
def import_from_garmin(activity_id: int, conn: sqlite3.Connection = Depends(get_db)):
    """Download the original FIT for a synced Garmin activity and analyse it."""
    existing = conn.execute("SELECT id FROM tracks WHERE activity_id = ?", (activity_id,)).fetchone()
    if existing:
        return {"id": existing["id"], "created": False}
    from app.ingest.garmin_client import GarminUnavailable, fetch_original_file

    try:
        data, name = fetch_original_file(activity_id)
    except GarminUnavailable as e:
        raise HTTPException(503, str(e)) from e
    try:
        tid, created = service.import_bytes(conn, data, name, activity_id=activity_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"id": tid, "created": created}


@router.get("/options")
def options(conn: sqlite3.Connection = Depends(get_db)):
    """Everything the UI needs for filters and settings forms."""
    return {
        "sports": [{"key": k, "label": v[0], "state": v[1], "on_kn": v[2], "off_kn": v[3]} for k, v in SPORTS.items()],
        "spots": service.spots(conn),
        "gear": service.list_gear(conn),
        "gear_kinds": list(service.GEAR_KINDS),
        "setups": service.list_setups(conn),
        "sail_parts": list(SAIL_PARTS),
        "sail_sports": list(SAIL_SPORTS),
        "defaults": DEFAULTS,
    }


@router.get("/polar")
def aggregate_polar(sport: str = "windfoil", spot: str | None = None, gear_id: int | None = None,
                    wind_min: float | None = None, wind_max: float | None = None,
                    ids: str | None = None, conn: sqlite3.Connection = Depends(get_db)):
    id_list = [int(x) for x in ids.split(",") if x.strip()] if ids else None
    polar, _, n = service.personal_polar(conn, sport, gear_id=gear_id, spot=spot or None,
                                         wind_min=wind_min, wind_max=wind_max, track_ids=id_list)
    return {"polar": polar, "sessions": n}


@router.get("/trends")
def get_trends(sport: str | None = None, spot: str | None = None, gear_id: int | None = None,
               wind_min: float | None = None, wind_max: float | None = None, wind_dir: float | None = None,
               dir_tol: float = 30.0, start: str | None = None, end: str | None = None,
               conn: sqlite3.Connection = Depends(get_db)):
    return service.trends(conn, **_filters(sport, spot, gear_id, wind_min, wind_max, wind_dir, dir_tol, start, end))


@router.get("/compare")
def compare(ids: str, conn: sqlite3.Connection = Depends(get_db)):
    id_list = [int(x) for x in ids.split(",") if x.strip()][:4]
    if not id_list:
        raise HTTPException(422, "ids required")
    return service.compare(conn, id_list)


@router.get("/speedmap")
def speedmap(spot: str | None = None, sport: str | None = None, wind_dir: float | None = None,
             dir_tol: float = 30.0, ids: str | None = None, conn: sqlite3.Connection = Depends(get_db)):
    id_list = [int(x) for x in ids.split(",") if x.strip()] if ids else None
    return service.speed_map(conn, spot=spot or None, sport=sport or None, wind_dir=wind_dir,
                             dir_tol=dir_tol, ids=id_list)


def _row_or_404(conn, track_id: int):
    t = service.get_track_row(conn, track_id)
    if t is None:
        raise HTTPException(404, "Track not found")
    return t


@router.get("/{track_id}")
def get_track(track_id: int, conn: sqlite3.Connection = Depends(get_db)):
    return service.track_meta(conn, _row_or_404(conn, track_id))


@router.get("/{track_id}/analysis")
def get_analysis(track_id: int, force: bool = False, conn: sqlite3.Connection = Depends(get_db)):
    t = _row_or_404(conn, track_id)
    result = service.get_analysis(conn, track_id, force=force)
    return {"track": service.track_meta(conn, service.get_track_row(conn, t["id"])), **result}


@router.patch("/{track_id}")
def patch_track(track_id: int, patch: TrackPatch, conn: sqlite3.Connection = Depends(get_db)):
    _row_or_404(conn, track_id)
    body = patch.model_dump(exclude_unset=True)
    if "conditions" in body and body["conditions"] is not None:
        body["conditions"] = Conditions(**body["conditions"]).model_dump()
    if body.pop("clear_conditions", False):
        body["conditions"] = None
    try:
        service.update_track(conn, track_id, body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if body.get("gear_ids"):
        # first time the gear is filled in: analyse once in the background
        sail_reports.maybe_auto_analyze(conn, track_id)
    return service.track_meta(conn, service.get_track_row(conn, track_id))


@router.post("/{track_id}/conditions/fetch")
def fetch_conditions(track_id: int, conn: sqlite3.Connection = Depends(get_db)):
    _row_or_404(conn, track_id)
    cond = service.refresh_conditions(conn, track_id)
    if cond is None:
        raise HTTPException(502, "Open-Meteo had no data for this time and place (or is unreachable)")
    return cond


@router.get("/{track_id}/file")
def download_file(track_id: int, conn: sqlite3.Connection = Depends(get_db)):
    t = _row_or_404(conn, track_id)
    return FileResponse(service.TRACKS_DIR / t["file_path"], filename=t["file_name"])


@router.delete("/{track_id}")
def delete_track(track_id: int, conn: sqlite3.Connection = Depends(get_db)):
    if not service.delete_track(conn, track_id):
        raise HTTPException(404, "Track not found")
    return {"deleted": True}
