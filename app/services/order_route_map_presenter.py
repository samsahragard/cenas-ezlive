from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config import STORE_ADDRESSES
from app.models import Order


KITCHEN_BY_SLUG: dict[str, dict[str, object]] = {
    "copperfield": {
        "slug": "copperfield",
        "label": "Copperfield",
        "address": STORE_ADDRESSES.get("store_1", "15650 FM 529 Rd, Houston, TX 77095"),
        "lat": 29.8730,
        "lng": -95.6428,
    },
    "tomball": {
        "slug": "tomball",
        "label": "Tomball",
        "address": STORE_ADDRESSES.get("store_2", "27727 Tomball Pkwy, Tomball, TX 77375"),
        "lat": 30.1118,
        "lng": -95.6230,
    },
}

_ORIGIN_TO_KITCHEN = {
    "store_1": "copperfield",
    "store_3": "copperfield",
    "copperfield": "copperfield",
    "store_2": "tomball",
    "store_4": "tomball",
    "tomball": "tomball",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def kitchen_slug_for_order(order: Order) -> str:
    pickup = _clean(getattr(order, "pickup_kitchen", "")).lower()
    if pickup in KITCHEN_BY_SLUG:
        return pickup

    origin = _clean(getattr(order, "origin_store_id", "")).lower()
    return _ORIGIN_TO_KITCHEN.get(origin, "")


def _iso(value: object) -> str:
    if not value:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def route_map_order_payload(order: Order) -> dict[str, object]:
    kitchen_slug = kitchen_slug_for_order(order)
    kitchen = KITCHEN_BY_SLUG.get(kitchen_slug)
    address = _clean(getattr(order, "delivery_address", ""))
    issues: list[str] = []

    if not kitchen:
        issues.append("Pickup kitchen is unknown.")
    if not address:
        issues.append("Delivery address is missing.")

    return {
        "order_id": _clean(getattr(order, "external_order_id", "")) or str(getattr(order, "id", "")),
        "delivery_date": _clean(getattr(order, "delivery_date", "")),
        "deliver_at": _clean(getattr(order, "deliver_at", "")),
        "client": _clean(getattr(order, "client", "")),
        "ask_for": _clean(getattr(order, "upon_delivery_ask_for", "")),
        "address": address,
        "kitchen_slug": kitchen_slug,
        "kitchen": kitchen,
        "pickup_miles": getattr(order, "pickup_miles", None),
        "updated_at": _iso(getattr(order, "updated_at", None)),
        "is_routable": bool(kitchen and address),
        "issues": issues,
    }


def build_route_map_payload(orders: list[Order]) -> dict[str, object]:
    order_payloads = [route_map_order_payload(order) for order in orders]
    used_kitchens = {
        str(order["kitchen_slug"]): order["kitchen"]
        for order in order_payloads
        if order.get("kitchen_slug") and order.get("kitchen")
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kitchens": used_kitchens,
        "orders": order_payloads,
    }
