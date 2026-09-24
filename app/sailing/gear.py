"""Gear codes and gear matching. Codes are stored exactly as typed
("SC 392 899"); comparisons ignore case and whitespace."""

from __future__ import annotations

from app.sailing.config import MATCH_PARTS


def normalize_code(code: str | None) -> str:
    """'  sc 392  899 ' -> 'SC392899'."""
    return "".join((code or "").split()).upper()


def part_identity(code: str | None, name: str | None = None) -> str:
    """What makes two inventory items the same part: the code, or the name
    for gear entered without one."""
    return normalize_code(code) or normalize_code(name)


def gear_key(gear: dict[str, str], parts: tuple[str, ...] = MATCH_PARTS) -> str | None:
    """Key for "same gear" comparisons from a {part kind: code} map, or None
    when any of the matching parts isn't tagged."""
    ids = []
    for p in parts:
        ident = normalize_code(gear.get(p))
        if not ident:
            return None
        ids.append(f"{p}={ident}")
    return "|".join(ids)


def gear_label(gear: dict[str, str], parts: tuple[str, ...] = MATCH_PARTS) -> str:
    """Human version of gear_key with the codes as entered."""
    return " + ".join(" ".join((gear.get(p) or "?").split()) for p in parts)
