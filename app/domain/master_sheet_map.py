from __future__ import annotations

from typing import Dict, List, TypedDict
from collections import defaultdict

from app.domain.schemas import (
    NormalizedOrder,
    KitchenOrderResult,
    KitchenLineItem,
    PacketLineItem,
)
from app.domain.ticket_context import TicketContext
from app.domain.menu_catalog import MenuCatalog
from app.domain.rules_utils import oz_to_lb

FlatMap = Dict[str, str]


class RowSpec(TypedDict):
    key: str
    label: str
    section: str
    sort: int


def _fmt_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _fmt_num(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return f"{value:.2f}".rstrip("0").rstrip(".")

def _sum_line_items_to_master_component(
    totals: dict[str, float],
    line_items: list[KitchenLineItem],
    oz_totals: dict[str, float] | None = None,
) -> None:
    for li in line_items:
        name = li["name"]
        if li["measure_type"] == "weight":
            totals[name] += oz_to_lb(li["total"])
            if oz_totals is not None:
                oz_totals[name] += float(li["total"])
        else:
            totals[name] += float(li["total"])


def _sum_packet_items_to_master_component(
    totals: dict[str, float],
    line_items: list[PacketLineItem],
) -> None:
    for li in line_items:
        totals[li["name"]] += li["raw_packets"]


def _package_style(item_key: str) -> str:
    return {
        "fajitas_mixed": "Mixed",
        "fajitas_chicken": "Chicken",
        "fajitas_beef": "Beef",
        "veggie_fajitas": "Veggie",
        "brochette_shrimp": "Shrimp",
    }.get(item_key, "")


def build_master_output(
    order: NormalizedOrder,
    result: KitchenOrderResult,
    ctx: TicketContext,
    catalog: MenuCatalog,
) -> FlatMap:
    master: FlatMap = {}

    # -------------------------
    # META
    # -------------------------
    master["meta.order_id"] = _fmt_value(ctx["order_id"])
    master["meta.date"] = _fmt_value(ctx["date"])
    master["meta.store_origin"] = _fmt_value(ctx["origin_store_label"])
    master["meta.driver"] = _fmt_value(ctx["driver_name"])
    master["meta.client"] = _fmt_value(ctx["client"])
    master["meta.ask_for"] = _fmt_value(ctx["upon_delivery_ask_for"])
    master["meta.phone"] = _fmt_value(ctx["customer_phone"])
    master["meta.address"] = _fmt_value(ctx["delivery_address"])
    master["meta.headcount"] = _fmt_value(ctx["headcount"])
    master["meta.deliver_at"] = _fmt_value(ctx["deliver_at"])
    master["meta.kitchen_ready"] = _fmt_value(ctx["kitchen_ready_time"])
    master["meta.driver_depart"] = _fmt_value(ctx["driver_departure_time"])
    sr = ctx["setup_required"]
    master["meta.setup_required"] = "Yes" if sr is True else ("No" if sr is False else "N/A")
    master["meta.delivery_instructions"] = _fmt_value(ctx["delivery_instructions"])
    master["meta.notes"] = _fmt_value(ctx.get("notes"))
    dw = order.get("delivery_window") or {}
    start, end = dw.get("start", ""), dw.get("end", "")
    master["meta.deliver_window"] = f"{start} - {end}" if start or end else ""

    # -------------------------
    # PACKAGE STYLE / INDIVIDUAL
    # -------------------------
    package_styles: list[str] = []
    individual_lines: list[str] = []

    for b in result["breakdowns"]:
        style = _package_style(b["item_key"])
        if style and style not in package_styles:
            package_styles.append(style)

        if "INDIVIDUAL_PACKAGING_SUMMARY_ONLY" in b["flags"] and b["summary_line"]:
            summary = b["summary_line"]
            summary = summary.replace("Individual - ", "").strip()
            individual_lines.append(summary)


    master["meta.package_style"] = "; ".join(package_styles)
    master["meta.individual"] = "; ".join(individual_lines)

    # -------------------------
    # COMPONENT TOTALS
    # -------------------------
    component_totals: dict[str, float] = defaultdict(float)
    component_oz_totals: dict[str, float] = defaultdict(float)

    for b in result["breakdowns"]:
        _sum_line_items_to_master_component(component_totals, b["proteins"], component_oz_totals)
        _sum_line_items_to_master_component(component_totals, b["sides"], component_oz_totals)
        _sum_line_items_to_master_component(component_totals, b["sauces"], component_oz_totals)
        _sum_packet_items_to_master_component(component_totals, b["counts"])

    for component_name, total in component_totals.items():
        if catalog.get_component_sheet_meta(component_name):
            master[f"component.{component_name}"] = _fmt_num(total)

    salad_dressing_parts = []
    for b in result["breakdowns"]:
        if b["choices"].get("packaging") != "individual":
            dressings = [e["raw_text"] for e in b["extras"] if e["name"] == "dressing"]
            if dressings:
                dressing_oz = sum(li["total"] for li in b["sauces"] if li["name"] == "Dressing")
                salad_dressing_parts.append(f"{', '.join(dressings)} | {_fmt_num(oz_to_lb(dressing_oz))}")
    if salad_dressing_parts:
        master["item.salad_dressing"] = " | ".join(salad_dressing_parts)

    # -------------------------
    # DIRECT ORDERED ITEMS
    # -------------------------
    direct_item_totals: dict[str, float] = defaultdict(float)
    direct_item_containers: dict[str, str] = {}
    direct_item_notes: dict[str, str] =  {}

    for item in order["normalized_items"]:
        item_key = item["item_key"]
        sheet_meta = catalog.get_sheet_meta(item_key)
        if not sheet_meta:
            continue
        section = sheet_meta.get("section")
        if section in {"A_LA_CARTE", "SIDES", "ENCHILADAS", "DRINKS_DESSERTS", "NON_FOOD", "PREMIUM", "SALADS"}:
            if section == "SALADS" and item["choices"].get("packaging") == "individual":
                continue
            direct_item_totals[item_key] += float(item["qty"])
            if item.get("container"):
                direct_item_containers[item_key] = item["container"]
            if section == "ENCHILADAS":
                note_parts = []
                sauces = [e["raw_text"] for e in item["extras"] if e["name"] == "sauce"]
                if sauces:
                    note_parts.append(", ".join(sauces))
                if item["choices"]["packaging"] == "individual" and item["choices"]["beans"] != "none":
                    note_parts.append(item["choices"]["beans"].replace("_", " ").title())
                if note_parts:
                    direct_item_notes[item_key] = " | ".join(note_parts)

    for item_key, total in direct_item_totals.items():
        value = _fmt_num(total)
        container = direct_item_containers.get(item_key)
        if container:
            value = f"{value} | {container}"
        note = direct_item_notes.get(item_key)
        if note:
            value = f"{value} | {note}"
        master[f"item.{item_key}"] = value

    # Populate individual tableware keys from utensil_sub_counts (computed by rule_tableware)
    for b in result["breakdowns"]:
        sub = b.get("utensil_sub_counts") or {}
        for component, count in sub.items():
            master[f"item.{component}"] = _fmt_num(count)

    ice_required = False

    for item in order["normalized_items"]:
        if item["package_type"] == "beverages" and item["choices"]["with_ice"]:
            ice_required = True
            break

    master["meta.ice_required"] = "ICE REQUIRED" if ice_required else ""

    return master

def _pick(master: FlatMap, keys: list[str]) -> FlatMap:
    return {key: master.get(key, "") for key in keys}


def build_kitchen_output(master: FlatMap) -> FlatMap:
    keys = [
        "meta.kitchen_ready",
        "meta.order_id",
        "meta.date",
        "meta.store_origin",

        "component.Chicken",
        "component.Beef",
        "component.Veggie",
        "component.Shrimp (4-Pack)",
        "component.Rice",
        "component.Refried Beans",
        "component.Charro Beans",
        "component.Black Beans",
        "component.Flour Tortillas",
        "component.Corn Tortillas",

        "component.Onions",
        "component.Pico De Gallo",
        "component.Guacamole",
        "component.Sour Cream",
        "component.Red Sauce",
        "component.Green Sauce",
        "component.Chips",

        "meta.individual",

        "item.cenas_exec_spread",
        "item.queso_and_chips",
        "item.guac_and_chips",
        "item.cheese_enchiladas",
        "item.shredded_chicken_enchiladas",
        "item.ground_beef_enchiladas",
        "item.veggie_enchiladas",
        "item.beef_enchiladas",
        "item.chicken_enchiladas",
        "item.pork_enchiladas",
        "item.seafood_enchiladas",
        "item.tamales",
        "item.veggie_enchiladas_individual",
        "item.beef_enchiladas_individual",
        "item.chicken_enchiladas_individual",
        "item.chicken_fajita_enchiladas_individual",
        "item.cheese_enchiladas_individual",
        "item.ground_beef_enchiladas_individual",
        "item.pork_enchiladas_individual",
        "item.seafood_enchiladas_individual",
        "item.tamales_individual",
        "item.jumbo_brochette_shrimp",
        "item.andouille_grilled_sausage",
        "item.baja_ribs",

        "item.churros",
        "item.sopapillas",
        "item.tres_leches",
        "item.gallon_unsweet_tea",
        "item.gallon_sweet_tea",
        "item.sodas",
        "item.water",
        "item.jarritos_sodas",
        "item.lemonade",
        "meta.ice_required",
    ]
    return _pick(master, keys)


def build_driver_output(master: FlatMap) -> FlatMap:
    keys = [
        "meta.driver",
        "meta.order_id",
        "meta.date",
        "meta.store_origin",
        "meta.client",
        "meta.ask_for",
        "meta.address",
        "meta.kitchen_ready",
        "meta.driver_depart",
        "meta.deliver_window",
        "meta.delivery_instructions",
    ]
    return _pick(master, keys)


def build_prep_expo_output(master: FlatMap) -> FlatMap:
    keys = [
        "meta.order_id",
        "meta.date",
        "meta.store_origin",
        "meta.driver",
        "meta.client",
        "meta.headcount",
        "meta.deliver_at",
        "meta.kitchen_ready",
        "meta.driver_depart",
        "meta.delivery_instructions",
        "meta.notes",
        "meta.setup_required",
        "meta.package_style",

        "component.Chicken",
        "component.Beef",
        "component.Veggie",
        "component.Shrimp (4-Pack)",
        "component.Rice",
        "component.Refried Beans",
        "component.Charro Beans",
        "component.Black Beans",
        "component.Flour Tortillas",
        "component.Corn Tortillas",

        "component.Onions",
        "component.Pico De Gallo",
        "component.Guacamole",
        "component.Sour Cream",
        "component.Red Sauce",
        "component.Green Sauce",
        "component.Chips",

        "meta.individual",

        "item.cenas_exec_spread",
        "item.queso_and_chips",
        "item.guac_and_chips",
        "item.cheese_enchiladas",
        "item.shredded_chicken_enchiladas",
        "item.ground_beef_enchiladas",
        "item.veggie_enchiladas",
        "item.beef_enchiladas",
        "item.chicken_enchiladas",
        "item.pork_enchiladas",
        "item.seafood_enchiladas",
        "item.tamales",
        "item.veggie_enchiladas_individual",
        "item.beef_enchiladas_individual",
        "item.chicken_enchiladas_individual",
        "item.chicken_fajita_enchiladas_individual",
        "item.cheese_enchiladas_individual",
        "item.ground_beef_enchiladas_individual",
        "item.pork_enchiladas_individual",
        "item.seafood_enchiladas_individual",
        "item.tamales_individual",
        "item.jumbo_brochette_shrimp",
        "item.andouille_grilled_sausage",
        "item.baja_ribs",

        "item.churros",
        "item.sopapillas",
        "item.tres_leches",
        "item.gallon_unsweet_tea",
        "item.gallon_sweet_tea",
        "item.lemonade",
        "item.sodas",
        "item.water",
        "item.jarritos_sodas",
        "meta.ice_required",

        "item.plates_and_bowls",
        "item.silverware",
        "item.catering_large_spoons",
        "item.catering_small_spoons",
        "item.black_tongs",
    ]
    return _pick(master, keys)


def build_all_outputs(
    order: NormalizedOrder,
    result: KitchenOrderResult,
    ctx: TicketContext,
    catalog: MenuCatalog,
) -> dict[str, FlatMap]:
    master = build_master_output(order, result, ctx, catalog)
    return {
        "master": master,
        "kitchen": build_kitchen_output(master),
        "driver": build_driver_output(master),
        "prep_expo": build_prep_expo_output(master),
    }

MASTER_ROWS: list[RowSpec] = [
    # meta
    {"key": "meta.order_id", "label": "Order #", "section": "Header", "sort": 10},
    {"key": "meta.date", "label": "Date", "section": "Header", "sort": 20},
    {"key": "meta.store_origin", "label": "Location Origin", "section": "Header", "sort": 30},
    {"key": "meta.driver", "label": "Driver", "section": "Header", "sort": 40},
    {"key": "meta.client", "label": "Client", "section": "Header", "sort": 50},
    {"key": "meta.ask_for", "label": "Ask For", "section": "Header", "sort": 60},
    {"key": "meta.phone", "label": "Phone", "section": "Header", "sort": 70},
    {"key": "meta.address", "label": "Address", "section": "Header", "sort": 80},
    {"key": "meta.headcount", "label": "Headcount", "section": "Header", "sort": 90},
    {"key": "meta.deliver_at", "label": "Deliver At", "section": "Header", "sort": 100},
    {"key": "meta.kitchen_ready", "label": "Kitchen Ready", "section": "Header", "sort": 110},
    {"key": "meta.driver_depart", "label": "Departure", "section": "Header", "sort": 120},
    {"key": "meta.setup_required", "label": "Setup", "section": "Header", "sort": 130},
    {"key": "meta.delivery_instructions", "label": "Instructions", "section": "Header", "sort": 135},
    {"key": "meta.notes", "label": "Notes", "section": "Header", "sort": 137},

    {"key": "meta.individual", "label": "Party Package", "section": "Individual Packages", "sort": 150},
    {"key": "item.cenas_exec_spread", "label": "Executive Package", "section": "Individual Packages", "sort": 160},
    # hot food
    {"key": "component.Chicken", "label": "Chicken (Lb)", "section": "Hot Food", "sort": 200},
    {"key": "component.Beef", "label": "Beef (Lb)", "section": "Hot Food", "sort": 210},
    {"key": "component.Veggie", "label": "Veggies (Lb)", "section": "Hot Food", "sort": 220},
    {"key": "component.Shrimp (4-Pack)", "label": "Brochette Shrimp (4-Pack)", "section": "Hot Food", "sort": 230},
    {"key": "component.Onions", "label": "Onions (Lb)", "section": "Hot Food", "sort": 235},
    {"key": "component.Rice", "label": "Rice (Lb)", "section": "Hot Food", "sort": 240},
    {"key": "component.Refried Beans", "label": "Refried (Lb)", "section": "Hot Food", "sort": 250},
    {"key": "component.Charro Beans", "label": "Charro (Lb)", "section": "Hot Food", "sort": 260},
    {"key": "component.Black Beans", "label": "Black (Lb)", "section": "Hot Food", "sort": 270},
    {"key": "component.Flour Tortillas", "label": "Flour (pkts of 2)", "section": "Hot Food", "sort": 280},
    {"key": "component.Corn Tortillas", "label": "Corn (pkts of 3)", "section": "Hot Food", "sort": 290},
    # cold food
    {"key": "component.Pico De Gallo", "label": "Pico De Gallo (Lb)", "section": "Cold Food", "sort": 310},
    {"key": "component.Guacamole", "label": "Guacamole (Lb)", "section": "Cold Food", "sort": 320},
    {"key": "component.Sour Cream", "label": "Sour Cream (Lb)", "section": "Cold Food", "sort": 330},
    {"key": "component.Lettuce", "label": "Lettuce (Lb)", "section": "Cold Food", "sort": 331},
    {"key": "component.Avocado Diced", "label": "Avocado Diced (Lb)", "section": "Cold Food", "sort": 332},
    {"key": "component.Tomatoes Diced", "label": "Tomatoes Diced (Lb)", "section": "Cold Food", "sort": 333},
    {"key": "component.Cucumber Diced", "label": "Cucumber Diced (Lb)", "section": "Cold Food", "sort": 334},
    {"key": "component.Grated Cheese", "label": "Grated Cheese (Lb)", "section": "Cold Food", "sort": 335},
    {"key": "component.Bacon", "label": "Bacon (Lb)", "section": "Cold Food", "sort": 336},
    {"key": "component.Egg", "label": "Egg (Lb)", "section": "Cold Food", "sort": 337},
    {"key": "component.Black Olives", "label": "Black Olives (Lb)", "section": "Cold Food", "sort": 338},
    {"key": "component.Beef Diced", "label": "Beef Diced (Lb)", "section": "Cold Food", "sort": 339},
    {"key": "component.Chicken Diced", "label": "Chicken Diced (Lb)", "section": "Cold Food", "sort": 340},
    {"key": "item.salad_dressing", "label": "Salad Dressing (Lb)", "section": "Cold Food", "sort": 341},
    {"key": "component.Red Sauce", "label": "Red Sauce (Lb)", "section": "Cold Food", "sort": 342},
    {"key": "component.Green Sauce", "label": "Green Sauce (Lb)", "section": "Cold Food", "sort": 350},
    {"key": "component.Chips", "label": "Chips (Lb)", "section": "Cold Food", "sort": 360},
    # other menu items that are entirely unimportant
    {"key": "item.jumbo_brochette_shrimp", "label": "Brochette Shrimp (4-Pack)", "section": "A La Carte", "sort": 500},
    {"key": "item.andouille_grilled_sausage", "label": "Grilled Sausage (4-Pack)", "section": "A La Carte", "sort": 510},
    {"key": "item.baja_ribs", "label": "Ribs (4-Pack)", "section": "A La Carte", "sort": 520},
    {"key": "item.beef_faj_per_pound", "label": "Beef Fajita (Lb)", "section": "A La Carte", "sort": 525},
    {"key": "item.chicken_faj_per_pound", "label": "Chicken Fajita (Lb)", "section": "A La Carte", "sort": 530},

    {"key": "item.queso_and_chips", "label": "Queso", "section": "Sides", "sort": 540},
    {"key": "item.guac_and_chips", "label": "Guacamole", "section": "Sides", "sort": 550},
    {"key": "item.rice", "label": "Rice", "section": "Sides", "sort": 560},
    {"key": "item.refried_beans", "label": "Refried", "section": "Sides", "sort": 570},
    {"key": "item.black_beans", "label": "Black", "section": "Sides", "sort": 580},
    {"key": "item.charro_beans", "label": "Charro", "section": "Sides", "sort": 590},
    {"key": "item.grated_cheese", "label": "Grated Cheese", "section": "Sides", "sort": 600},
    {"key": "item.pickled_jalapenos", "label": "Pickled Jalapeños", "section": "Sides", "sort": 610},
    {"key": "item.fresh_jalapenos", "label": "Fresh Jalapeños", "section": "Sides", "sort": 620},
    {"key": "item.sour_cream", "label": "Sour Cream", "section": "Sides", "sort": 630},
    {"key": "item.pico_de_gallo", "label": "Pico De Gallo", "section": "Sides", "sort": 640},
    {"key": "item.red_sauce", "label": "Red Sauce", "section": "Sides", "sort": 650},
    {"key": "item.green_sauce", "label": "Green Sauce", "section": "Sides", "sort": 660},
    {"key": "item.fresh_avocado", "label": "Fresh Avocado", "section": "Sides", "sort": 670},
    {"key": "item.flour_tort", "label": "Flour (pkts of 2)", "section": "Sides", "sort": 690},
    {"key": "item.corn_tort", "label": "Corn (pkts of 3)", "section": "Sides", "sort": 700},

    {"key": "item.cheese_enchiladas", "label": "Cheese", "section": "Enchiladas (1 Dozen)", "sort": 710},
    {"key": "item.shredded_chicken_enchiladas", "label": "Shredded Chicken", "section": "Enchiladas (1 Dozen)", "sort": 720},
    {"key": "item.ground_beef_enchiladas", "label": "Ground Beef", "section": "Enchiladas (1 Dozen)", "sort": 730},
    {"key": "item.veggie_enchiladas", "label": "Veggie", "section": "Enchiladas (1 Dozen)", "sort": 740},
    {"key": "item.beef_enchiladas", "label": "Beef Fajita", "section": "Enchiladas (1 Dozen)", "sort": 750},
    {"key": "item.chicken_enchiladas", "label": "Chicken Fajita", "section": "Enchiladas (1 Dozen)", "sort": 760},
    {"key": "item.pork_enchiladas", "label": "Pork", "section": "Enchiladas (1 Dozen)", "sort": 770},
    {"key": "item.seafood_enchiladas", "label": "Seafood", "section": "Enchiladas (1 Dozen)", "sort": 780},
    {"key": "item.tamales", "label": "Tamales", "section": "Enchiladas (1 Dozen)", "sort": 790},

    {"key": "item.cheese_enchiladas_individual", "label": "Cheese", "section": "Enchiladas (Individually Packaged)", "sort": 800},
    {"key": "item.shredded_chicken_enchiladas_individual", "label": "Shredded Chicken", "section": "Enchiladas (Individually Packaged)", "sort": 810},
    {"key": "item.ground_beef_enchiladas_individual", "label": "Ground Beef", "section": "Enchiladas (Individually Packaged)", "sort": 820},
    {"key": "item.veggie_enchiladas_individual", "label": "Veggie", "section": "Enchiladas (Individually Packaged)", "sort": 830},
    {"key": "item.beef_enchiladas_individual", "label": "Beef Fajita", "section": "Enchiladas (Individually Packaged)", "sort": 840},
    {"key": "item.chicken_enchiladas_individual", "label": "Chicken Fajita", "section": "Enchiladas (Individually Packaged)", "sort": 850},
    {"key": "item.pork_enchiladas_individual", "label": "Pork", "section": "Enchiladas (Individually Packaged)", "sort": 860},
    {"key": "item.seafood_enchiladas_individual", "label": "Seafood", "section": "Enchiladas (Individually Packaged)", "sort": 870},
    {"key": "item.tamales_individual", "label": "Tamales", "section": "Enchiladas (Individually Packaged)", "sort": 880},

    {"key": "item.churros", "label": "Churros", "section": "Drinks & Desserts", "sort": 900},
    {"key": "item.sopapillas", "label": "Sopapillas", "section": "Drinks & Desserts", "sort": 910},
    {"key": "item.tres_leches", "label": "Tres Leches", "section": "Drinks & Desserts", "sort": 920},
    {"key": "item.gallon_unsweet_tea", "label": "(G) Unsweet Tea", "section": "Drinks & Desserts", "sort": 930},
    {"key": "item.gallon_sweet_tea", "label": "(G) Sweet Tea", "section": "Drinks & Desserts", "sort": 940},
    {"key": "item.lemonade", "label": "(G) Lemonade", "section": "Drinks & Desserts", "sort": 950},
    {"key": "item.sodas", "label": "Sodas", "section": "Drinks & Desserts", "sort": 951},
    {"key": "item.water", "label": "Bottled Waters", "section": "Drinks & Desserts", "sort": 952},
    {"key": "item.jarritos_sodas", "label": "Jarritos", "section": "Drinks & Desserts", "sort": 953},
    {"key": "meta.ice_required", "label": "With Ice", "section": "Drinks & Desserts", "sort": 955},

    {"key": "item.plates_and_bowls", "label": "Plates", "section": "Utensils", "sort": 1000},
    {"key": "item.silverware", "label": "Silverware", "section": "Utensils", "sort": 1020},
    {"key": "item.catering_large_spoons", "label": "Black Large Spoons", "section": "Utensils", "sort": 1030},
    {"key": "item.catering_small_spoons", "label": "Black Small Spoons", "section": "Utensils", "sort": 1040},
    {"key": "item.black_tongs", "label": "Black Tongs", "section": "Utensils", "sort": 1050},
]

KITCHEN_ROWS: list[RowSpec] = [
    # meta
    {"key": "meta.order_id", "label": "Order #", "section": "Header", "sort": 5},
    {"key": "meta.kitchen_ready", "label": "Kitchen Ready", "section": "Header", "sort": 10},
    {"key": "meta.date", "label": "Date", "section": "Header", "sort": 30},
    {"key": "meta.store_origin", "label": "Location Origin", "section": "Header", "sort": 40},

    {"key": "meta.individual", "label": "Party Package", "section": "Individual Packages", "sort": 45},
    {"key": "item.cenas_exec_spread", "label": "Executive Package", "section": "Individual Packages", "sort": 50},
    # hot food breakdown
    {"key": "component.Chicken", "label": "Chicken (Lb)", "section": "Hot Food", "sort": 100},
    {"key": "component.Beef", "label": "Beef (Lb)", "section": "Hot Food", "sort": 110},
    {"key": "component.Veggie", "label": "Veggies (Lb)", "section": "Hot Food", "sort": 120},
    {"key": "component.Shrimp (4-Pack)", "label": "Brochette Shrimp (4-Pack)", "section": "Hot Food", "sort": 130},
    {"key": "component.Onions", "label": "Onions (Lb)", "section": "Hot Food", "sort": 135},
    {"key": "component.Rice", "label": "Rice (Lb)", "section": "Hot Food", "sort": 140},
    {"key": "component.Flour Tortillas", "label": "Flour (pkts of 2)", "section": "Hot Food", "sort": 180},
    {"key": "component.Corn Tortillas", "label": "Corn (pkts of 3)", "section": "Hot Food", "sort": 190},
    # cold food breakdown
    {"key": "component.Pico De Gallo", "label": "Pico De Gallo (Lb)", "section": "Cold Food", "sort": 210},
    {"key": "component.Guacamole", "label": "Guacamole (Lb)", "section": "Cold Food", "sort": 220},
    {"key": "component.Sour Cream", "label": "Sour Cream (Lb)", "section": "Cold Food", "sort": 230},
    {"key": "component.Avocado Diced", "label": "Avocado Diced (Lb)", "section": "Cold Food", "sort": 332},
    {"key": "component.Tomatoes Diced", "label": "Tomatoes Diced (Lb)", "section": "Cold Food", "sort": 333},
    {"key": "component.Cucumber Diced", "label": "Cucumber Diced (Lb)", "section": "Cold Food", "sort": 334},
    {"key": "component.Grated Cheese", "label": "Grated Cheese (Lb)", "section": "Cold Food", "sort": 335},
    {"key": "component.Bacon", "label": "Bacon (Lb)", "section": "Cold Food", "sort": 336},
    {"key": "component.Egg", "label": "Egg (Lb)", "section": "Cold Food", "sort": 337},
    {"key": "component.Black Olives", "label": "Black Olives (Lb)", "section": "Cold Food", "sort": 338},
    {"key": "component.Beef Diced", "label": "Beef Diced (Lb)", "section": "Cold Food", "sort": 339},
    {"key": "component.Chicken Diced", "label": "Chicken Diced (Lb)", "section": "Cold Food", "sort": 340},
    # other menu items
    {"key": "item.jumbo_brochette_shrimp", "label": "Brochette Shrimp (4-Pack)", "section": "A La Carte", "sort": 400},
    {"key": "item.andouille_grilled_sausage", "label": "Grilled Sausage (4-Pack)", "section": "A La Carte", "sort": 410},
    {"key": "item.baja_ribs", "label": "Ribs (4-Pack)", "section": "A La Carte", "sort": 420},
    {"key": "item.beef_faj_per_pound", "label": "Beef Fajita (Lb)", "section": "A La Carte", "sort": 425},
    {"key": "item.chicken_faj_per_pound", "label": "Chicken Fajita (Lb)", "section": "A La Carte", "sort": 430},

    {"key": "item.queso_and_chips", "label": "Queso", "section": "Sides", "sort": 540},
    {"key": "item.guac_and_chips", "label": "Guacamole", "section": "Sides", "sort": 550},      
    {"key": "item.rice", "label": "Rice", "section": "Sides", "sort": 560},
    {"key": "item.refried_beans", "label": "Refried", "section": "Sides", "sort": 570},
    {"key": "item.black_beans", "label": "Black", "section": "Sides", "sort": 580},
    {"key": "item.charro_beans", "label": "Charro", "section": "Sides", "sort": 590},
    {"key": "item.grated_cheese", "label": "Grated Cheese", "section": "Sides", "sort": 600},
    {"key": "item.pickled_jalapenos", "label": "Pickled Jalapeños", "section": "Sides", "sort": 610},
    {"key": "item.fresh_jalapenos", "label": "Fresh Jalapeños", "section": "Sides", "sort": 620},
    {"key": "item.sour_cream", "label": "Sour Cream", "section": "Sides", "sort": 630},
    {"key": "item.pico_de_gallo", "label": "Pico De Gallo", "section": "Sides", "sort": 640},
    {"key": "item.fresh_avocado", "label": "Fresh Avocado", "section": "Sides", "sort": 670},
    {"key": "item.flour_tort", "label": "Flour (pkts of 2)", "section": "Sides", "sort": 690},
    {"key": "item.corn_tort", "label": "Corn (pkts of 3)", "section": "Sides", "sort": 700},

    {"key": "item.cheese_enchiladas", "label": "Cheese", "section": "Enchiladas (1 Dozen)", "sort": 710},
    {"key": "item.shredded_chicken_enchiladas", "label": "Shredded Chicken", "section": "Enchiladas (1 Dozen)", "sort": 720},
    {"key": "item.ground_beef_enchiladas", "label": "Ground Beef", "section": "Enchiladas (1 Dozen)", "sort": 730},
    {"key": "item.veggie_enchiladas", "label": "Veggie", "section": "Enchiladas (1 Dozen)", "sort": 740},
    {"key": "item.beef_enchiladas", "label": "Beef Fajita", "section": "Enchiladas (1 Dozen)", "sort": 750},
    {"key": "item.chicken_enchiladas", "label": "Chicken Fajita", "section": "Enchiladas (1 Dozen)", "sort": 760},
    {"key": "item.pork_enchiladas", "label": "Pork", "section": "Enchiladas (1 Dozen)", "sort": 770},
    {"key": "item.seafood_enchiladas", "label": "Seafood", "section": "Enchiladas (1 Dozen)", "sort": 780},
    {"key": "item.tamales", "label": "Tamales", "section": "Enchiladas (1 Dozen)", "sort": 790},

    {"key": "item.cheese_enchiladas_individual", "label": "Cheese", "section": "Enchiladas (Individually Packaged)", "sort": 800},
    {"key": "item.shredded_chicken_enchiladas_individual", "label": "Shredded Chicken", "section": "Enchiladas (Individually Packaged)", "sort": 810},
    {"key": "item.ground_beef_enchiladas_individual", "label": "Ground Beef", "section": "Enchiladas (Individually Packaged)", "sort": 820},
    {"key": "item.veggie_enchiladas_individual", "label": "Veggie", "section": "Enchiladas (Individually Packaged)", "sort": 830},
    {"key": "item.beef_enchiladas_individual", "label": "Beef Fajita", "section": "Enchiladas (Individually Packaged)", "sort": 840},
    {"key": "item.chicken_enchiladas_individual", "label": "Chicken Fajita", "section": "Enchiladas (Individually Packaged)", "sort": 850},
    {"key": "item.pork_enchiladas_individual", "label": "Pork", "section": "Enchiladas (Individually Packaged)", "sort": 860},
    {"key": "item.seafood_enchiladas_individual", "label": "Seafood", "section": "Enchiladas (Individually Packaged)", "sort": 870},
    {"key": "item.tamales_individual", "label": "Tamales", "section": "Enchiladas (Individually Packaged)", "sort": 880},

    {"key": "item.churros", "label": "Churros", "section": "Drinks & Desserts", "sort": 900},
    {"key": "item.sopapillas", "label": "Sopapillas", "section": "Drinks & Desserts", "sort": 910},
    {"key": "item.tres_leches", "label": "Tres Leches", "section": "Drinks & Desserts", "sort": 920},
    {"key": "item.gallon_unsweet_tea", "label": "(G) Unsweet Tea", "section": "Drinks & Desserts", "sort": 930},
    {"key": "item.gallon_sweet_tea", "label": "(G) Sweet Tea", "section": "Drinks & Desserts", "sort": 940},
    {"key": "item.lemonade", "label": "(G) Lemonade", "section": "Drinks & Desserts", "sort": 950},
    {"key": "item.sodas", "label": "Sodas", "section": "Drinks & Desserts", "sort": 951},
    {"key": "item.water", "label": "Bottled Waters", "section": "Drinks & Desserts", "sort": 952},
    {"key": "item.jarritos_sodas", "label": "Jarritos", "section": "Drinks & Desserts", "sort": 953},
    {"key": "meta.ice_required", "label": "With Ice", "section": "Drinks & Desserts", "sort": 960},
]

DRIVER_ROWS: list[RowSpec] = [
    {"key": "meta.order_id", "label": "Order #", "section": "Header", "sort": 5 },
    {"key": "meta.driver", "label": "Driver", "section": "Header", "sort": 10},
    {"key": "meta.date", "label": "Date", "section": "Header", "sort": 30},
    {"key": "meta.store_origin", "label": "Location Origin", "section": "Header", "sort": 40},
    {"key": "meta.client", "label": "Client Name", "section": "Header", "sort": 50},
    {"key": "meta.ask_for", "label": "Ask For", "section": "Header", "sort": 60},
    {"key": "meta.delivery_instructions", "label": "Instructions", "section": "Header", "sort": 65},
    {"key": "meta.address", "label": "Address", "section": "Header", "sort": 70},
    {"key": "meta.kitchen_ready", "label": "Kitchen Ready", "section": "Header", "sort": 80},
    {"key": "meta.driver_depart", "label": "Departure", "section": "Header", "sort": 90},
    {"key": "meta.deliver_window", "label": "Delivery Window", "section": "Header", "sort": 100},
]

PREP_EXPO_ROWS: list[RowSpec] = [
    # meta
    {"key": "meta.order_id", "label": "Order #", "section": "Header", "sort": 10},
    {"key": "meta.date", "label": "Date", "section": "Header", "sort": 20},
    {"key": "meta.store_origin", "label": "Location Origin", "section": "Header", "sort": 30},
    {"key": "meta.driver", "label": "Driver", "section": "Header", "sort": 40},
    {"key": "meta.client", "label": "Client Name", "section": "Header", "sort": 50},
    {"key": "meta.headcount", "label": "Headcount", "section": "Header", "sort": 60},
    {"key": "meta.deliver_at", "label": "Deliver At", "section": "Header", "sort": 70},
    {"key": "meta.kitchen_ready", "label": "Kitchen Ready", "section": "Header", "sort": 80},
    {"key": "meta.driver_depart", "label": "Departure Time", "section": "Header", "sort": 90},
    {"key": "meta.delivery_instructions", "label": "Instructions", "section": "Header", "sort": 95},
    {"key": "meta.notes", "label": "Notes", "section": "Header", "sort": 97},
    {"key": "meta.setup_required", "label": "Setup", "section": "Header", "sort": 100},
    {"key": "meta.individual", "label": "Party Package", "section": "Individual Packages", "sort": 110},
    {"key": "item.cenas_exec_spread", "label": "Executive Package", "section": "Individual Packages", "sort": 120},
    # hot food
    {"key": "component.Chicken", "label": "Chicken (lb)", "section": "Hot Food", "sort": 200},
    {"key": "component.Beef", "label": "Beef (lb)", "section": "Hot Food", "sort": 210},
    {"key": "component.Veggie", "label": "Veggies (lb)", "section": "Hot Food", "sort": 220},
    {"key": "component.Shrimp (4-Pack)", "label": "Brochette Shrimp (4-pack pkts)", "section": "Hot Food", "sort": 230},
    {"key": "component.Onions", "label": "Onions (lb)", "section": "Hot Food", "sort": 235},
    {"key": "component.Rice", "label": "Rice (lb)", "section": "Hot Food", "sort": 240},
    {"key": "component.Refried Beans", "label": "Refried (lb)", "section": "Hot Food", "sort": 250},
    {"key": "component.Charro Beans", "label": "Charro (lb)", "section": "Hot Food", "sort": 260},
    {"key": "component.Black Beans", "label": "Black (lb)", "section": "Hot Food", "sort": 270},
    {"key": "component.Flour Tortillas", "label": "Flour (pkts of 2)", "section": "Hot Food", "sort": 280},
    {"key": "component.Corn Tortillas", "label": "Corn (pkts of 3)", "section": "Hot Food", "sort": 290},
    # cold food
    {"key": "component.Pico De Gallo", "label": "Pico De Gallo (lb)", "section": "Cold Food", "sort": 310},
    {"key": "component.Guacamole", "label": "Guacamole (lb)", "section": "Cold Food", "sort": 320},
    {"key": "component.Sour Cream", "label": "Sour Cream (lb)", "section": "Cold Food", "sort": 330},
    {"key": "component.Avocado Diced", "label": "Avocado Diced (Lb)", "section": "Cold Food", "sort": 332},
    {"key": "component.Tomatoes Diced", "label": "Tomatoes Diced (Lb)", "section": "Cold Food", "sort": 333},
    {"key": "component.Cucumber Diced", "label": "Cucumber Diced (Lb)", "section": "Cold Food", "sort": 334},
    {"key": "component.Grated Cheese", "label": "Grated Cheese (Lb)", "section": "Cold Food", "sort": 335},
    {"key": "component.Bacon", "label": "Bacon (Lb)", "section": "Cold Food", "sort": 336},
    {"key": "component.Egg", "label": "Egg (Lb)", "section": "Cold Food", "sort": 337},
    {"key": "component.Black Olives", "label": "Black Olives (Lb)", "section": "Cold Food", "sort": 338},
    {"key": "component.Beef Diced", "label": "Beef Diced (Lb)", "section": "Cold Food", "sort": 339},
    {"key": "component.Chicken Diced", "label": "Chicken Diced (Lb)", "section": "Cold Food", "sort": 340},
    {"key": "component.Red Sauce", "label": "Red Sauce (lb)", "section": "Cold Food", "sort": 341},
    {"key": "component.Green Sauce", "label": "Green Sauce (lb)", "section": "Cold Food", "sort": 350},
    {"key": "component.Chips", "label": "Chips (lb)", "section": "Cold Food", "sort": 360},
    # other menu items
    {"key": "item.jumbo_brochette_shrimp", "label": "Brochette Shrimp (4-Pack)", "section": "A La Carte", "sort": 500},
    {"key": "item.andouille_grilled_sausage", "label": "Grilled Sausage (4-Pack)", "section": "A La Carte", "sort": 510},
    {"key": "item.baja_ribs", "label": "Ribs (4-Pack)", "section": "A La Carte", "sort": 520},
    {"key": "item.beef_faj_per_pound", "label": "Beef Fajita (Lb)", "section": "A La Carte", "sort": 525},
    {"key": "item.chicken_faj_per_pound", "label": "Chicken Fajita (Lb)", "section": "A La Carte", "sort": 530},

    {"key": "item.queso_and_chips", "label": "Queso", "section": "Sides", "sort": 540},
    {"key": "item.guac_and_chips", "label": "Guacamole", "section": "Sides", "sort": 550},
    {"key": "item.rice", "label": "Rice", "section": "Sides", "sort": 560},
    {"key": "item.refried_beans", "label": "Refried", "section": "Sides", "sort": 570},
    {"key": "item.black_beans", "label": "Black", "section": "Sides", "sort": 580},
    {"key": "item.charro_beans", "label": "Charro", "section": "Sides", "sort": 590},
    {"key": "item.grated_cheese", "label": "Grated Cheese", "section": "Sides", "sort": 600},
    {"key": "item.pickled_jalapenos", "label": "Pickled Jalapeños", "section": "Sides", "sort": 610},
    {"key": "item.fresh_jalapenos", "label": "Fresh Jalapeños", "section": "Sides", "sort": 620},
    {"key": "item.sour_cream", "label": "Sour Cream", "section": "Sides", "sort": 630},
    {"key": "item.pico_de_gallo", "label": "Pico De Gallo", "section": "Sides", "sort": 640},
    {"key": "item.red_sauce", "label": "Red Sauce", "section": "Sides", "sort": 650},
    {"key": "item.green_sauce", "label": "Green Sauce", "section": "Sides", "sort": 660},
    {"key": "item.fresh_avocado", "label": "Fresh Avocado", "section": "Sides", "sort": 670},
    {"key": "item.flour_tort", "label": "Flour", "section": "Sides", "sort": 690},
    {"key": "item.corn_tort", "label": "Corn", "section": "Sides", "sort": 700},

    {"key": "item.cheese_enchiladas", "label": "Cheese", "section": "Enchiladas (1 Dozen)", "sort": 710},
    {"key": "item.shredded_chicken_enchiladas", "label": "Shredded Chicken", "section": "Enchiladas (1 Dozen)", "sort": 720},
    {"key": "item.ground_beef_enchiladas", "label": "Ground Beef", "section": "Enchiladas (1 Dozen)", "sort": 730},
    {"key": "item.veggie_enchiladas", "label": "Veggie", "section": "Enchiladas (1 Dozen)", "sort": 740},
    {"key": "item.beef_enchiladas", "label": "Beef Fajita", "section": "Enchiladas (1 Dozen)", "sort": 750},
    {"key": "item.chicken_enchiladas", "label": "Chicken Fajita", "section": "Enchiladas (1 Dozen)", "sort": 760},
    {"key": "item.pork_enchiladas", "label": "Pork Enchiladas", "section": "Enchiladas (1 Dozen)", "sort": 770},
    {"key": "item.seafood_enchiladas", "label": "Seafood", "section": "Enchiladas (1 Dozen)", "sort": 780},
    {"key": "item.tamales", "label": "Tamales", "section": "Enchiladas (1 Dozen)", "sort": 790},

    {"key": "item.cheese_enchiladas_individual", "label": "Cheese", "section": "Enchiladas (Individually Packaged)", "sort": 800},
    {"key": "item.shredded_chicken_enchiladas_individual", "label": "Shredded Chicken", "section": "Enchiladas (Individually Packaged)", "sort": 810},
    {"key": "item.ground_beef_enchiladas_individual", "label": "Ground Beef", "section": "Enchiladas (Individually Packaged)", "sort": 820},
    {"key": "item.veggie_enchiladas_individual", "label": "Veggie", "section": "Enchiladas (Individually Packaged)", "sort": 830},
    {"key": "item.beef_enchiladas_individual", "label": "Beef Fajita", "section": "Enchiladas (Individually Packaged)", "sort": 840},
    {"key": "item.chicken_enchiladas_individual", "label": "Chicken Fajita", "section": "Enchiladas (Individually Packaged)", "sort": 850},
    {"key": "item.pork_enchiladas_individual", "label": "Pork", "section": "Enchiladas (Individually Packaged)", "sort": 860},
    {"key": "item.seafood_enchiladas_individual", "label": "Seafood", "section": "Enchiladas (Individually Packaged)", "sort": 870},
    {"key": "item.tamales_individual", "label": "Tamales", "section": "Enchiladas (Individually Packaged)", "sort": 880},

    {"key": "item.churros", "label": "Churros", "section": "Drinks & Desserts", "sort": 900},
    {"key": "item.sopapillas", "label": "Sopapillas", "section": "Drinks & Desserts", "sort": 910},
    {"key": "item.tres_leches", "label": "Tres Leches", "section": "Drinks & Desserts", "sort": 920},
    {"key": "item.gallon_unsweet_tea", "label": "(G) Unsweet Tea", "section": "Drinks & Desserts", "sort": 930},
    {"key": "item.gallon_sweet_tea", "label": "(G) Sweet Tea", "section": "Drinks & Desserts", "sort": 940},
    {"key": "item.lemonade", "label": "(G) Lemonade", "section": "Drinks & Desserts", "sort": 950},
    {"key": "item.sodas", "label": "Sodas", "section": "Drinks & Desserts", "sort": 951},
    {"key": "item.water", "label": "Bottled Waters", "section": "Drinks & Desserts", "sort": 952},
    {"key": "item.jarritos_sodas", "label": "Jarritos", "section": "Drinks & Desserts", "sort": 953},
    {"key": "meta.ice_required", "label": "With Ice", "section": "Drinks & Desserts", "sort": 955},

    {"key": "item.plates_and_bowls", "label": "Plates", "section": "Utensils", "sort": 1000},
    {"key": "item.silverware", "label": "Silverware", "section": "Utensils", "sort": 1020},
    {"key": "item.catering_large_spoons", "label": "Black Large Spoons", "section": "Utensils", "sort": 1030},
    {"key": "item.catering_small_spoons", "label": "Black Small Spoons", "section": "Utensils", "sort": 1040},
    {"key": "item.black_tongs", "label": "Black Tongs", "section": "Utensils", "sort": 1050},
]

VIEW_ROWS: dict[str, list[RowSpec]] = {
    "master": MASTER_ROWS,
    "kitchen": KITCHEN_ROWS,
    "driver": DRIVER_ROWS,
    "prep_expo": PREP_EXPO_ROWS,
}