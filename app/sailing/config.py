"""Every tunable of the iQFOIL session analysis in one place: wind bands,
which gear parts define a "same gear" comparison, baseline windows, the
packet budget and DeepSeek pricing for the cost log."""

from __future__ import annotations

import os

# Sessions of these sports get gear slots, baselines and DeepSeek analyses.
SPORTS = ("windfoil",)

# Average wind (knots) -> band. Lower edge inclusive, upper exclusive; None =
# open-ended. Anything below the first band's lower edge is ignored entirely
# (no foiling in that wind), as is a session with no wind value at all.
WIND_BANDS: tuple[tuple[float, float | None], ...] = (
    (6, 9),
    (9, 12),
    (12, 16),
    (16, 20),
    (20, None),
)
MIN_WIND_KN = WIND_BANDS[0][0]

# iQFOIL gear slots: one part of each kind per session, identified by its code.
PARTS = ("board", "front_wing", "back_wing", "foil_mast", "fuselage", "sail", "mast", "boom")
PART_LABELS = {
    "board": "Board", "front_wing": "Front wing", "back_wing": "Back wing", "foil_mast": "Foil mast",
    "fuselage": "Fuselage", "sail": "Sail", "mast": "Mast", "boom": "Boom",
}

# Parts that must match for a band + gear baseline -- the ones that change
# performance the most. Override with e.g. SAIL_MATCH_PARTS=front_wing,sail,board
MATCH_PARTS: tuple[str, ...] = tuple(
    p.strip() for p in os.environ.get("SAIL_MATCH_PARTS", "front_wing,sail").split(",")
    if p.strip() in PARTS
) or ("front_wing", "sail")

ROLLING_N = 10           # rolling average over the last N sessions of a group
MIN_COMBO_SESSIONS = 3   # fewer band + gear sessions -> fall back to the band baseline

# Metrics compared against baselines: key -> (summary_json field, higher is better)
METRICS: dict[str, tuple[str, bool]] = {
    "vmg_up_kn": ("upwind_vmg_kn", True),
    "vmg_down_kn": ("downwind_vmg_kn", True),
    "max_kn": ("max_kn", True),
    "angle_up_deg": ("upwind_twa", True),    # athlete's choice: a bigger upwind TWA counts as better
}

PACKET_MAX_TOKENS = 1500
NOTE_MAX_CHARS = 600

# DeepSeek list prices, USD per 1M tokens, only for the cost estimate in the
# usage log. Check https://api-docs.deepseek.com/quick_start/pricing and
# override via env if they change.
PRICE_CACHE_HIT = float(os.environ.get("DEEPSEEK_PRICE_CACHE_HIT", "0.028"))
PRICE_CACHE_MISS = float(os.environ.get("DEEPSEEK_PRICE_CACHE_MISS", "0.28"))
PRICE_OUTPUT = float(os.environ.get("DEEPSEEK_PRICE_OUTPUT", "0.42"))
