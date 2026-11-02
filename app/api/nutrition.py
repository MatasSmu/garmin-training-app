"""Weight & nutrition API (/api/nutrition) and the offline phone page (/food/)."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import AfterValidator, BaseModel, Field, field_validator

from app.api.deps import get_db
from app.nutrition import estimate as estimator
from app.nutrition import openfoodfacts, sheets, store
from app.sailing import deepseek

router = APIRouter(tags=["nutrition"])

FOOD_STATIC = Path(__file__).resolve().parent.parent / "static" / "food"
MAX_BATCH = 2000


def _date(v: str) -> str:
    return date.fromisoformat(v).isoformat()


def _ts(v: str | None) -> str | None:
    if v in (None, ""):
        return None
    datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v


DateStr = Annotated[str, AfterValidator(_date)]
Timestamp = Annotated[str | None, AfterValidator(_ts)]


class WeightIn(BaseModel):
    date: DateStr
    slot: Literal["morning", "evening"]
    weight_kg: float | None = Field(default=None, ge=20, le=300)
    note: str | None = Field(default=None, max_length=1000)
    score: float | None = Field(default=None, ge=-1000, le=1000)
    time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    updated_at: Timestamp = None


def _weight_dicts(weights: list[WeightIn]) -> list[dict]:
    # Only the fields the client sent: an omitted note/score/time keeps the stored one.
    return [w.model_dump(exclude_unset=True) for w in weights]


class WeightBatch(BaseModel):
    entries: list[WeightIn] = Field(max_length=MAX_BATCH)


class FoodIn(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    brand: str | None = Field(default=None, max_length=120)
    kcal_100g: float = Field(ge=0, le=1000)
    protein_100g: float = Field(default=0, ge=0, le=100)
    carbs_100g: float | None = Field(default=None, ge=0, le=100)
    fat_100g: float | None = Field(default=None, ge=0, le=100)
    serving_g: float | None = Field(default=None, gt=0, le=5000)
    barcode: str | None = Field(default=None, max_length=32)
    source: Literal["manual", "openfoodfacts", "estimate"] = "manual"
    deleted: bool = False
    updated_at: Timestamp = None

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name is empty")
        return v.strip()


class MealItemIn(BaseModel):
    food_id: str | None = Field(default=None, max_length=64)
    grams: float | None = Field(default=None, gt=0, le=5000)
    description: str | None = Field(default=None, max_length=200)
    kcal: float | None = Field(default=None, ge=0, le=20000)
    protein_g: float | None = Field(default=None, ge=0, le=1000)
    carbs_g: float | None = Field(default=None, ge=0, le=3000)
    fat_g: float | None = Field(default=None, ge=0, le=2000)


class SavedMealIn(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    items: list[MealItemIn] = Field(default_factory=list, max_length=50)
    deleted: bool = False
    updated_at: Timestamp = None

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name is empty")
        return v.strip()


class LogIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    date: DateStr
    time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    meal: Literal["breakfast", "lunch", "dinner", "snack"] | None = None
    kcal: float = Field(ge=0, le=20000)
    protein_g: float = Field(default=0, ge=0, le=1000)
    carbs_g: float | None = Field(default=None, ge=0, le=3000)
    fat_g: float | None = Field(default=None, ge=0, le=2000)
    food_id: str | None = Field(default=None, max_length=64)
    grams: float | None = Field(default=None, ge=0, le=5000)
    saved_meal_id: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=300)
    source: Literal["food", "saved_meal", "manual", "estimate"]
    deleted: bool = False
    updated_at: Annotated[str, AfterValidator(_ts)]


class SyncIn(BaseModel):
    foods: list[FoodIn] = Field(default_factory=list, max_length=MAX_BATCH)
    saved_meals: list[SavedMealIn] = Field(default_factory=list, max_length=500)
    logs: list[LogIn] = Field(default_factory=list, max_length=MAX_BATCH)
    weights: list[WeightIn] = Field(default_factory=list, max_length=MAX_BATCH)


class LogSavedMeal(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    date: str | None = None
    time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    meal: Literal["breakfast", "lunch", "dinner", "snack"] | None = None


class EstimateIn(BaseModel):
    text: str = Field(min_length=2, max_length=1000)


def _with_id(model: BaseModel) -> dict:
    d = model.model_dump()
    d["id"] = d.get("id") or str(uuid.uuid4())
    return d


# ------------------------------------------------------------------ API


@router.get("/api/nutrition/bootstrap")
def get_bootstrap(conn: sqlite3.Connection = Depends(get_db)):
    return store.bootstrap(conn)


@router.post("/api/nutrition/sync")
def post_sync(payload: SyncIn, conn: sqlite3.Connection = Depends(get_db)):
    for f in payload.foods:
        if not f.id:
            raise HTTPException(422, "every synced food needs an id")
    for m in payload.saved_meals:
        if not m.id:
            raise HTTPException(422, "every synced saved meal needs an id")
    data = payload.model_dump()
    data["weights"] = _weight_dicts(payload.weights)
    return store.sync(conn, data)


@router.get("/api/nutrition/summary")
def get_summary(conn: sqlite3.Connection = Depends(get_db)):
    return store.summary(conn)


@router.get("/api/nutrition/targets")
def get_targets(conn: sqlite3.Connection = Depends(get_db)):
    return store.targets(conn)


@router.get("/api/nutrition/weights")
def get_weights(start: str | None = None, end: str | None = None, conn: sqlite3.Connection = Depends(get_db)):
    return store.list_weights(conn, start, end)


@router.put("/api/nutrition/weights")
def put_weights(payload: WeightBatch, conn: sqlite3.Connection = Depends(get_db)):
    """Batch save, e.g. a week typed in from paper. weight_kg null clears that cell."""
    results = store.save_weights(conn, _weight_dicts(payload.entries))
    return {"results": results, "weight": store.weight_analysis(conn)}


@router.get("/api/nutrition/weight")
def get_weight_analysis(conn: sqlite3.Connection = Depends(get_db)):
    return store.weight_analysis(conn)


class SheetConfigIn(BaseModel):
    url: str = Field(min_length=10, max_length=500)
    tab: str | None = Field(default=None, max_length=100)


@router.get("/api/nutrition/sheet")
def get_sheet_status(conn: sqlite3.Connection = Depends(get_db)):
    """Google Sheet import: key / link status, the service-account e-mail to share with, last result."""
    return sheets.status(conn)


@router.put("/api/nutrition/sheet")
def put_sheet_config(payload: SheetConfigIn, conn: sqlite3.Connection = Depends(get_db)):
    try:
        sheets.save_config(conn, payload.url, payload.tab)
    except sheets.SheetError as e:
        raise HTTPException(422, str(e)) from e
    return sheets.status(conn)


@router.post("/api/nutrition/sheet/import")
def post_sheet_import(conn: sqlite3.Connection = Depends(get_db)):
    """Import now (a few seconds); the auto-import does the same every few minutes."""
    result = sheets.run_import(conn)
    return {"result": result, "status": sheets.status(conn)}


@router.get("/api/nutrition/checkins")
def get_checkins(days: int = 60, conn: sqlite3.Connection = Depends(get_db)):
    """Recent weigh-ins with time, morning score and note, newest first."""
    start = (date.today().toordinal() - max(1, min(days, 3650)))
    rows = store.list_weights(conn, date.fromordinal(start).isoformat())
    return list(reversed(rows))


@router.get("/api/nutrition/day/{day}")
def get_day(day: str, conn: sqlite3.Connection = Depends(get_db)):
    try:
        day = _date(day)
    except ValueError as e:
        raise HTTPException(400, "bad date") from e
    return {"date": day, "logs": store.day_logs(conn, day), "totals": store.day_totals(conn, day)}


@router.get("/api/nutrition/foods")
def get_foods(q: str | None = None, conn: sqlite3.Connection = Depends(get_db)):
    return store.list_foods(conn, q)


@router.post("/api/nutrition/foods")
def post_food(payload: FoodIn, conn: sqlite3.Connection = Depends(get_db)):
    food = _with_id(payload)
    store.upsert_food(conn, food)
    conn.commit()
    return store.get_food(conn, food["id"])


@router.get("/api/nutrition/barcode/{code}")
def get_barcode(code: str, conn: sqlite3.Connection = Depends(get_db)):
    clean = openfoodfacts.clean_barcode(code)
    if not clean:
        raise HTTPException(400, "not a barcode")
    try:
        food = openfoodfacts.lookup(conn, clean)
    except openfoodfacts.LookupUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return {"barcode": clean, "found": food is not None, "food": food}


@router.get("/api/nutrition/saved-meals")
def get_saved_meals(conn: sqlite3.Connection = Depends(get_db)):
    return store.list_saved_meals(conn)


@router.post("/api/nutrition/saved-meals")
def post_saved_meal(payload: SavedMealIn, conn: sqlite3.Connection = Depends(get_db)):
    meal = _with_id(payload)
    store.upsert_saved_meal(conn, meal)
    conn.commit()
    return next((m for m in store.list_saved_meals(conn) if m["id"] == meal["id"]), None)


@router.delete("/api/nutrition/saved-meals/{meal_id}")
def delete_saved_meal(meal_id: str, conn: sqlite3.Connection = Depends(get_db)):
    if not store.delete_saved_meal(conn, meal_id):
        raise HTTPException(404, "saved meal not found")
    return {"ok": True}


@router.post("/api/nutrition/saved-meals/{meal_id}/log")
def post_log_saved_meal(meal_id: str, payload: LogSavedMeal, conn: sqlite3.Connection = Depends(get_db)):
    """One tap: the saved meal's totals as one entry (today unless a date is given).
    Sending the same id again doesn't add a second entry."""
    try:
        day = _date(payload.date) if payload.date else date.today().isoformat()
    except ValueError as e:
        raise HTTPException(400, "bad date") from e
    entry = store.log_saved_meal(conn, meal_id, day, payload.time, payload.meal, payload.id)
    if entry is None:
        raise HTTPException(404, "saved meal not found")
    return {"log": entry, "totals": store.day_totals(conn, day)}


@router.post("/api/nutrition/estimate")
def post_estimate(payload: EstimateIn, conn: sqlite3.Connection = Depends(get_db)):
    """DeepSeek estimate for a quick text. Nothing is saved: the phone shows the
    items for editing and logs them only when the athlete confirms."""
    try:
        return estimator.estimate(conn, payload.text)
    except deepseek.NoApiKey as e:
        raise HTTPException(503, str(e)) from e
    except deepseek.LLMError as e:
        raise HTTPException(502, str(e)) from e


# ------------------------------------------------------------------ phone page (PWA)


@router.get("/food", include_in_schema=False)
def food_redirect():
    return RedirectResponse("/food/")


@router.get("/food-login", include_in_schema=False)
def food_login():
    """Outside the service worker's scope, so the browser really asks for the
    login instead of getting the cached page (see /gym-login)."""
    return RedirectResponse("/food/")


@router.get("/food/", include_in_schema=False)
def food_page():
    return FileResponse(FOOD_STATIC / "index.html", media_type="text/html")


@router.get("/food/sw.js", include_in_schema=False)
def food_service_worker():
    return FileResponse(FOOD_STATIC / "sw.js", media_type="text/javascript")


@router.get("/food/manifest.webmanifest", include_in_schema=False)
def food_manifest():
    return FileResponse(FOOD_STATIC / "manifest.webmanifest", media_type="application/manifest+json")
