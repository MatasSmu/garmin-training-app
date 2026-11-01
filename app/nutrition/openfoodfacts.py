"""Barcode lookup on Open Food Facts (server side), cached in `foods` so a
product is fetched once and then works offline on the phone."""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid

import httpx

from app.nutrition import store

log = logging.getLogger(__name__)

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
FIELDS = "product_name,product_name_lt,product_name_en,brands,nutriments,serving_quantity"
USER_AGENT = "TrainingApp/1.0 (personal nutrition log)"


class LookupUnavailable(RuntimeError):
    """Open Food Facts couldn't be reached; the message is safe to show."""


def clean_barcode(code: str) -> str | None:
    code = re.sub(r"\D", "", code or "")
    return code if 6 <= len(code) <= 14 else None


def fetch_product(code: str) -> dict | None:
    """The raw OFF product, or None if OFF doesn't know it. Separate so tests
    can replace it."""
    try:
        resp = httpx.get(OFF_URL.format(code=code), params={"fields": FIELDS},
                         headers={"User-Agent": USER_AGENT}, timeout=10)
    except httpx.HTTPError as e:
        raise LookupUnavailable(f"couldn't reach Open Food Facts ({type(e).__name__})") from e
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise LookupUnavailable(f"Open Food Facts returned HTTP {resp.status_code}")
    data = resp.json()
    return data.get("product") if data.get("status") == 1 else None


def _num(v) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def parse_product(product: dict, code: str) -> dict | None:
    """OFF product -> food fields per 100 g; None without usable kcal."""
    n = product.get("nutriments") or {}
    kcal = _num(n.get("energy-kcal_100g"))
    if kcal is None and _num(n.get("energy_100g")) is not None:  # kJ only
        kcal = round(_num(n.get("energy_100g")) / 4.184, 1)
    if kcal is None:
        return None
    name = (product.get("product_name_lt") or product.get("product_name") or product.get("product_name_en") or "").strip()
    brand = (product.get("brands") or "").split(",")[0].strip() or None
    return {
        "name": name or (brand or f"Product {code}"),
        "brand": brand,
        "kcal_100g": kcal,
        "protein_100g": _num(n.get("proteins_100g")) or 0,
        "carbs_100g": _num(n.get("carbohydrates_100g")),
        "fat_100g": _num(n.get("fat_100g")),
        "serving_g": _num(product.get("serving_quantity")),
        "barcode": code,
        "source": "openfoodfacts",
    }


def lookup(conn: sqlite3.Connection, code: str) -> dict | None:
    """Food for a barcode: from the local cache, else from OFF (then cached).
    None when nobody knows it (the athlete enters the label once)."""
    cached = store.food_by_barcode(conn, code)
    if cached:
        return cached
    product = fetch_product(code)
    food = parse_product(product, code) if product else None
    if food is None:
        return None
    food["id"] = str(uuid.uuid4())
    store.upsert_food(conn, food)
    conn.commit()
    log.info("Open Food Facts: cached %s (%s)", code, food["name"])
    return store.get_food(conn, food["id"])
