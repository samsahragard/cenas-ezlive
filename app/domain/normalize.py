from __future__ import annotations
import logging
from typing import List, Optional

from app.domain.schemas import (
    RawOrder, NormalizedOrder, NormalizedItem,
    ItemChoices, ItemSource, PackageType, ExtraLineItem
)
from app.domain.menu_catalog import MenuCatalog
from app.services.date_normalizer import infer_order_date, normalize_pdf_time

logger = logging.getLogger(__name__)


def normalize_store_id(store: str) -> str:
    text = store.strip().lower()

    if "#1" in text or "15650" in text or "fm 529" in text:
        return "store_1"
    if "#2" in text or "27727" in text or "tomball pkwy" in text or "tomball" in text:
        return "store_2"
    if "#3" in text or "3733" in text or "westheimer" in text:
        return "store_3"
    if "#4" in text or "2162" in text or "spring stuebner" in text or "spring, tx" in text or "spring tx" in text:
        return "store_4"

    return "unknown"


def resolve_origin_store_id(store_id: str) -> str:
    if store_id in {"store_2", "store_4"}:
        return "store_2"
    if store_id in {"store_1", "store_3"}:
        return "store_1"
    return store_id


# Physical-kitchen display labels for collapsed origin_store_id values.
# Used by pickup_label() to render the kitchen-of-origin in driver-facing
# views (the ezCater storefront-of-record on the raw payload is the GHOST
# address for #3/#4 and would mislead drivers — see Sam #1486 / samai
# #1488 for the bug + policy lock).
#
# Em dash is U+2014, NOT a hyphen-minus. The visual difference matters
# and the tests byte-assert the exact glyph.
_KITCHEN_DISPLAY = {"store_1": "Copperfield", "store_2": "Tomball"}
_KITCHEN_ADDRESS = {
    "store_1": "15650 FM 529, Houston, TX 77095",
    "store_2": "27727 Tomball Pkwy, Tomball, TX 77375",
}
_LABEL_TEMPLATE = "{kitchen} Kitchen"


def pickup_label(order) -> str:
    """Display label for the pickup kitchen — "Copperfield Kitchen — <addr>"
    or "Tomball Kitchen — <addr>" based on the collapsed origin_store_id.

    Falls back to Order.reported_store for un-normalized legacy rows (e.g.
    origin_store_id is None or doesn't match a known kitchen). Driver-
    facing templates call this; the audit / review surfaces stay on the
    raw reported_store so the original ezCater storefront-of-record is
    preserved for forensic review.

    Direction: domain -> templates. Deliberately NOT an Order @property,
    to keep app.models free of presentation concerns.
    """
    osid = getattr(order, "origin_store_id", None)
    kitchen = _KITCHEN_DISPLAY.get(osid)
    address = _KITCHEN_ADDRESS.get(osid)
    if kitchen and address:
        return _LABEL_TEMPLATE.format(kitchen=kitchen, address=address)
    return getattr(order, "reported_store", None) or ""


def _parse_choices(raw_alias: str, line_items: List[str]) -> ItemChoices:
    text = " ".join([raw_alias, *line_items]).lower()

    if "individual" in text or "packaging @ $" in text:
        packaging = "individual"
    elif "tray" in text:
        packaging = "tray"
    else:
        packaging = "none"

    if "refried" in text:
        beans = "refried"
    elif "charro" in text:       
        beans = "charro"
    elif "black" in text:
        beans = "black"
    elif "most popular" in text:
        beans = "most_popular"
    else:
        beans = "none"

    if "half flour" in text and "half corn" in text:
        tortillas = "half_flour_half_corn"
    elif "flour" in text:
        tortillas = "flour"
    elif "corn" in text:
        tortillas = "corn"
    else:
        tortillas = "none"

    with_ice = True if "ice" in text else None

    return {
        "packaging": packaging,
        "beans": beans,
        "tortillas": tortillas,
        "with_ice": with_ice
    }


def _parse_tableware_extras(line_items: List[str]) -> List[ExtraLineItem]:
    """
    Parses tableware sub-component line items in "ComponentName: QTY" format.
    Maps component names to canonical keys used by rule_tableware and master sheet.
    """
    import re

    # Maps (regex pattern) -> canonical name
    COMPONENT_PATTERNS = [
        (r"silverware|fork|knife|spoon\s*set", "silverware"),
        (r"catering\s+large\s+spoon|large\s+catering\s+spoon|large\s+spoon", "catering_large_spoons"),
        (r"catering\s+small\s+spoon|small\s+catering\s+spoon|small\s+spoon", "catering_small_spoons"),
        (r"catering\s+spoon(?!\s*(large|small))|serving\s+spoon", "catering_large_spoons"),  # unqualified → large
        (r"black\s+tong|tong", "black_tongs"),
        (r"plate|bowl", "plates_and_bowls"),
        (r"napkin", "napkins"),
    ]

    extras = []
    for li in line_items:
        # Expect "ComponentName: QTY" — extract the number after the colon
        match = re.match(r"^(.+?):\s*(\d+)\s*$", li.strip())
        if not match:
            continue
        component_text = match.group(1).strip().lower()
        qty_str = match.group(2)

        canonical = None
        for pattern, name in COMPONENT_PATTERNS:
            if re.search(pattern, component_text):
                canonical = name
                break

        if canonical:
            extras.append({"name": canonical, "raw_text": qty_str})

    return extras

def _parse_side_container(line_items: List[str]) -> Optional[str]:
    """
    Scans PDF line items for a container size specification.
    Returns the first matching container keyword, or None if not found.
    """
    CONTAINER_KEYWORDS = ["half gallon", "quart", "half pint", "pint"]
    text = " ".join(line_items).lower()
    for kw in CONTAINER_KEYWORDS:
        if kw in text:
            return kw
    return None


def _parse_beverage_extras(line_items: List[str]) -> List[ExtraLineItem]:
    """
    Extracts flavor/soda type info from beverage line items.
    Tries to parse "Flavor: X" or "Soda Types: X" prefixes first.
    Falls back to using the raw line item text as-is so nothing is lost.
    """
    extras = []
    for li in line_items:
        lower = li.lower().strip()
        if lower.startswith("flavor:"):
            extras.append({"name": "flavor", "raw_text": li.split(":", 1)[-1].strip()})
        elif lower.startswith("soda types:") or lower.startswith("soda type:"):
            extras.append({"name": "soda_types", "raw_text": li.split(":", 1)[-1].strip()})
        else:
            # fallback: Gemini returned the value without a label prefix
            extras.append({"name": "flavor", "raw_text": li.strip()})
    return extras

def _parse_salad_extras(line_items: List[str]) -> List[ExtraLineItem]:
    """
    Extracts dressing and protein choices from salad line items.
    Expects Gemini to emit "Dressing: <choice>" for each dressing.
    Expects Gemini to emit "Protein: <choice>" for each protein.
    Falls back to treating any unrecognized line item as a dressing note.
    """
    extras = []
    for li in line_items:
        lower = li.lower().strip()
        if lower.startswith("dressing"):
            value = li.split(":", 1)[-1].strip() if ":" in li else li.strip()
            extras.append({"name": "dressing", "raw_text": value})
        elif lower.startswith("protein"):
            value = li.split(":", 1)[-1].strip().lower() if ":" in li else lower
            if "chicken" in value:
                extras.append({"name": "protein", "raw_text": "chicken"})
            elif "beef" in value:
                extras.append({"name": "protein", "raw_text": "beef"})
            else:
                extras.append({"name": "protein", "raw_text": "mix"})
        else:
            extras.append({"name": "note", "raw_text": li.strip()})
    return extras

def _parse_enchilada_extras(line_items: List[str]) -> List[ExtraLineItem]:
    """
    Extracts sauce choices from enchilada line items.
    Expects Gemini to emit "Sauce": <choice>" for each sauce.
    Falls back to treating any unrecognized line item as a sauce note.
    """
    extras = []
    for li in line_items:
        lower = li.lower().strip()
        if lower.startswith("sauce"):
            value = li.split(":", 1)[-1].strip() if ":" in li else li.strip()
            extras.append({"name": "sauce", "raw_text": value})
        else:
            extras.append({"name": "note", "raw_text": li.strip()})
    return extras

def normalize_order(raw_order: RawOrder, catalog: MenuCatalog) -> NormalizedOrder:
    order_flags: List[str] = []
    normalized_items: List[NormalizedItem] = []

    reported_store = raw_order["store"]
    reported_store_id = normalize_store_id(reported_store)
    if reported_store_id == "unknown":
        logger.warning("Could not identify store from: %r", reported_store)
        order_flags.append(f"UNKNOWN_STORE:{reported_store}")
    origin_store_id = resolve_origin_store_id(reported_store_id)

    try:
        normalized_date = infer_order_date(raw_order["date"])
    except ValueError:
        normalized_date = raw_order["date"]
        order_flags.append(f"UNPARSED_DATE:{raw_order['date']}")

    try:
        normalized_deliver_at = normalize_pdf_time(raw_order["deliver_at"])
    except ValueError:
        normalized_deliver_at = raw_order["deliver_at"]
        order_flags.append(f"UNPARSED_TIME:{raw_order['deliver_at']}")

    for ri in raw_order["raw_items"]:
        raw_alias = ri["alias"]
        item_key = catalog.get_item_key(raw_alias)

        if not item_key:
            logger.warning("No menu match for alias: %r", raw_alias)
            item_key = "unknown"
            package_type: PackageType = "other"
            flags = [f"UNKNOWN_ITEM:{raw_alias}"]
        else:
            package_type = catalog.get_package_type(item_key) or "other"
            flags = []

        line_items = ri.get("line_items", [])
        choices = _parse_choices(raw_alias, line_items)
        container = _parse_side_container(line_items) if package_type == "sides" else None
        if package_type == "beverages":
            extras = _parse_beverage_extras(line_items)
        elif package_type == "non_food_items" and item_key == "tableware":
            extras = _parse_tableware_extras(line_items)
        elif package_type == "salads":
            extras = _parse_salad_extras(line_items)
        elif package_type == "enchiladas":
            extras = _parse_enchilada_extras(line_items)
        else:
            extras = []
            
        source: ItemSource = {
            "raw_alias": raw_alias,
            "raw_qty": ri["qty"],
            "raw_line_items": line_items
        }

        normalized_items.append({
            "item_key": item_key,
            "package_type": package_type,
            "qty": ri["qty"],
            "choices": choices,
            "extras": extras,
            "container": container,
            "source": source,
            "flags": flags,
        })

    if any("UNKNOWN_ITEM" in it["flags"] for it in normalized_items):
        order_flags.append("UNKNOWN_ITEM")
    
    return {
        "order_id": raw_order["order_id"],
        "client": raw_order.get("client") or "",
        "upon_delivery_ask_for": raw_order.get("upon_delivery_ask_for") or "" ,
        "reported_store": reported_store,
        "reported_store_id": reported_store_id,
        "origin_store_id": origin_store_id,
        "headcount": raw_order["headcount"],
        "customer_phone": raw_order.get("customer_phone") or "",
        "date": normalized_date,
        "deliver_at": normalized_deliver_at,
        "delivery_window": raw_order.get("delivery_window") or {"start": "", "end": ""},
        "delivery_address": raw_order["delivery_address"],
        "delivery_instructions": raw_order.get("delivery_instructions"),
        "setup_required": raw_order.get("setup_required"),
        "notes": raw_order.get("notes") or None,
        "normalized_items": normalized_items,
        "route_group_id": None,
        "route_stop_index": None,
        "assigned_driver": None,
        "flags": order_flags
    }