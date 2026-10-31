"""Analysis settings: per-sport defaults plus the user's per-session overrides.

Everything the engine treats as a tunable lives here so the UI can show and
edit it, and so a cached analysis can be keyed on exactly what produced it.
Speeds are stored in knots (what sailors think in) and converted by the
engine; everything else is SI.
"""

from __future__ import annotations

import copy
import hashlib
import json

ENGINE_VERSION = "1.2.0"

SPORTS = {
    # key: (label, on-state name, on kn, off kn)
    "windfoil": ("Windfoil", "foiling", 10.0, 8.0),
    "wingfoil": ("Wingfoil", "foiling", 9.0, 7.0),
    "kitefoil": ("Kitefoil", "foiling", 9.0, 7.0),
    "windsurf": ("Windsurf", "planing", 12.0, 9.0),
    "kite": ("Kite", "planing", 10.0, 7.0),
    "sail": ("Sailing", "planing", 8.0, 6.0),
}

DEFAULTS: dict = {
    "sport": "windfoil",
    # --- cleaning
    "max_accel_ms2": 3.0,          # speed spikes: jump in AND out faster than this
    "max_speed_kn": 60.0,          # position jumps implying more than this are dropped
    "max_hdop": 5.0,               # fixes worse than this are flagged (if the file has HDOP)
    "gap_s": 5.0,                  # recording gaps longer than this split the track
    "heading_sigma_s": 1.5,        # Gaussian smoothing of course over ground
    # --- state (foiling / planing) hysteresis
    "on_kn": None,                 # None -> sport default
    "off_kn": None,
    # --- sailing / maneuver detection
    "stop_kn": 2.0,                # below this you're stopped / in the water
    "steady_turn_dps": 8.0,        # turn rate above this isn't steady sailing
    "min_steady_s": 6.0,           # a tack side must be held this long to count
    "maneuver_window_s": 40.0,     # max gap between the two steady sides of a maneuver
    "min_leg_s": 15.0,
    "upwind_max_twa": 75.0,        # |TWA| <= this is upwind
    "downwind_min_twa": 105.0,     # |TWA| >= this is downwind
    # --- wind
    "wind_override_deg": None,     # manual constant wind direction (from)
    "wind_keyframes": [],          # manual [{t: epoch_s, deg}] -- piecewise wind
    "wind_window_min": 20.0,       # rolling window for shift tracking
    "wind_step_min": 4.0,
    "wind_max_shift_deg": 35.0,    # rolling estimate is clamped to global +- this
    "wind_prior_deg": None,        # soft hint to decide which end of the axis is upwind
    "wind_prior_source": None,     # "open-meteo" | "device" | "manual" -- sets its weight
    # --- targets (knots / degrees). mode: "polar" uses the personal polar.
    # percentile: which polar curve polar-mode targets come from (50/75/90)
    "targets": {"mode": "polar", "percentile": 75, "upwind_kn": None, "upwind_twa": None,
                "downwind_kn": None, "downwind_twa": None},
    # --- HR
    "max_hr": None,                # None -> from file, else 190
    "rest_hr": None,
    # --- race
    "race_start": None,            # epoch seconds (UTC) of the start gun
    "marks": [],                   # [{lat, lon, name}] course order; empty -> auto
    "mark_radius_m": 60.0,
}


def sport_defaults(sport: str) -> dict:
    label, state, on, off = SPORTS.get(sport, SPORTS["windfoil"])
    return {"label": label, "state_name": state, "on_kn": on, "off_kn": off}


def resolve(overrides: dict | None, sport: str | None = None) -> dict:
    """Defaults <- sport profile <- user overrides (None values fall through)."""
    s = copy.deepcopy(DEFAULTS)
    ov = overrides or {}
    if sport:
        s["sport"] = sport
    if ov.get("sport"):
        s["sport"] = ov["sport"]
    sd = sport_defaults(s["sport"])
    s["on_kn"], s["off_kn"] = sd["on_kn"], sd["off_kn"]
    s["state_name"] = sd["state_name"]
    for k, v in ov.items():
        if v is None or k == "sport":
            continue
        if k == "targets" and isinstance(v, dict):
            s["targets"].update({tk: tv for tk, tv in v.items() if tv is not None or tk == "mode"})
        else:
            s[k] = v
    if s["off_kn"] >= s["on_kn"]:
        s["off_kn"] = s["on_kn"] - 0.5
    return s


def settings_hash(settings: dict, extra: object = None) -> str:
    blob = json.dumps([ENGINE_VERSION, settings, extra], sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def guess_sport(*names: str | None) -> str:
    text = " ".join(n for n in names if n).lower()
    foil = "foil" in text
    if "wing" in text:
        return "wingfoil"
    if "kite" in text:
        return "kitefoil" if foil else "kite"
    if "windsurf" in text or "wind" in text:
        return "windfoil" if foil else "windsurf"
    if foil:
        return "windfoil"
    if "sail" in text:
        return "sail"
    return "windsurf"
