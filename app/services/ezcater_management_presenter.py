from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import STORE_ADDRESSES


STORE_KEY_LABELS = {
    "store_1": "Copperfield",
    "store_2": "Tomball",
    "store_3": "Copperfield",
    "store_4": "Tomball",
}

STORE_SLUGS = {
    "store_1": "uno",
    "store_2": "dos",
    "store_3": "uno",
    "store_4": "dos",
}

STORE_ADDRESS_KEYS = {
    "store_1": "store_1",
    "store_2": "store_2",
    "store_3": "store_1",
    "store_4": "store_2",
}


def compact_order_card(
    order: Any,
    *,
    payout: float | int | None = None,
    competing_count: int | None = None,
    status_label: str | None = None,
    current_driver: str | None = None,
    display_driver: str | None = None,
) -> dict[str, Any]:
    """Presentation fields for manager-side Ez orders/market/manage cards."""
    raw_store_key = (getattr(order, "origin_store_id", None) or "").strip()
    store_key = raw_store_key if raw_store_key in STORE_KEY_LABELS else ""
    address_key = STORE_ADDRESS_KEYS.get(store_key, store_key)
    store_label = STORE_KEY_LABELS.get(store_key) or (
        getattr(order, "reported_store", None) or "Store"
    )

    date_raw = getattr(order, "delivery_date", None) or ""
    date_label = date_raw or "No date"
    weekday_label = ""
    try:
        parsed = datetime.strptime(date_raw, "%Y-%m-%d").date()
        date_label = f"{parsed.month}/{parsed.day}/{parsed.year}"
        weekday_label = parsed.strftime("%A")
    except (TypeError, ValueError):
        weekday_label = date_raw or ""

    payout_value = payout
    if payout_value is None:
        payout_value = getattr(order, "potential_payout", None)
    if payout_value is None:
        payout_value = 35
    try:
        payout_label = f"${float(payout_value):.0f}"
    except (TypeError, ValueError):
        payout_label = "$35"

    miles = getattr(order, "pickup_miles", None)
    try:
        miles_label = f"{float(miles):.1f} mi from pickup" if miles is not None else "-"
    except (TypeError, ValueError):
        miles_label = "-"

    heads = getattr(order, "headcount", None)
    heads_label = f"{heads} heads" if heads not in (None, "") else "- heads"

    selected_driver = current_driver or getattr(order, "ezcater_driver_name", None)
    selected_driver = selected_driver or getattr(order, "assigned_driver", None)
    current_driver_label = selected_driver or "no driver"

    if competing_count not in (None, ""):
        try:
            comp_n = int(competing_count)
            competing_label = f"{comp_n} driver{'s' if comp_n != 1 else ''} competing"
        except (TypeError, ValueError):
            competing_label = "0 drivers competing"
    else:
        competing_label = "0 drivers competing"

    return {
        "external_id": getattr(order, "external_order_id", None) or f"Order #{getattr(order, 'id', '')}",
        "store_key": store_key,
        "store_slug": STORE_SLUGS.get(store_key, ""),
        "store_label": store_label,
        "date_label": date_label,
        "weekday_label": weekday_label,
        "time_label": getattr(order, "deliver_at", None) or "-",
        "payout_label": payout_label,
        "status_label": status_label or getattr(order, "status", None) or "Order",
        "pickup_name": f"{store_label} Kitchen" if store_label != "Store" else "Kitchen",
        "pickup_address": STORE_ADDRESSES.get(address_key, "") or "-",
        "miles_label": miles_label,
        "heads_label": heads_label,
        "dropoff_address": getattr(order, "delivery_address", None) or "-",
        "client": getattr(order, "client", None) or "-",
        "current_driver_label": current_driver_label,
        "display_driver": display_driver or getattr(order, "assigned_driver", None) or "",
        "competing_label": competing_label,
    }
