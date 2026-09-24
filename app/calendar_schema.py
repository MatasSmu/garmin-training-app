"""Validation for calendar events, shared by the calendar API and the coach's
calendar tools so both enforce the same rules."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator


class CalendarEvent(BaseModel):
    kind: Literal["training", "race", "plan", "blocked"]
    date: str
    end_date: str | None = None
    time: str | None = None
    title: str
    reason: Literal["exam", "friends", "party", "other"] | None = None
    notes: str | None = None

    @field_validator("date", "end_date")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        if not v:
            return None
        date.fromisoformat(v)
        return v

    @field_validator("time", "notes")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None

    @field_validator("time")
    @classmethod
    def _hh_mm(cls, v: str | None) -> str | None:
        if v and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time must be HH:MM")
        return v

    @field_validator("title")
    @classmethod
    def _title_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title must not be empty")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "CalendarEvent":
        if self.date is None:
            raise ValueError("date is required")
        if self.end_date and self.end_date < self.date:
            raise ValueError("end_date must not be before date")
        if self.end_date == self.date:
            self.end_date = None
        if self.kind == "blocked":
            self.reason = self.reason or "other"
        else:
            self.reason = None
        return self
