from __future__ import annotations

from app.domain.party_pack_rules import party_sides
from app.domain.rules_salads import rule_cobb_salad, rule_fajitas_and_salad


def _line_named(lines: list[dict], name: str) -> dict:
    return next(line for line in lines if line["name"] == name)


def test_party_package_chips_are_three_oz_per_person():
    sides = party_sides(10, "refried")
    chips = _line_named(sides, "Chips")

    assert chips["per_qty"] == 3.0
    assert chips["unit"] == "oz"
    assert chips["total"] == 30.0
    assert chips["display_total"] == "1.88 lb / 30.0 oz"


def test_cobb_salad_lettuce_is_four_oz_per_person():
    item = {
        "item_key": "cobb_salad",
        "package_type": "salads",
        "qty": 10,
        "choices": {"packaging": "tray"},
        "extras": [],
        "flags": [],
    }

    breakdown = rule_cobb_salad(item, {})
    lettuce = _line_named(breakdown["sides"], "Lettuce")

    assert lettuce["per_qty"] == 4.0
    assert lettuce["unit"] == "oz"
    assert lettuce["total"] == 40.0
    assert lettuce["display_total"] == "2.5 lb / 40.0 oz"


def test_fajita_salad_lettuce_is_four_oz_per_person():
    item = {
        "item_key": "fajitas_and_salad",
        "package_type": "salads",
        "qty": 10,
        "choices": {"packaging": "tray"},
        "extras": [],
        "flags": [],
    }

    breakdown = rule_fajitas_and_salad(item, {})
    lettuce = _line_named(breakdown["sides"], "Lettuce")

    assert lettuce["per_qty"] == 4.0
    assert lettuce["unit"] == "oz"
    assert lettuce["total"] == 40.0
    assert lettuce["display_total"] == "2.5 lb / 40.0 oz"
