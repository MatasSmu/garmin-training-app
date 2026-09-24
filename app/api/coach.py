from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_db
from app.coach.engine import answer
from app.coach.intents import EXAMPLE_QUESTIONS
from app.db import (
    create_conversation,
    delete_conversation,
    get_conversation,
    insert_coach_message,
    list_coach_messages,
    list_conversations,
)

router = APIRouter(prefix="/api/coach", tags=["coach"])

TITLE_MAX_CHARS = 60


class CoachQuery(BaseModel):
    query: str
    conversation_id: int | None = None


def _title_from(query: str) -> str:
    q = " ".join(query.split())
    return q if len(q) <= TITLE_MAX_CHARS else q[: TITLE_MAX_CHARS - 1].rstrip() + "…"


@router.post("/ask")
def ask_coach(payload: CoachQuery, conn: sqlite3.Connection = Depends(get_db)):
    conv_id = payload.conversation_id
    if conv_id is None:
        conv_id = create_conversation(conn, _title_from(payload.query))
    elif get_conversation(conn, conv_id) is None:
        raise HTTPException(404, "conversation not found")

    history = [
        {"role": r["role"], "content": r["content"]} for r in list_coach_messages(conn, conv_id)
    ]
    result = answer(payload.query, conn, history=history)
    insert_coach_message(conn, conv_id, "user", payload.query)
    insert_coach_message(conn, conv_id, "assistant", result["text"])
    conn.commit()
    return {**result, "conversation_id": conv_id}


class ImportedMessage(BaseModel):
    role: str
    content: str


class ChatImport(BaseModel):
    messages: list[ImportedMessage]


@router.post("/import")
def import_chat(payload: ChatImport, conn: sqlite3.Connection = Depends(get_db)):
    """Move a chat that an old app version kept in browser storage into the DB."""
    msgs = [m for m in payload.messages if m.role in ("user", "assistant") and m.content.strip()]
    if not msgs:
        return {"conversation_id": None}
    first_user = next((m.content for m in msgs if m.role == "user"), "Imported chat")
    conv_id = create_conversation(conn, "Imported: " + _title_from(first_user))
    for m in msgs:
        insert_coach_message(conn, conv_id, m.role, m.content)
    conn.commit()
    return {"conversation_id": conv_id}


@router.get("/conversations")
def get_conversations(conn: sqlite3.Connection = Depends(get_db)):
    return [dict(r) for r in list_conversations(conn)]


@router.get("/conversations/{conversation_id}/messages")
def get_conversation_messages(conversation_id: int, conn: sqlite3.Connection = Depends(get_db)):
    if get_conversation(conn, conversation_id) is None:
        raise HTTPException(404, "conversation not found")
    return [dict(r) for r in list_coach_messages(conn, conversation_id, limit=500)]


@router.delete("/conversations/{conversation_id}")
def remove_conversation(conversation_id: int, conn: sqlite3.Connection = Depends(get_db)):
    delete_conversation(conn, conversation_id)
    conn.commit()
    return {"deleted": conversation_id}


@router.get("/intents")
def get_intents():
    return {"examples": EXAMPLE_QUESTIONS}
