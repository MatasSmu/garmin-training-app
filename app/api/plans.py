"""Plan tab: the coach's plan PDFs (/api/plans)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import plans
from app.api.deps import get_db

router = APIRouter(prefix="/api/plans", tags=["plans"])


class PlanEdit(BaseModel):
    title: str = Field(min_length=1, max_length=120)


def _plan_or_404(conn: sqlite3.Connection, plan_id: int) -> dict:
    plan = plans.get_plan(conn, plan_id)
    if plan is None:
        raise HTTPException(404, "plan not found")
    return plan


@router.get("")
def list_plans(conn: sqlite3.Connection = Depends(get_db)):
    return plans.list_plans(conn)


@router.post("")
async def upload_plan(file: UploadFile = File(...), title: str | None = Form(None),
                      conn: sqlite3.Connection = Depends(get_db)):
    data = await file.read(plans.MAX_BYTES + 1)
    try:
        plan_id, created = plans.add_plan(conn, data, file.filename or "plan.pdf", title)
    except plans.BadPlan as e:
        raise HTTPException(400, str(e)) from e
    return {**plans.get_plan(conn, plan_id), "created": created}


@router.patch("/{plan_id}")
def edit_plan(plan_id: int, payload: PlanEdit, conn: sqlite3.Connection = Depends(get_db)):
    _plan_or_404(conn, plan_id)
    plans.rename_plan(conn, plan_id, payload.title)
    return plans.get_plan(conn, plan_id)


@router.delete("/{plan_id}")
def delete_plan(plan_id: int, conn: sqlite3.Connection = Depends(get_db)):
    if not plans.delete_plan(conn, plan_id):
        raise HTTPException(404, "plan not found")
    return {"deleted": plan_id}


@router.get("/{plan_id}/pdf")
def plan_pdf(plan_id: int, download: bool = False, conn: sqlite3.Connection = Depends(get_db)):
    plan = _plan_or_404(conn, plan_id)
    path = plans.pdf_path(plan["sha1"])
    if not path.exists():
        raise HTTPException(404, "file missing")
    return FileResponse(path, media_type="application/pdf", filename=plan["file_name"],
                        content_disposition_type="attachment" if download else "inline")


@router.get("/{plan_id}/pages/{n}.png")
def plan_page(plan_id: int, n: int, conn: sqlite3.Connection = Depends(get_db)):
    plan = _plan_or_404(conn, plan_id)
    path = plans.page_path(plan["sha1"], n)
    if n < 1 or n > (plan["pages"] or 0) or not path.exists():
        raise HTTPException(404, "page not found")
    # Page images never change for a given file (named by its hash): cache them.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000, immutable"})
