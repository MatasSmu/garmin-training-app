"""Quick text -> DeepSeek estimate ("2 burgers and 500 ml milk"). Returns
items for the athlete to check and edit; nothing is saved here."""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field, field_validator

from app.nutrition import calc
from app.sailing import deepseek

MAX_ITEMS = 15

SYSTEM = """You estimate nutrition for a food diary. The user describes what they \
ate in Lithuanian or English, often loosely ("2 burgers and 500 ml milk", \
"didelė lėkštė grikių su vištiena"). Split it into items and estimate each \
item's grams, kcal, protein, carbs and fat for the amount described, using \
typical values (Lithuanian/European portions and products when unspecified). \
Be realistic, not optimistic; when unsure, pick the typical portion.
Answer with JSON only:
{"items": [{"name": "short item name in the user's language", "grams": number or null, \
"kcal": number, "protein_g": number, "carbs_g": number, "fat_g": number}], \
"note": "one short sentence on assumptions, or empty"}"""


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    grams: float | None = Field(default=None, ge=0, le=5000)
    kcal: float = Field(ge=0, le=10000)
    protein_g: float = Field(default=0, ge=0, le=500)
    carbs_g: float | None = Field(default=None, ge=0, le=1500)
    fat_g: float | None = Field(default=None, ge=0, le=1000)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return str(v).strip()[:120]

    @field_validator("kcal", "protein_g", "carbs_g", "fat_g", "grams", mode="before")
    @classmethod
    def _round(cls, v):
        return None if v in (None, "") else round(float(v), 1)


class Estimate(BaseModel):
    items: list[Item] = Field(min_length=1, max_length=MAX_ITEMS)
    note: str = ""

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, v):
        return "" if v is None else str(v).strip()[:300]


def estimate(conn: sqlite3.Connection, text: str) -> dict:
    """{items, totals, note}. Raises deepseek.LLMError (incl. NoApiKey)."""
    data, _usage = deepseek.call_json(conn, system=SYSTEM, user=text.strip(), schema=Estimate,
                                      purpose="nutrition_estimate", ref=None, max_tokens=800)
    return {"items": data["items"], "totals": calc.totals(data["items"]), "note": data["note"]}
