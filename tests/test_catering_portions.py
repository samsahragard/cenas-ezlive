from __future__ import annotations

from app.domain.party_pack_rules import party_sides
from app.domain.rules_fajitas import rule_fajitas
from app.domain.rules_salads import rule_cobb_salad, rule_fajitas_and_salad
from app.domain.rules_premium import rule_premium
from app.domain.kitchen_engine import build_kitchen_result


def _line_named(lines: list[dict], name: str) -> dict:
    return next(line for line in lines if line["name"] == name)


def _order(items: list[dict], headcount: int = 10) -> dict:
    return {
        "order_id": "TST-123",
        "date": "2026-06-16",
        "deliver_at": "11:00 AM",
        "reported_store": "Tomball",
        "origin_store_id": "store_2",
        "headcount": headcount,
        "normalized_items": items,
        "flags": [],
    }


def _item(
    item_key: str,
    package_type: str,
    qty: int = 1,
    packaging: str = "none",
    beans: str = "none",
    tortillas: str = "none",
    extras: list[dict] | None = None,
    container: str | None = None,
) -> dict:
    return {
        "item_key": item_key,
        "package_type": package_type,
        "qty": qty,
        "choices": {"packaging": packaging, "beans": beans, "tortillas": tortillas},
        "extras": extras or [],
        "container": container,
        "flags": [],
        "source": {"raw_alias": item_key},
    }


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


def test_bulk_salad_single_dressing_is_three_oz_per_person():
    item = {
        "item_key": "cobb_salad",
        "package_type": "salads",
        "qty": 10,
        "choices": {"packaging": "tray"},
        "extras": [{"name": "dressing", "raw_text": "Ranch"}],
        "flags": [],
    }

    breakdown = rule_cobb_salad(item, {})
    dressing = _line_named(breakdown["sauces"], "Dressing - Ranch")

    assert dressing["per_qty"] == 3.0
    assert dressing["total"] == 30.0


def test_bulk_salad_two_dressings_split_to_one_and_half_oz_each():
    item = {
        "item_key": "fajitas_and_salad",
        "package_type": "salads",
        "qty": 10,
        "choices": {"packaging": "tray"},
        "extras": [
            {"name": "dressing", "raw_text": "Ranch"},
            {"name": "dressing", "raw_text": "Honey Mustard"},
        ],
        "flags": [],
    }

    breakdown = rule_fajitas_and_salad(item, {})

    assert _line_named(breakdown["sauces"], "Dressing - Ranch")["total"] == 15.0
    assert _line_named(breakdown["sauces"], "Dressing - Honey Mustard")["total"] == 15.0


def test_individual_salad_keeps_dressing_portion_cups():
    item = {
        "item_key": "cobb_salad",
        "package_type": "salads",
        "qty": 10,
        "choices": {"packaging": "individual"},
        "extras": [
            {"name": "dressing", "raw_text": "Ranch"},
            {"name": "dressing", "raw_text": "Honey Mustard"},
        ],
        "flags": [],
    }

    breakdown = rule_cobb_salad(item, {})

    assert "INDIVIDUAL_PACKAGING_SUMMARY_ONLY" in breakdown["flags"]
    assert _line_named(breakdown["sauces"], "Dressing - Ranch")["per_qty"] == 1.5
    assert _line_named(breakdown["sauces"], "Dressing - Honey Mustard")["per_qty"] == 1.5


def test_executive_package_uses_full_per_person_breakdown():
    item = _item(
        "cenas_exec_spread",
        "premium",
        qty=10,
        packaging="tray",
    )
    item["source"]["raw_alias"] = "Cenas Executive Fajita Spread"

    breakdown = rule_premium(item, {})

    assert _line_named(breakdown["proteins"], "Chicken")["total"] == 25.0
    assert _line_named(breakdown["proteins"], "Beef")["total"] == 25.0
    assert _line_named(breakdown["sides"], "Rice")["total"] == 38.0
    assert _line_named(breakdown["sides"], "Charro Beans")["total"] == 38.0
    assert _line_named(breakdown["sides"], "Queso Blanco")["total"] == 15.0
    assert _line_named(breakdown["sides"], "Lettuce")["total"] == 40.0
    assert _line_named(breakdown["sides"], "Churros")["total"] == 20
    assert _line_named(breakdown["sauces"], "Dressing - Dressing")["total"] == 30.0
    assert _line_named(breakdown["counts"], "Flour Tortillas")["packets"] == 13


def test_tableware_defaults_to_guest_count_plus_buffer():
    items = [
        _item("fajitas_mixed", "fajitas", qty=10, packaging="tray", beans="charro", tortillas="flour"),
        _item("tableware", "non_food_items"),
    ]

    result = build_kitchen_result(_order(items, headcount=10))
    tableware = next(b for b in result["breakdowns"] if b["item_key"] == "tableware")

    assert tableware["utensil_sub_counts"]["plates_and_bowls"] == 13
    assert tableware["utensil_sub_counts"]["silverware"] == 13


def test_side_spoons_are_one_per_different_side_type():
    items = [
        _item("queso_and_chips", "sides", qty=1, container="quart"),
        _item("queso_and_chips", "sides", qty=1, container="pint"),
        _item("guac_and_chips", "sides", qty=1, container="pint"),
        _item("tableware", "non_food_items"),
    ]

    result = build_kitchen_result(_order(items, headcount=10))
    tableware = next(b for b in result["breakdowns"] if b["item_key"] == "tableware")

    assert tableware["utensil_sub_counts"]["catering_small_spoons"] == 2


def test_enchilada_trays_add_one_large_spoon_each():
    items = [
        _item("cheese_enchiladas", "enchiladas", qty=2, packaging="tray"),
        _item("tableware", "non_food_items"),
    ]

    result = build_kitchen_result(_order(items, headcount=10))
    tableware = next(b for b in result["breakdowns"] if b["item_key"] == "tableware")

    assert tableware["utensil_sub_counts"]["catering_large_spoons"] == 2
