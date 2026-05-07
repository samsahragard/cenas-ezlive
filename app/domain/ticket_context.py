# shared ticket context builder (one place to compute common fields)
from __future__ import annotations
from typing import TypedDict, Optional, List, Dict, Any

from app.domain.schemas import NormalizedOrder, KitchenOrderResult

class TicketContext(TypedDict):
    order_id: str
    date: str
    deliver_at: str

    reported_store: str
    reported_store_id: str
    origin_store_id: str
    origin_store_label: str

    client: str
    upon_delivery_ask_for: str
    delivery_address: str
    delivery_instructions: Optional[str]
    customer_phone: str
    setup_required: Optional[bool]
    notes: Optional[str]
    headcount: int

    kitchen_ready_time: str
    driver_departure_time: str

    driver_name: Optional[str]

    utensils_summary: str

def origin_store_label(store_id: str) -> str:
    return {
        "store_1": "Cenas Kitchen #1",
        "store_2": "Cenas Kitchen #2",
        "unknown": "Unknown Store",
    }.get(store_id, "Unknown Store")
    
def build_ticket_context(order: NormalizedOrder, result: KitchenOrderResult, dispatch: dict,) -> TicketContext:

    return {
        "order_id": order["order_id"],
        "date": order["date"],
        "deliver_at": order["deliver_at"],

        "reported_store": order["reported_store"],
        "reported_store_id": order["reported_store_id"],
        "origin_store_id": order["origin_store_id"],
        "origin_store_label": origin_store_label(order["origin_store_id"]),

        "client": order["client"],
        "upon_delivery_ask_for": order["upon_delivery_ask_for"],
        "delivery_address": order["delivery_address"],
        "delivery_instructions": order.get("delivery_instructions"),
        "customer_phone": order["customer_phone"],
        "setup_required": order["setup_required"],
        "notes": order.get("notes"),
        "headcount": order["headcount"],

        "kitchen_ready_time": dispatch.get("kitchen_ready_time"),
        "driver_departure_time": dispatch.get("driver_departure_time"),
        "driver_name": dispatch.get("assigned_driver"),

        "utensils_summary": next(
            (b["summary_line"] for b in result["breakdowns"] if b.get("utensil_sub_counts") and b.get("summary_line")),
            "",
        ),
    }