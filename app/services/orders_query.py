"""Reconstruct the 4-view grids (Master / Kitchen / Driver / Prep Expo)
from persisted Order + OrderItem + PrepBreakdownRecord rows. Used by the
listing pages so a saved order can be re-rendered without re-uploading
its PDF or re-calling Claude / Google Maps."""
from __future__ import annotations

from typing import Any
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Order, OrderItem, PrepBreakdownRecord
from app.domain.menu_catalog import MenuCatalog, MENU_CATALOG
from app.domain.ticket_context import build_ticket_context
from app.domain.master_sheet_map import build_all_outputs
from app.domain.grid_builder import build_all_view_grids


_catalog = MenuCatalog(MENU_CATALOG)


def _normalized_item_from_row(item: OrderItem) -> dict[str, Any]:
    """Reverse the OrderItem -> NormalizedItem persistence mapping."""
    return {
        "item_key": item.item_key,
        "package_type": item.package_type,
        "qty": item.qty or 0,
        "choices": item.choices or {
            "packaging": item.packaging or "none",
            "beans": "none",
            "tortillas": "none",
            "with_ice": None,
        },
        "extras": item.extras or [],
        "container": (item.choices or {}).get("container") if item.choices else None,
        "source": item.source or {"raw_alias": item.raw_alias, "raw_qty": item.qty or 0, "raw_line_items": []},
        "flags": item.flags or [],
    }


def _normalized_order_from_row(order: Order, items: list[OrderItem]) -> dict[str, Any]:
    return {
        "order_id": order.external_order_id,
        "client": order.client or "",
        "upon_delivery_ask_for": order.upon_delivery_ask_for or "",
        "reported_store": order.reported_store or "",
        "reported_store_id": order.reported_store_id or "",
        "origin_store_id": order.origin_store_id or "",
        "headcount": order.headcount or 0,
        "customer_phone": order.customer_phone or "",
        "date": order.delivery_date or "",
        "deliver_at": order.deliver_at or "",
        "delivery_window": order.delivery_window or {"start": "", "end": ""},
        "delivery_address": order.delivery_address or "",
        "delivery_instructions": order.delivery_instructions,
        "setup_required": order.setup_required,
        "notes": None,
        "normalized_items": [_normalized_item_from_row(it) for it in items],
        "route_group_id": order.route_group_id,
        "route_stop_index": order.route_stop_index,
        "assigned_driver": order.assigned_driver,
        "flags": order.flags or [],
    }


def _kitchen_result_from_rows(order: Order, items: list[OrderItem],
                              breakdowns_by_item_id: dict[int, list[PrepBreakdownRecord]]) -> dict[str, Any]:
    breakdowns: list[dict[str, Any]] = []
    for item in items:
        recs = breakdowns_by_item_id.get(item.id, [])
        if recs:
            breakdowns.append(recs[0].breakdown)
    return {
        "kitchen_ready_time": order.kitchen_ready_time,
        "order_id": order.external_order_id,
        "date": order.delivery_date or "",
        "store": order.origin_store_id or "",
        "breakdowns": breakdowns,
        "flags": order.flags or [],
        "kitchen_ticket_text": "",
    }


def _dispatch_from_order(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.external_order_id,
        "origin_store_id": order.origin_store_id,
        "route_group_id": order.route_group_id,
        "route_stop_index": order.route_stop_index,
        "assigned_driver": order.assigned_driver,
        "driver_departure_time": order.driver_departure_time,
        "kitchen_ready_time": order.kitchen_ready_time,
        "pickup_at": None,
        "travel_minutes": None,
        "total_drive_minutes": None,
        "flags": [],
    }


def reconstruct_bundle(order: Order, items: list[OrderItem],
                       breakdowns_by_item_id: dict[int, list[PrepBreakdownRecord]],
                       dispatch_override: dict | None = None) -> dict[str, Any]:
    """Rebuild the per-PDF "bundle" shape that build_view_grid expects.

    If `dispatch_override` is provided, use it instead of reading the stale
    per-order dispatch from the DB. The combined per-date view passes a fresh
    plan computed across all orders for that date so Driver A/B/C/D/E rotate
    correctly across the day instead of every order saying "Driver A."
    """
    normalized = _normalized_order_from_row(order, items)
    kitchen_result = _kitchen_result_from_rows(order, items, breakdowns_by_item_id)
    dispatch = dispatch_override if dispatch_override is not None else _dispatch_from_order(order)
    ctx = build_ticket_context(normalized, kitchen_result, dispatch)
    views = build_all_outputs(normalized, kitchen_result, ctx, _catalog)
    return {
        "order_id": normalized["order_id"],
        "normalized_order": normalized,
        "kitchen_result": kitchen_result,
        "ticket_context": ctx,
        "views": views,
        "dispatch": dispatch,
    }


def load_order_by_id(db: Session, external_order_id: str) -> tuple[Order, list[OrderItem],
                                                                   dict[int, list[PrepBreakdownRecord]]] | None:
    order = db.query(Order).filter_by(external_order_id=external_order_id).first()
    if not order:
        return None
    items = db.query(OrderItem).filter_by(order_id=order.id).order_by(OrderItem.id).all()
    item_ids = [it.id for it in items]
    breakdowns: dict[int, list[PrepBreakdownRecord]] = defaultdict(list)
    if item_ids:
        for b in db.query(PrepBreakdownRecord).filter(PrepBreakdownRecord.order_item_id.in_(item_ids)).all():
            breakdowns[b.order_item_id].append(b)
    return order, items, breakdowns


def build_grids_for_single_order(db: Session, external_order_id: str) -> dict[str, Any] | None:
    loaded = load_order_by_id(db, external_order_id)
    if not loaded:
        return None
    order, items, breakdowns = loaded
    bundle = reconstruct_bundle(order, items, breakdowns)
    grids = build_all_view_grids([bundle], collapse_empty_rows=False)
    return {"order": order, "bundle": bundle, "grids": grids}


def build_grids_for_orders(db: Session, orders: list[Order],
                           collapse_empty_rows: bool = False) -> dict[str, Any]:
    """Combined-day view: rebuild all listed orders into a single grid set.

    Re-runs `build_dispatch_plans` across the WHOLE day's orders so Driver
    A/B/C/D/E rotate correctly. (Per-order dispatch in the DB was assigned
    when each order was ingested in isolation, so they all read "Driver A".)
    """
    # Pass 1: load orders + items + breakdowns + build minimal normalized for
    # dispatch planning.
    loaded: list[tuple[Order, list[OrderItem], dict[int, list[PrepBreakdownRecord]], dict]] = []
    normalized_list: list[dict] = []
    for order in orders:
        items = db.query(OrderItem).filter_by(order_id=order.id).order_by(OrderItem.id).all()
        item_ids = [it.id for it in items]
        breakdowns: dict[int, list[PrepBreakdownRecord]] = defaultdict(list)
        if item_ids:
            for b in db.query(PrepBreakdownRecord).filter(PrepBreakdownRecord.order_item_id.in_(item_ids)).all():
                breakdowns[b.order_item_id].append(b)
        normalized = _normalized_order_from_row(order, items)
        loaded.append((order, items, breakdowns, normalized))
        normalized_list.append(normalized)

    # Pass 2: run the dispatch planner across all orders in the day so it can
    # actually pair routes + rotate driver letters across the batch.
    try:
        from app.services.dispatch_planner import build_dispatch_plans
        fresh_dispatch_by_id = build_dispatch_plans(normalized_list)
    except Exception:
        fresh_dispatch_by_id = {}

    # Pass 3: reconstruct bundles using the fresh dispatch override.
    bundles: list[dict[str, Any]] = []
    for order, items, breakdowns, normalized in loaded:
        oid = normalized.get("order_id")
        fresh = fresh_dispatch_by_id.get(oid)
        bundles.append(reconstruct_bundle(order, items, breakdowns, dispatch_override=fresh))

    # Sort left-to-right by earliest kitchen-ready time (prefer dispatch
    # planner's computed value; fall back to customer-requested delivery time
    # then driver letter for stable ordering when times tie).
    def _sort_key(b: dict) -> tuple:
        d = b.get("dispatch") or {}
        n = b.get("normalized_order") or {}
        return (
            d.get("kitchen_ready_time") or n.get("deliver_at") or "~",
            d.get("assigned_driver") or "",
            d.get("route_stop_index") or 0,
        )
    bundles.sort(key=_sort_key)
    grids = build_all_view_grids(bundles, collapse_empty_rows=collapse_empty_rows)
    return {"bundles": bundles, "grids": grids}


# Mapping shown to the user. Stores 1 + 3 collapse to "copperfield" (Houston FM
# 529 + Westheimer); stores 2 + 4 collapse to "tomball" (Tomball + Spring) —
# matches resolve_origin_store_id() in normalize.py.
LOCATION_TO_ORIGIN = {
    "copperfield": "store_1",
    "tomball": "store_2",
}
LOCATION_LABELS = {
    "copperfield": "Copperfield",
    "tomball": "Tomball",
}


def list_orders_for_location(db: Session, location: str) -> list[Order]:
    origin = LOCATION_TO_ORIGIN.get(location)
    if not origin:
        return []
    # Rolling cutoff: hide orders whose delivery date is in the past so the
    # listing only shows today + upcoming. Past orders stay in the DB and are
    # still reachable by direct URL (/orders/view/<id>); they just don't
    # appear in /orders/<location>. Reset each day automatically since the
    # cutoff is computed at query time.
    today_iso = datetime.now().strftime("%Y-%m-%d")
    return (
        db.query(Order)
        .filter(Order.origin_store_id == origin)
        .filter(Order.delivery_date >= today_iso)
        .filter(Order.status != "cancelled")
        .order_by(Order.delivery_date.asc(), Order.deliver_at)
        .all()
    )


def group_orders_by_date(orders: list[Order]) -> list[dict[str, Any]]:
    """Returns [{date: 'YYYY-MM-DD', display: 'Wed, May 6, 2026', orders: [Order, ...]}, ...]
    sorted by date ascending — earliest (today) at top, scroll down for later dates."""
    by_date: dict[str, list[Order]] = defaultdict(list)
    for o in orders:
        by_date[o.delivery_date or "(no date)"].append(o)
    out = []
    for date_str, rows in sorted(by_date.items()):
        rows.sort(key=lambda r: r.deliver_at or "")
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            display = f"{d.strftime('%a, %b')} {d.day}, {d.year}"
        except (ValueError, TypeError):
            display = date_str
        out.append({"date": date_str, "display": display, "orders": rows})
    return out
