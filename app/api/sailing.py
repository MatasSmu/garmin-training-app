"""iQFOIL analysis API: per-session baseline comparison and DeepSeek
analysis, baselines, weekly summaries, month/season reviews, token usage."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_db
from app.sailing import deepseek, packet, reports
from app.sailing.config import PART_LABELS, PARTS

router = APIRouter(prefix="/api/sailing", tags=["sailing"])


def _track_or_404(conn, track_id: int) -> None:
    if not conn.execute("SELECT 1 FROM tracks WHERE id = ?", (track_id,)).fetchone():
        raise HTTPException(404, "Track not found")


@router.get("/config")
def get_config(conn: sqlite3.Connection = Depends(get_db)):
    return {**reports.baselines_overview(conn), "parts": [{"key": p, "label": PART_LABELS[p]} for p in PARTS]}


@router.get("/baselines")
def get_baselines(conn: sqlite3.Connection = Depends(get_db)):
    return reports.baselines_overview(conn)


@router.get("/sessions/{track_id}")
def get_session(track_id: int, conn: sqlite3.Connection = Depends(get_db)):
    _track_or_404(conn, track_id)
    return reports.session_state(conn, track_id)


@router.get("/sessions/{track_id}/packet")
def get_packet(track_id: int, conn: sqlite3.Connection = Depends(get_db)):
    """The exact packet an analysis would send now (no model call)."""
    from app.analysis import service

    _track_or_404(conn, track_id)
    try:
        pk = packet.build(conn, track_id, point_of_sail=service.get_analysis(conn, track_id).get("point_of_sail"))
    except packet.NotEligible as e:
        raise HTTPException(422, str(e)) from e
    return {"packet": pk, "tokens": packet.estimate_tokens(pk)}


@router.post("/sessions/{track_id}/analyze")
def analyze(track_id: int, conn: sqlite3.Connection = Depends(get_db)):
    """Run (or re-run) the DeepSeek analysis now. Only ever triggered by the
    athlete; an existing analysis is kept if the new call fails."""
    _track_or_404(conn, track_id)
    if not reports.claim(track_id):
        raise HTTPException(409, "This session is already being analysed.")
    try:
        reports.analyze_session(conn, track_id)
    except packet.NotEligible as e:
        raise HTTPException(422, str(e)) from e
    except deepseek.NoApiKey as e:
        raise HTTPException(503, str(e)) from e
    except deepseek.LLMError as e:
        raise HTTPException(502, str(e)) from e
    finally:
        reports.release(track_id)
    return reports.session_state(conn, track_id)


@router.get("/weekly")
def weekly(conn: sqlite3.Connection = Depends(get_db)):
    return reports.list_weekly(conn)


class WeekIn(BaseModel):
    week: date | None = None   # any day of the week; default = this week


@router.post("/weekly")
def make_weekly(body: WeekIn, conn: sqlite3.Connection = Depends(get_db)):
    try:
        return reports.generate_weekly(conn, body.week or date.today())
    except packet.NotEligible as e:
        raise HTTPException(422, str(e)) from e
    except deepseek.NoApiKey as e:
        raise HTTPException(503, str(e)) from e
    except deepseek.LLMError as e:
        raise HTTPException(502, str(e)) from e


@router.get("/reviews")
def reviews(conn: sqlite3.Connection = Depends(get_db)):
    return reports.list_reviews(conn)


class ReviewIn(BaseModel):
    kind: Literal["month", "season"] = "month"
    start: date | None = None   # default: the month / calendar year containing today
    end: date | None = None


@router.post("/reviews")
def make_review(body: ReviewIn, conn: sqlite3.Connection = Depends(get_db)):
    start, end = reports.review_period(body.kind, date.today())
    start, end = body.start or start, body.end or end
    if end < start:
        raise HTTPException(422, "end is before start")
    try:
        return reports.generate_review(conn, body.kind, start, end)
    except packet.NotEligible as e:
        raise HTTPException(422, str(e)) from e
    except deepseek.NoApiKey as e:
        raise HTTPException(503, str(e)) from e
    except deepseek.LLMError as e:
        raise HTTPException(502, str(e)) from e


@router.get("/usage")
def usage(conn: sqlite3.Connection = Depends(get_db)):
    return deepseek.usage_summary(conn)


@router.get("/focus")
def focus(conn: sqlite3.Connection = Depends(get_db)):
    return reports.planner_focus(conn)
