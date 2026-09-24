"""Gear codes are matched ignoring case and spaces; the gear key uses only
the configured parts (front wing + sail by default)."""

from app.sailing.config import MATCH_PARTS
from app.sailing.gear import gear_key, gear_label, normalize_code, part_identity


def test_normalize_ignores_case_and_spaces():
    assert normalize_code("SC 392 899") == "SC392899"
    assert normalize_code("  sc 392   899 ") == "SC392899"
    assert normalize_code("sc392899") == normalize_code("SC 392 899")
    assert normalize_code("SC 392 898") != normalize_code("SC 392 899")
    assert normalize_code(None) == "" and normalize_code("   ") == ""


def test_part_identity_falls_back_to_name():
    assert part_identity("SC 1", "whatever") == "SC1"
    assert part_identity(None, "Old 7.8 sail") == "OLD7.8SAIL"


def test_default_match_parts_are_front_wing_and_sail():
    assert MATCH_PARTS == ("front_wing", "sail")


def test_same_gear_matches_despite_formatting():
    a = {"front_wing": "SC 392 899", "sail": "IQ 9.0 AB", "board": "B 1", "boom": "X"}
    b = {"front_wing": "sc392899", "sail": "iq  9.0 ab", "board": "B 2"}   # other board: irrelevant
    assert gear_key(a) == gear_key(b) == "front_wing=SC392899|sail=IQ9.0AB"


def test_different_front_wing_or_sail_does_not_match():
    base = {"front_wing": "SC 392 899", "sail": "IQ 9.0 AB"}
    assert gear_key(base) != gear_key({**base, "front_wing": "SC 392 900"})
    assert gear_key(base) != gear_key({**base, "sail": "IQ 8.0 AB"})


def test_missing_matching_part_gives_no_key():
    assert gear_key({"front_wing": "SC 1"}) is None
    assert gear_key({"sail": "S 1", "front_wing": "  "}) is None
    assert gear_key({}) is None


def test_match_parts_are_configurable():
    g = {"front_wing": "F1", "sail": "S1", "board": "B1"}
    assert gear_key(g, ("front_wing", "sail", "board")) == "front_wing=F1|sail=S1|board=B1"
    assert gear_key({"front_wing": "F1", "sail": "S1"}, ("front_wing", "sail", "board")) is None
    assert gear_key(g, ("board",)) == gear_key({"board": "b 1"}, ("board",))


def test_label_keeps_codes_as_entered():
    assert gear_label({"front_wing": "SC  392 899", "sail": "iq 9"}) == "SC 392 899 + iq 9"
