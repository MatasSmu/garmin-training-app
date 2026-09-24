"""DeepSeek JSON calls for the iQFOIL analyses: one system message (stable,
cache-friendly), one user message (the packet), JSON mode, schema
validation, and a row in llm_usage for every attempt."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from app import config
from app.sailing.config import PRICE_CACHE_HIT, PRICE_CACHE_MISS, PRICE_OUTPUT

log = logging.getLogger(__name__)

MAX_ITEMS = 5
MAX_CHARS = 1200


class LLMError(RuntimeError):
    """The model couldn't produce a usable answer; the message is safe to show."""


class NoApiKey(LLMError):
    pass


# ------------------------------------------------------------------ schemas
def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        return [str(x).strip()[:MAX_CHARS] for x in v if str(x).strip()][:MAX_ITEMS]
    raise ValueError("expected a list of strings")


def _as_text(v):
    if isinstance(v, list):
        v = " ".join(str(x) for x in v)
    elif isinstance(v, dict):
        v = "; ".join(f"{k}: {x}" for k, x in v.items())
    if not isinstance(v, str):
        raise ValueError("expected text")
    return v.strip()[:MAX_CHARS]


class _Model(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def _coerce(cls, v, info):
        ann = cls.model_fields[info.field_name].annotation
        if ann == list[str]:
            return _as_list(v)
        if ann is str:
            return _as_text(v)
        return v


class Causes(_Model):
    technique: list[str] = []
    gear: list[str] = []
    fatigue_recovery: list[str] = []


class SessionAnalysis(_Model):
    summary: str = Field(min_length=1)
    strengths: list[str]
    weaknesses: list[str]
    vs_baseline: str
    possible_causes: Causes
    focus_next_session: list[str] = Field(min_length=1)
    plan_adjustment: str

    @field_validator("possible_causes", mode="before")
    @classmethod
    def _cause_keys(cls, v):
        # "fatigue-recovery", "Fatigue/Recovery", "fatigue" -> fatigue_recovery
        if not isinstance(v, dict):
            raise ValueError("expected an object with technique / gear / fatigue_recovery")
        out = {}
        for k, x in v.items():
            key = "".join(c if c.isalnum() else "_" for c in str(k).lower())
            out["fatigue_recovery" if key.startswith("fatigue") or key == "recovery" else key] = x
        return out


class WeeklySummary(_Model):
    summary: str = Field(min_length=1)
    progress: list[str]
    recurring_issues: list[str]
    focus_next_week: list[str]
    plan_adjustment: str


class Review(_Model):
    summary: str = Field(min_length=1)
    trends: list[str]
    strengths: list[str]
    weaknesses: list[str]
    priorities: list[str]


# ------------------------------------------------------------------ usage log
def cost_usd(usage: dict) -> float:
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is None and miss is None:
        hit, miss = 0, usage.get("prompt_tokens") or 0
    return round(((hit or 0) * PRICE_CACHE_HIT + (miss or 0) * PRICE_CACHE_MISS
                  + (usage.get("completion_tokens") or 0) * PRICE_OUTPUT) / 1e6, 6)


def log_usage(conn: sqlite3.Connection, purpose: str, ref, model: str | None, usage: dict | None,
              ok: bool, error: str | None = None) -> dict:
    u = usage or {}
    row = {"purpose": purpose, "ref": None if ref is None else str(ref), "model": model,
           "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
           "cache_hit_tokens": u.get("prompt_cache_hit_tokens"), "cache_miss_tokens": u.get("prompt_cache_miss_tokens"),
           "cost_usd": cost_usd(u) if usage else None, "ok": int(ok), "error": error,
           "created_at": datetime.now(timezone.utc).isoformat()}
    conn.execute(f"INSERT INTO llm_usage ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))
    conn.commit()
    log.info("DeepSeek %s %s: %s prompt (%s cached) + %s completion tokens, ~$%s%s", purpose, ref,
             row["prompt_tokens"], row["cache_hit_tokens"], row["completion_tokens"], row["cost_usd"],
             "" if ok else f" -- FAILED: {error}")
    return row


def usage_summary(conn: sqlite3.Connection, limit: int = 50) -> dict:
    tot = conn.execute(
        "SELECT COUNT(*) AS calls, SUM(ok) AS ok, SUM(prompt_tokens) AS prompt_tokens, "
        "SUM(completion_tokens) AS completion_tokens, SUM(cache_hit_tokens) AS cache_hit_tokens, "
        "SUM(cost_usd) AS cost_usd FROM llm_usage").fetchone()
    return {"totals": {k: tot[k] or 0 for k in tot.keys()},
            "calls": [dict(r) for r in conn.execute("SELECT * FROM llm_usage ORDER BY id DESC LIMIT ?", (limit,))]}


# ------------------------------------------------------------------ calls
def post_chat(body: dict) -> dict:
    """One /chat/completions request; separate so tests can replace it."""
    resp = httpx.post(f"{config.DEEPSEEK_BASE_URL}/chat/completions",
                      headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"}, json=body, timeout=90)
    resp.raise_for_status()
    return resp.json()


def _parse(content: str, schema: type[BaseModel]) -> dict:
    text = content.strip()
    if text.startswith("```"):  # tolerate a fenced reply despite JSON mode
        text = text.strip("`")
        text = text[text.find("{"):]
    return schema.model_validate(json.loads(text)).model_dump()


def call_json(conn: sqlite3.Connection, *, system: str, user: str, schema: type[BaseModel], purpose: str,
              ref, max_tokens: int = 2000, attempts: int = 2) -> tuple[dict, dict]:
    """Ask for a JSON object matching schema. Returns (data, usage row of the
    successful call). Retries once on invalid output or a transient error;
    raises LLMError otherwise. Every attempt is logged with its tokens."""
    if not config.DEEPSEEK_API_KEY:
        raise NoApiKey("DEEPSEEK_API_KEY isn't set, so AI analysis is off.")
    body = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0.4,
        "max_tokens": max_tokens,
    }
    last = "no answer"
    for _ in range(attempts):
        usage, model = None, config.DEEPSEEK_MODEL
        try:
            resp = post_chat(body)
            usage, model = resp.get("usage"), resp.get("model") or model
            choice = resp["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError("reply was cut off (max_tokens)")
            data = _parse(choice["message"].get("content") or "", schema)
        except httpx.HTTPStatusError as e:
            last = f"DeepSeek returned HTTP {e.response.status_code}"
            log_usage(conn, purpose, ref, model, usage, False, last)
            if e.response.status_code in (400, 401, 402, 403, 422):
                break  # bad key / no balance / bad request: retrying won't help
            continue
        except httpx.HTTPError as e:
            last = f"couldn't reach DeepSeek ({type(e).__name__})"
            log_usage(conn, purpose, ref, model, usage, False, last)
            continue
        except (ValueError, KeyError, IndexError, TypeError, ValidationError) as e:
            last = f"invalid answer from the model ({str(e).splitlines()[0][:200]})"
            log_usage(conn, purpose, ref, model, usage, False, last)
            continue
        row = log_usage(conn, purpose, ref, model, usage, True)
        return data, row
    raise LLMError(f"AI analysis failed: {last}.")
