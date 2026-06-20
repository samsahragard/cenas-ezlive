from __future__ import annotations

from types import SimpleNamespace

from app.domain.party_pack_rules import party_sides, round_tortilla_packets
from app.domain.rules_fajitas import rule_fajitas
from app.domain.rules_salads import rule_cobb_salad, rule_fajitas_and_salad
from app.domain.rules_premium import rule_premium
from app.domain.kitchen_engine import build_kitchen_result
from app.domain.master_sheet_map import build_all_outputs
from app.domain.menu_catalog import MenuCatalog, MENU_CATALOG
from app.domain.ticket_context import build_ticket_context
from app.services.orders_query import reconstruct_bundle


def _line_named(lines: list[dict], name: str) -> dict:
    return next(line for line in lines if line["name"] == name)


def _order(items: list[dict], headcount: int = 10) -> dict:
    return {
        "order_id": "TST-123",
        "date": "2026-06-16",
        "deliver_at": "11:00 AM",
        "reported_store": "Tomball",
        "reported_store_id": "store_2",
        "origin_store_id": "store_2",
        "headcount": headcount,
        "client": "",
        "upon_delivery_ask_for": "",
        "customer_phone": "",
        "delivery_address": "",
        "delivery_window": {"start": "", "end": ""},
        "delivery_instructions": None,
        "setup_required": None,
        "notes": None,
        "normalized_items": items,
        "flags": [],
    }


def _master(items: list[dict], headcount: int = 10) -> dict:
    order = _order(items, headcount=headcount)
    result = build_kitchen_result(order)
    ctx = build_ticket_context(order, result, {})
    return build_all_outputs(order, result, ctx, MenuCatalog(MENU_CATALOG))["master"]


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


def _row_order(headcount: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        external_order_id="CWM-PRP",
        client="",
        upon_delivery_ask_for="",
        reported_store="Tomball",
        reported_store_id="store_2",
        origin_store_id="store_2",
        headcount=headcount,
        customer_phone="",
        delivery_date="2026-06-16",
        deliver_at="11:00 AM",
        delivery_window={"start": "", "end": ""},
        delivery_address="",
        delivery_instructions=None,
        setup_required=None,
        route_group_id=None,
        route_stop_index=None,
        assigned_driver=None,
        driver_departure_time=None,
        kitchen_ready_time=None,
        flags=[],
    )


def _row_item(
    row_id: int,
    item_key: str,
    package_type: str,
    qty: int,
    packaging: str = "none",
    raw_alias: str | None = None,
    extras: list[dict] | None = None,
) -> SimpleNamespace:
    choices = {"packaging": packaging, "beans": "none", "tortillas": "none", "with_ice": None}
    return SimpleNamespace(
        id=row_id,
        item_key=item_key,
        package_type=package_type,
        qty=qty,
        packaging=packaging,
        choices=choices,
        extras=extras or [],
        source={"raw_alias": raw_alias or item_key, "raw_qty": qty, "raw_line_items": []},
        raw_alias=raw_alias or item_key,
        flags=[],
    )
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


def test_tortilla_packets_round_to_nearest_whole_packet():
    assert round_tortilla_packets(10.25) == 10
    assert round_tortilla_packets(10.51) == 11


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


def test_cobb_salad_chicken_choice_adds_chicken_diced():
    item = {
        "item_key": "cobb_salad",
        "package_type": "salads",
        "qty": 9,
        "choices": {"packaging": "tray"},
        "extras": [{"name": "protein", "raw_text": "chicken"}],
        "flags": [],
    }

    breakdown = rule_cobb_salad(item, {})
    chicken = _line_named(breakdown["proteins"], "Chicken Diced")

    assert chicken["per_qty"] == 2.0
    assert chicken["total"] == 18.0


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


def test_master_bulk_salad_dressing_displays_ounces():
    master = _master([
        _item(
            "cobb_salad",
            "salads",
            qty=9,
            packaging="tray",
            extras=[
                {"name": "dressing", "raw_text": "Most Popular"},
                {"name": "protein", "raw_text": "chicken"},
            ],
        )
    ])

    assert master["item.salad_dressing"] == "27 Most Popular"
    assert master["component.Chicken Diced"] == "1.12"


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


def test_master_individual_salad_dressing_displays_portion_cups():
    master = _master([
        _item(
            "cobb_salad",
            "salads",
            qty=10,
            packaging="individual",
            extras=[
                {"name": "dressing", "raw_text": "Ranch"},
                {"name": "dressing", "raw_text": "Honey Mustard"},
            ],
        )
    ])

    assert master["item.salad_dressing"] == (
        "10 1.5oz Ranch portion cups | 10 1.5oz Honey Mustard portion cups"
    )


def test_master_side_rows_show_container_units():
    master = _master([
        _item("queso_and_chips", "sides", qty=1, container="quart"),
        _item("guac_and_chips", "sides", qty=2),
    ])

    assert master["item.queso_and_chips"] == "1 quart"
    assert master["item.guac_and_chips"] == "2 pints"


def test_master_individual_package_uses_specific_label_row():
    master = _master([
        _item("fajitas_mixed", "fajitas", qty=16, packaging="individual", beans="charro", tortillas="flour"),
    ], headcount=16)

    assert master["meta.individual.fajitas_mixed"] == "16"
    assert master["meta.individual"] == ""


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


def test_bulk_tableware_uses_ordered_food_qty_plus_buffer():
    items = [
        _item("fajitas_mixed", "fajitas", qty=10, packaging="tray", beans="charro", tortillas="flour"),
        _item("tableware", "non_food_items"),
    ]

    result = build_kitchen_result(_order(items, headcount=100))
    tableware = next(b for b in result["breakdowns"] if b["item_key"] == "tableware")

    assert tableware["utensil_sub_counts"]["plates_and_bowls"] == 13
    assert tableware["utensil_sub_counts"]["silverware"] == 13


def test_bulk_tableware_is_generated_when_pdf_has_no_tableware_line():
    items = [
        _item("fajitas_mixed", "fajitas", qty=10, packaging="tray", beans="charro", tortillas="flour"),
    ]

    result = build_kitchen_result(_order(items, headcount=100))
    tableware = next(b for b in result["breakdowns"] if b["item_key"] == "tableware")

    assert "AUTO_TABLEWARE" in tableware["flags"]
    assert tableware["utensil_sub_counts"]["plates_and_bowls"] == 13
    assert tableware["utensil_sub_counts"]["silverware"] == 13


def test_bulk_tableware_ignores_pdf_plate_override_when_food_qty_exists():
    items = [
        _item("fajitas_mixed", "fajitas", qty=30, packaging="tray", beans="charro", tortillas="flour"),
        _item(
            "tableware",
            "non_food_items",
            extras=[
                {"name": "plates_and_bowls", "raw_text": "3"},
                {"name": "silverware", "raw_text": "3"},
            ],
        ),
    ]

    result = build_kitchen_result(_order(items, headcount=100))
    tableware = next(b for b in result["breakdowns"] if b["item_key"] == "tableware")

    assert tableware["utensil_sub_counts"]["plates_and_bowls"] == 33
    assert tableware["utensil_sub_counts"]["silverware"] == 33


def test_individual_enchiladas_get_silverware_without_plates():
    items = [
        _item("cheese_enchiladas_individual", "enchiladas", qty=30, packaging="individual"),
        _item("tableware", "non_food_items"),
    ]

    result = build_kitchen_result(_order(items, headcount=100))
    tableware = next(b for b in result["breakdowns"] if b["item_key"] == "tableware")

    assert tableware["utensil_sub_counts"]["silverware"] == 33
    assert tableware["utensil_sub_counts"]["plates_and_bowls"] == 0
    assert tableware["utensil_sub_counts"]["catering_large_spoons"] == 0


def test_saved_individual_enchilada_order_refreshes_stale_tableware_breakdown():
    items = [
        _row_item(1, "cheese_enchiladas_individual", "enchiladas", 9, "individual", "Cheese"),
        _row_item(2, "veggie_enchiladas_individual", "enchiladas", 2, "individual", "Veggie"),
        _row_item(3, "beef_enchiladas_individual", "enchiladas", 9, "individual", "Beef Fajita"),
        _row_item(4, "chicken_fajita_enchiladas_individual", "enchiladas", 10, "individual", "Chicken Fajita"),
        _row_item(
            5,
            "tableware",
            "non_food_items",
            1,
            extras=[{"name": "plates_and_bowls", "raw_text": "4"}],
        ),
    ]
    stale_tableware = {
        "item_key": "tableware",
        "package_type": "non_food_items",
        "qty": 1,
        "choices": {"packaging": "none"},
        "proteins": [],
        "sides": [],
        "sauces": [],
        "extras": [],
        "counts": [],
        "summary_line": "4x Plates | 0x Silverware",
        "flags": [],
        "utensil_sub_counts": {
            "plates_and_bowls": 4,
            "silverware": 0,
            "catering_large_spoons": 0,
            "catering_small_spoons": 0,
            "black_tongs": 0,
        },
    }
    bundle = reconstruct_bundle(
        _row_order(headcount=0),
        items,
        {5: [SimpleNamespace(breakdown=stale_tableware)]},
    )
    tableware = next(b for b in bundle["kitchen_result"]["breakdowns"] if b["item_key"] == "tableware")

    assert tableware["utensil_sub_counts"]["silverware"] == 33
    assert tableware["utensil_sub_counts"]["plates_and_bowls"] == 0


def test_saved_bulk_order_refreshes_tableware_from_food_qty_not_pdf_plates():
    items = [
        _row_item(1, "fajitas_mixed", "fajitas", 15, "tray", "Beef & Chicken Fajita Party Package"),
        _row_item(
            2,
            "tableware",
            "non_food_items",
            1,
            extras=[{"name": "plates_and_bowls", "raw_text": "3"}],
        ),
    ]
    stale_tableware = {
        "item_key": "tableware",
        "package_type": "non_food_items",
        "qty": 1,
        "choices": {"packaging": "none"},
        "proteins": [],
        "sides": [],
        "sauces": [],
        "extras": [],
        "counts": [],
        "summary_line": "3x Plates | 15x Silverware",
        "flags": [],
        "utensil_sub_counts": {
            "plates_and_bowls": 3,
            "silverware": 15,
            "catering_large_spoons": 0,
            "catering_small_spoons": 0,
            "black_tongs": 0,
        },
    }
    bundle = reconstruct_bundle(
        _row_order(headcount=15),
        items,
        {2: [SimpleNamespace(breakdown=stale_tableware)]},
    )
    tableware = next(b for b in bundle["kitchen_result"]["breakdowns"] if b["item_key"] == "tableware")

    assert tableware["utensil_sub_counts"]["silverware"] == 18
    assert tableware["utensil_sub_counts"]["plates_and_bowls"] == 18


def test_saved_bulk_order_without_tableware_gets_generated_tableware():
    order = _row_order(headcount=0)
    order.delivery_date = "2099-01-01"
    items = [
        _row_item(1, "fajitas_mixed", "fajitas", 15, "tray", "Beef & Chicken Fajita Party Package"),
    ]

    bundle = reconstruct_bundle(order, items, {})
    tableware = next(b for b in bundle["kitchen_result"]["breakdowns"] if b["item_key"] == "tableware")

    assert "AUTO_TABLEWARE" in tableware["flags"]
    assert tableware["utensil_sub_counts"]["silverware"] == 18
    assert tableware["utensil_sub_counts"]["plates_and_bowls"] == 18
    assert bundle["views"]["master"]["item.silverware"] == "18"
    assert bundle["views"]["master"]["item.plates_and_bowls"] == "18"


def test_active_saved_salad_order_refreshes_current_food_breakdown():
    items = [
        _row_item(
            1,
            "cobb_salad",
            "salads",
            9,
            "tray",
            "Cobb Salad Party Package",
            extras=[
                {"name": "dressing", "raw_text": "Most Popular"},
                {"name": "protein", "raw_text": "chicken"},
            ],
        ),
    ]
    stale_salad = {
        "item_key": "cobb_salad",
        "package_type": "salads",
        "qty": 9,
        "choices": {"packaging": "tray"},
        "proteins": [],
        "sides": [],
        "sauces": [{"name": "Dressing", "measure_type": "weight", "per_qty": 1.0, "unit": "oz", "total": 9.0}],
        "extras": [{"name": "dressing", "raw_text": "Most Popular"}],
        "counts": [],
        "summary_line": "9ppl - Cobb Salad | Dressing: Most Popular",
        "flags": [],
    }
    order = _row_order(headcount=0)
    order.delivery_date = "2099-01-01"
    bundle = reconstruct_bundle(
        order,
        items,
        {1: [SimpleNamespace(breakdown=stale_salad)]},
    )
    salad = next(b for b in bundle["kitchen_result"]["breakdowns"] if b["item_key"] == "cobb_salad")

    assert _line_named(salad["proteins"], "Chicken Diced")["total"] == 18.0
    assert _line_named(salad["sauces"], "Dressing - Most Popular")["total"] == 27.0
    assert bundle["views"]["master"]["item.salad_dressing"] == "27 Most Popular"


def test_individual_fajitas_get_silverware_without_plates_or_serving_tools():
    items = [
        _item("fajitas_mixed", "fajitas", qty=50, packaging="individual", beans="charro", tortillas="flour"),
        _item("tableware", "non_food_items"),
    ]

    result = build_kitchen_result(_order(items, headcount=50))
    tableware = next(b for b in result["breakdowns"] if b["item_key"] == "tableware")

    assert tableware["utensil_sub_counts"]["silverware"] == 53
    assert tableware["utensil_sub_counts"]["plates_and_bowls"] == 0
    assert tableware["utensil_sub_counts"]["black_tongs"] == 0
    assert tableware["utensil_sub_counts"]["catering_large_spoons"] == 0
    assert tableware["utensil_sub_counts"]["catering_small_spoons"] == 0


def test_mixed_bulk_and_individual_tableware_uses_food_qty_by_packaging():
    items = [
        _item("fajitas_mixed", "fajitas", qty=30, packaging="tray", beans="charro", tortillas="flour"),
        _item("cheese_enchiladas_individual", "enchiladas", qty=20, packaging="individual"),
        _item("tableware", "non_food_items"),
    ]

    result = build_kitchen_result(_order(items, headcount=100))
    tableware = next(b for b in result["breakdowns"] if b["item_key"] == "tableware")

    assert tableware["utensil_sub_counts"]["silverware"] == 53
    assert tableware["utensil_sub_counts"]["plates_and_bowls"] == 33


def test_individual_executive_package_gets_silverware_without_plates():
    items = [
        _item("cenas_exec_spread", "premium", qty=60, packaging="individual"),
        _item("tableware", "non_food_items"),
    ]
    items[0]["source"]["raw_alias"] = "Cenas Executive Fajita Spread"

    result = build_kitchen_result(_order(items, headcount=60))
    tableware = next(b for b in result["breakdowns"] if b["item_key"] == "tableware")

    assert tableware["utensil_sub_counts"]["silverware"] == 63
    assert tableware["utensil_sub_counts"]["plates_and_bowls"] == 0
    assert tableware["utensil_sub_counts"]["black_tongs"] == 0
    assert tableware["utensil_sub_counts"]["catering_large_spoons"] == 0
    assert tableware["utensil_sub_counts"]["catering_small_spoons"] == 0


def test_standalone_plates_item_is_zero_for_individual_packaging():
    items = [
        _item("fajitas_mixed", "fajitas", qty=50, packaging="individual"),
        _item("plates_and_bowls", "non_food_items"),
    ]

    result = build_kitchen_result(_order(items, headcount=50))
    plates = next(b for b in result["breakdowns"] if b["item_key"] == "plates_and_bowls")

    assert plates["utensil_sub_counts"]["plates_and_bowls"] == 0
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
