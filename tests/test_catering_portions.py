from __future__ import annotations

from app.domain.party_pack_rules import party_sides
from app.domain.rules_fajitas import rule_fajitas
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


def test_party_package_side_rates_drop_over_30_people():
    sides = party_sides(31, "refried")

    assert _line_named(sides, "Onions")["per_qty"] == 1.0
    assert _line_named(sides, "Pico De Gallo")["per_qty"] == 1.0
    assert _line_named(sides, "Guacamole")["per_qty"] == 1.5
    assert _line_named(sides, "Sour Cream")["per_qty"] == 1.0
    assert _line_named(sides, "Rice")["per_qty"] == 3.5
    assert _line_named(sides, "Refried Beans")["per_qty"] == 3.5
    assert _line_named(sides, "Chips")["per_qty"] == 2.5
    assert _line_named(sides, "Red Sauce")["per_qty"] == 1.5
    assert _line_named(sides, "Green Sauce")["per_qty"] == 1.5


def test_party_package_side_rates_drop_over_50_people():
    sides = party_sides(51, "refried")

    assert _line_named(sides, "Onions")["per_qty"] == 0.7
    assert _line_named(sides, "Pico De Gallo")["per_qty"] == 1.0
    assert _line_named(sides, "Guacamole")["per_qty"] == 1.5
    assert _line_named(sides, "Sour Cream")["per_qty"] == 0.8
    assert _line_named(sides, "Rice")["per_qty"] == 3.0
    assert _line_named(sides, "Refried Beans")["per_qty"] == 3.0
    assert _line_named(sides, "Chips")["per_qty"] == 2.3
    assert _line_named(sides, "Red Sauce")["per_qty"] == 1.5
    assert _line_named(sides, "Green Sauce")["per_qty"] == 1.5


def test_fajita_package_protein_rates_are_updated():
    base_item = {
        "package_type": "fajitas",
        "qty": 10,
        "choices": {"packaging": "tray", "beans": "refried", "tortillas": "flour"},
        "extras": [],
        "flags": [],
        "source": {"raw_alias": "Fajitas"},
    }

    mixed = rule_fajitas({**base_item, "item_key": "fajitas_mixed"}, {})
    assert _line_named(mixed["proteins"], "Chicken")["per_qty"] == 2.5
    assert _line_named(mixed["proteins"], "Chicken")["total"] == 25.0
    assert _line_named(mixed["proteins"], "Beef")["per_qty"] == 2.5
    assert _line_named(mixed["proteins"], "Beef")["total"] == 25.0

    chicken = rule_fajitas({**base_item, "item_key": "fajitas_chicken"}, {})
    assert _line_named(chicken["proteins"], "Chicken")["per_qty"] == 5.0
    assert _line_named(chicken["proteins"], "Chicken")["total"] == 50.0

    beef = rule_fajitas({**base_item, "item_key": "fajitas_beef"}, {})
    assert _line_named(beef["proteins"], "Beef")["per_qty"] == 5.0
    assert _line_named(beef["proteins"], "Beef")["total"] == 50.0


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
