"""Glue: match the query to an intent, build context, generate a reply."""

from __future__ import annotations

import sqlite3

from app.coach.intents import match_intent
from app.coach.metrics import (
    calendar_outlook,
    current_streak,
    days_since_rest,
    recent_journal,
    sleep_trend,
    training_load,
)
from app.coach.responder import generate_response
from app.coach.wind import forecast_briefing
from app.sailing.reports import planner_focus


def build_context(conn: sqlite3.Connection) -> dict:
    """Full training snapshot. Cheap enough (a handful of indexed SQLite
    queries, plus a rate-limited wind lookup) to compute on every turn, so
    both the DeepSeek coach and the template fallback always see the same
    complete picture rather than a slice keyed to one intent."""
    return {
        "days_since_rest": days_since_rest(conn),
        "load_7d": training_load(conn, 7),
        "load_28d": training_load(conn, 28),
        "sleep_trend": sleep_trend(conn),
        "streak": current_streak(conn),
        "journal": recent_journal(conn, 14),
        "wind_briefing": forecast_briefing(conn),
        "calendar": calendar_outlook(conn),
        "sailing_focus": planner_focus(conn),
    }


def answer(query: str, conn: sqlite3.Connection, history: list[dict] | None = None) -> dict:
    intent = match_intent(query)
    context = build_context(conn)
    changes: list[dict] = []
    text = generate_response(
        context, intent, query=query, history=history or [], conn=conn, changes=changes
    )
    return {"intent": intent, "text": text, "context": context, "calendar_changes": changes}
