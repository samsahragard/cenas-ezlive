# dispatcher: item_key + packaging -> calls correct rule
from __future__ import annotations
from typing import Callable, Dict, List

from app.domain.schemas import NormalizedOrder, NormalizedItem, PrepBreakdown, KitchenOrderResult
from app.domain.rules_fajitas import rule_fajitas
from app.domain.rules_brochette import rule_brochette
from app.domain.rules_veggie import rule_veggie
from app.domain.rules_salads import rule_salads
from app.domain.rules_premium import rule_premium

def _empty_breakdown(item: NormalizedItem) -> PrepBreakdown:
    return {
        "item_key": item["item_key"],
        "package_type": item["package_type"],
        "qty": item["qty"],
        "choices": item["choices"],
        "proteins": [],
        "sides": [],
        "sauces": [],
        "extras": [],
        "counts": [],
        "summary_line": None,
        "flags": item["flags"][:],
    }

def rule_other(item: NormalizedItem, order: NormalizedOrder) -> PrepBreakdown:
    b = _empty_breakdown(item)
    b["summary_line"] = f'{item["qty"]}x {item["source"]["raw_alias"]}'
    b["flags"].append("NO_RULE_APPLIED")
    return b

def rule_sides(item: NormalizedItem, order: NormalizedOrder) -> PrepBreakdown:
    b = _empty_breakdown(item)
    return b

def rule_beverages(item: NormalizedItem, order: NormalizedOrder) -> PrepBreakdown:
    b = _empty_breakdown(item)
    flavor_parts = [e["raw_text"] for e in item["extras"]]
    flavor_str = ", ".join(flavor_parts) if flavor_parts else "no flavors specified"
    ice_str = " (with ice)" if item["choices"]["with_ice"] else ""
    b["summary_line"] = f'{item["qty"]}x {item["source"]["raw_alias"]} — {flavor_str}{ice_str}'
    return b

_TRAY_TYPES = {"fajitas", "veggie_fajitas", "brochette_shrimp", "premium", "enchiladas", "salads"}
_PLATES_BUFFER = 3

_SIDES_SMALL_SPOON_KEYS = {
    "queso_and_chips", "guac_and_chips", "refried_beans", "charro_beans",
    "black_beans", "rice", "sour_cream", "pico_de_gallo", "fresh_avocado",
}

_SIDES_TONGS_KEYS = {
    "grated_cheese", "pickled_jalapenos", "fresh_jalapenos", "jumbo_brochette_shrimp", "andouille_grilled_sausage",
    "baja_ribs", "beef_faj_per_pound", "chicken_faj_per_pound",
}

_SAUCE_KEYS = {"red_sauce", "green_sauce"}

_PREP_TONG_COMPONENT_VALUES = {
    "Chicken", "Beef", "Veggie", "Shrimp (4-Pack)", "Onions", "Lettuce", "Bacon", "Chicken Diced", "Beef Diced", "Churros",
}

_PREP_LARGE_SPOON_COMPONENT_VALUES = {
    "Rice", "Refried Beans", "Charro Beans", "Black Beans"
}

_PREP_SMALL_SPOON_COMPONENT_VALUES = {
    "Guacamole", "Sour Cream", "Avocado Diced", "Cucumber Diced", "Egg", "Grated Cheese", "Queso Blanco",
}

_PREP_SAUCE_COMPONENT_VALUES = {"Red Sauce", "Green Sauce"}

_DESSERT_TONGS_KEYS = {"churros", "sopapillas"}

def _utensil_summary(sub: dict) -> str:
    return (
        f'{sub.get("plates_and_bowls", 0)}x Plates | {sub.get("silverware", 0)}x Silverware | '
        f'{sub.get("catering_large_spoons", 0)}x Lg Spoons | {sub.get("catering_small_spoons", 0)}x Sm Spoons | '
        f'{sub.get("black_tongs", 0)}x Tongs'
    )

def _is_individual_packaging(item: dict) -> bool:
    choices = item.get("choices") or {}
    return choices.get("packaging") == "individual" or str(item.get("item_key") or "").endswith("_individual")

def _is_individual_meal(item: dict) -> bool:
    return item.get("package_type") in _TRAY_TYPES and _is_individual_packaging(item)

def _individual_meal_qty(order: NormalizedOrder) -> int:
    return sum(
        int(i.get("qty") or 0)
        for i in order["normalized_items"]
        if _is_individual_meal(i)
    )

def _has_bulk_meals(order: NormalizedOrder) -> bool:
    return any(
        i.get("package_type") in _TRAY_TYPES and not _is_individual_meal(i)
        for i in order["normalized_items"]
    )

def _default_tableware_counts(order: NormalizedOrder) -> tuple[int, int]:
    headcount = int(order.get("headcount") or 0)
    individual_qty = _individual_meal_qty(order)
    has_bulk = _has_bulk_meals(order)

    if individual_qty and not has_bulk:
        return individual_qty + _PLATES_BUFFER, 0

    if individual_qty:
        silverware = max(headcount, individual_qty) + _PLATES_BUFFER
        bulk_guests = max(headcount - individual_qty, 0)
        plates = bulk_guests + _PLATES_BUFFER if bulk_guests else 0
        return silverware, plates

    return headcount + _PLATES_BUFFER, headcount + _PLATES_BUFFER

def _tray_items(order: NormalizedOrder) -> list:
    return [
        i for i in order["normalized_items"]
        if i["package_type"] in _TRAY_TYPES and not _is_individual_meal(i)
    ]


def rule_tableware(item: NormalizedItem, order: NormalizedOrder) -> PrepBreakdown:
    b = _empty_breakdown(item)

    if item["item_key"] == "tableware":
        # Pull PDF-parsed counts from extras (set by _parse_tableware_extras in normalize.py)
        pdf: dict[str, int] = {}
        for e in item["extras"]:
            try:
                pdf[e["name"]] = int(e["raw_text"])
            except (ValueError, TypeError):
                pass

        sides_spoon_keys = {
            i["item_key"] for i in order["normalized_items"]
            if i["package_type"] == "sides" and i["item_key"] in (_SIDES_SMALL_SPOON_KEYS | _SAUCE_KEYS)
        }
        sides_tong_keys = {
            i["item_key"] for i in order["normalized_items"]
            if i["package_type"] in {"sides", "a_la_carte"} and i["item_key"] in _SIDES_TONGS_KEYS
        }
        for i in order["normalized_items"]:
            if i["package_type"] == "sides" and i["item_key"] in _SAUCE_KEYS:
                sides_spoon_keys.add(i["item_key"])
        default_silverware, default_plates = _default_tableware_counts(order)
        individual_qty = _individual_meal_qty(order)
        if individual_qty:
            # Individually packaged meals need one silverware pack per meal plus buffer,
            # and no loose plates/bowls unless there is a bulk tray portion too.
            silverware = max(pdf.get("silverware") or 0, default_silverware)
            plates = default_plates
        else:
            # Bulk catering: 1 set per guest plus buffer, unless PDF supplied a count.
            silverware = pdf.get("silverware") or default_silverware
            plates = pdf.get("plates_and_bowls") or default_plates

        # Catering large spoons are added from cooked tray components after all breakdowns are built.
        catering_large_spoons = pdf.get("catering_large_spoons") or 0

        # Catering small spoons: one per different side type, not per container count.
        catering_small_spoons = pdf.get("catering_small_spoons") or len(sides_spoon_keys)
  
        # Black tongs: one per different tong side/a-la-carte type.
        black_tongs = pdf.get("black_tongs") or len(sides_tong_keys)


        sub_counts = {
            "silverware": silverware,
            "catering_large_spoons": catering_large_spoons,
            "catering_small_spoons": catering_small_spoons,
            "black_tongs": black_tongs,
            "plates_and_bowls": plates,
        }
        b["utensil_sub_counts"] = sub_counts
        b["summary_line"] = _utensil_summary(sub_counts)

    elif item["item_key"] == "plates_and_bowls":
        _silverware, plates = _default_tableware_counts(order)
        b["utensil_sub_counts"] = {"plates_and_bowls": plates}
        if plates:
            b["summary_line"] = f'{plates}x Plates/Bowls ({order["headcount"]} guests + {_PLATES_BUFFER} buffer)'
        else:
            b["summary_line"] = "0x Plates/Bowls (individual packaging)"

    else:
        b["summary_line"] = f'{item["qty"]}x {item["source"]["raw_alias"]}'

    return b

def _apply_prep_utensils(breakdowns: List[PrepBreakdown]) -> None:
    """
    Counts tong- and large-spoon-requiring prep components across all tray breakdowns
    and adds them into tableware breakdowns untensil counts.
    Must be called after all breakdowns are computed
    """
    prep_tongs = 0
    prep_large_spoons = 0
    prep_small_spoons = 0

    seen_tongs: set[str] = set()  # to avoid double-counting shared components across trays
    seen_large: set[str] = set()
    seen_small: set[str] = set()

    for b in breakdowns:
        if b["package_type"] == "desserts" and b["item_key"] in _DESSERT_TONGS_KEYS:
            prep_tongs += 1
            seen_tongs.add(b["item_key"])
        if b["package_type"] == "enchiladas" and b["choices"].get("packaging") != "individual":
            prep_large_spoons += int(b.get("qty") or 0)
        if _is_individual_meal(b):
            continue
        if b["package_type"] not in _TRAY_TYPES:
            continue
        for li in b.get("proteins", []) + b.get("sides", []) + b.get("sauces", []):
            name = li["name"]
            if name in _PREP_TONG_COMPONENT_VALUES and name not in seen_tongs:
                prep_tongs += 1
                seen_tongs.add(name)
            if name in _PREP_LARGE_SPOON_COMPONENT_VALUES and name not in seen_large:
                prep_large_spoons += 1
                seen_large.add(name)
            if name in _PREP_SMALL_SPOON_COMPONENT_VALUES and name not in seen_small:
                prep_small_spoons += 1
                seen_small.add(name)
            if li["name"] == "Pico De Gallo" and "Pico De Gallo" not in seen_small:
                prep_small_spoons += 1
                seen_small.add("Pico De Gallo")
            if li["name"] == "Tomatoes Diced" and "Tomatoes Diced" not in seen_small:
                prep_small_spoons += 1
                seen_small.add("Tomatoes Diced")
            if li["name"] == "Cucumber Diced" and "Cucumber Diced" not in seen_small:
                prep_small_spoons += 1
                seen_small.add("Cucumber Diced")
            if li["name"] == "Black Olives" and "Black Olives" not in seen_small:
                prep_small_spoons += 1
                seen_small.add("Black Olives")
            if li["name"].startswith("Dressing") and "Dressing" not in seen_small:
                prep_small_spoons += 1
                seen_small.add("Dressing")

    for b in breakdowns:
        sub = b.get("utensil_sub_counts")
        if not sub:
            continue
        if "black_tongs" in sub:
            sub["black_tongs"] += prep_tongs
        if "catering_large_spoons" in sub:
            sub["catering_large_spoons"] += prep_large_spoons
        if "catering_small_spoons" in sub:
            sub["catering_small_spoons"] += prep_small_spoons
        b["summary_line"] = _utensil_summary(sub)

RULES: Dict[str, Callable[[NormalizedItem, NormalizedOrder], PrepBreakdown]] = {
    "fajitas": rule_fajitas,
    "veggie_fajitas": rule_veggie,         # you can split later if needed
    "brochette_shrimp": rule_brochette,
    "premium": rule_premium,
    "enchiladas": rule_other,
    "beverages": rule_beverages,
    "a_la_carte": rule_other,
    "salads": rule_salads,
    "sides": rule_sides,
    "desserts": rule_other,
    "non_food_items": rule_tableware,
}

def build_kitchen_result(order: NormalizedOrder) -> KitchenOrderResult:
    breakdowns: List[PrepBreakdown] = []
    flags: List[str] = order["flags"][:]

    for item in order["normalized_items"]:
        handler = RULES.get(item["package_type"], rule_other)
        breakdowns.append(handler(item, order))
    
    _apply_prep_utensils(breakdowns)

    result: KitchenOrderResult = {
        "kitchen_ready_time": None,
        "order_id": order["order_id"],
        "date": order["date"],
        "store": order["origin_store_id"],
        "breakdowns": breakdowns,
        "flags": flags,
        "kitchen_ticket_text": "",   # keep, but set correctly
    }

    # If you want a simple built-in fallback text:
    lines = [f'ORDER {order["order_id"]} — {order["date"]} {order["deliver_at"]} — {order["reported_store"]}']
    for b in breakdowns:
        lines.append(b["summary_line"] or f'{b["qty"]}x {b["item_key"]}')
    result["kitchen_ticket_text"] = "\n".join(lines)

    return result
