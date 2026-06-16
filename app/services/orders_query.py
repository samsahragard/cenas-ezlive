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
from app.domain.kitchen_engine import build_kitchen_result


_catalog = MenuCatalog(MENU_CATALOG)
_SIDE_CONTAINER_KEYWORDS = ["half gallon", "quart", "half pint", "pint"]


def _clock_minutes(raw: object) -> tuple[int, int, str]:
    """Return a sortable clock-time key, with unparseable values last."""
    value = str(raw or "").strip()
    if not value:
        return (1, 0, "")

    for fmt in ("%I:%M %p", "%I %p", "%H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(value.upper(), fmt)
            return (0, parsed.hour * 60 + parsed.minute, value)
        except ValueError:
            pass

    try:
        parsed_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (0, parsed_dt.hour * 60 + parsed_dt.minute, value)
    except ValueError:
        return (1, 0, value)


def _side_container_from_source(item: OrderItem) -> str | None:
    choices = item.choices or {}
    if choices.get("container"):
        return choices.get("container")
    source = item.source or {}
    text = " ".join(source.get("raw_line_items") or []).lower()
    for keyword in _SIDE_CONTAINER_KEYWORDS:
        if keyword in text:
            return keyword
    return None


def _should_refresh_current_breakdowns(order: Order) -> bool:
    if getattr(order, "status", None) == "cancelled":
        return False
    try:
        today_iso = datetime.now().strftime("%Y-%m-%d")
        return bool(order.delivery_date and order.delivery_date >= today_iso)
    except Exception:
        return False


def _normalized_item_from_row(item: OrderItem) -> dict[str, Any]:
    """Reverse the OrderItem -> NormalizedItem persistence mapping."""
    choices = item.choices or {
        "packaging": item.packaging or "none",
        "beans": "none",
        "tortillas": "none",
        "with_ice": None,
    }
    return {
        "item_key": item.item_key,
        "package_type": item.package_type,
        "qty": item.qty or 0,
        "choices": choices,
        "extras": item.extras or [],
        "container": _side_container_from_source(item) if item.package_type == "sides" else choices.get("container"),
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


def _is_tableware_breakdown(breakdown: dict[str, Any] | None) -> bool:
    return bool(
        breakdown
        and breakdown.get("package_type") == "non_food_items"
        and breakdown.get("item_key") in {"tableware", "plates_and_bowls"}
    )


def _kitchen_result_from_rows(order: Order, items: list[OrderItem],
                              breakdowns_by_item_id: dict[int, list[PrepBreakdownRecord]],
                              normalized: dict[str, Any] | None = None) -> dict[str, Any]:
    breakdowns: list[dict[str, Any]] = []
    current_breakdowns: list[dict[str, Any]] = []
    if normalized is not None:
        try:
            current_breakdowns = build_kitchen_result(normalized).get("breakdowns", [])
        except Exception:
            current_breakdowns = []
    refresh_current = _should_refresh_current_breakdowns(order)

    for idx, item in enumerate(items):
        recs = breakdowns_by_item_id.get(item.id, [])
        stored = recs[0].breakdown if recs else None
        current = current_breakdowns[idx] if idx < len(current_breakdowns) else None

        # Active/upcoming catering views should reflect current portion rules
        # without forcing a PDF re-ingest. Historical rows keep their saved food
        # breakdowns, while tableware always refreshes as policy-like logic.
        if refresh_current and current:
            breakdowns.append(current)
        elif _is_tableware_breakdown(current):
            breakdowns.append(current)
        elif stored:
            breakdowns.append(stored)
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
    kitchen_result = _kitchen_result_from_rows(order, items, breakdowns_by_item_id, normalized)
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
    grids = build_all_view_grids([bundle], collapse_empty_rows=True)
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
        k = b.get("kitchen_result") or {}
        kitchen_time = (
            d.get("kitchen_ready_time")
            or k.get("kitchen_ready_time")
            or n.get("deliver_at")
        )
        return (
            _clock_minutes(kitchen_time),
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


def rotated_dispatch_letters(groups: list[dict[str, Any]]) -> dict[int, str]:
    """Per Sam #2870 follow-up: per-location dashboard was showing 'DRIVER A'
    for every row because each order's assigned_driver was set at ingest time
    in isolation (planner index always 0). Recompute a per-date rotation by
    deliver_at sort order so A/B/C/D/E cycle. Lightweight (no Maps API) —
    pairings shown on the per-location list are positional, not route-grouped.
    The combined-view route uses the full dispatch planner with route pairing.
    """
    from app.domain.delivery_timing import next_driver_name
    letters: dict[int, str] = {}
    for grp in groups:
        for idx, o in enumerate(grp.get("orders") or []):
            letters[o.id] = next_driver_name(idx)
    return letters
