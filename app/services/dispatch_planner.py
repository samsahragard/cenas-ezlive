from __future__ import annotations

import logging
from typing import Dict, List, Any
from itertools import combinations

from app.config import STORE_ADDRESSES
from app.domain.delivery_timing import compute_solo_timing, next_driver_name
from app.infra.geo import GoogleMapsClient
from app.services.routing_service import compute_pair_route_plan

logger = logging.getLogger(__name__)


def _validate_dispatch_order(order: Dict[str, Any]) -> str | None:
    required_keys = [
        "order_id",
        "origin_store_id",
        "delivery_address",
        "date",
        "deliver_at",
    ]

    missing = [key for key in required_keys if not order.get(key)]
    if missing:
        return f"missing required fields: {', '.join(missing)}"

    if order.get("origin_store_id") == "unknown":
        return f"unrecognized store: {order.get('reported_store', '(no store text)')!r} — could not map to a known store"

    return None


def _failed_entry(order_id: str, origin_store_id: Any, flag: str, travel_minutes: Any = None) -> dict:
    return {
        "order_id": order_id,
        "origin_store_id": origin_store_id,
        "route_group_id": None,
        "route_stop_index": None,
        "assigned_driver": None,
        "driver_departure_time": None,
        "pickup_at": None,
        "kitchen_ready_time": None,
        "travel_minutes": travel_minutes,
        "total_drive_minutes": None,
        "flags": [flag],
    }


def build_dispatch_plans(orders: List[dict]) -> Dict[str, dict]:
    dispatch_by_order_id: Dict[str, dict] = {}

    try:
        maps_client = GoogleMapsClient()
    except Exception as e:
        logger.error("dispatch: maps client failed to initialize — all orders will have no dispatch data: %s", e)
        for fallback_index, order in enumerate(orders):
            order_id = order.get("order_id") or f"unknown_{fallback_index}"
            dispatch_by_order_id[order_id] = _failed_entry(
                order_id,
                order.get("origin_store_id"),
                f"dispatch_failed: maps client init error: {str(e)}",
            )
        return dispatch_by_order_id

    # ---------------------------------------------------------------
    # Phase 1: validate and compute solo timing for every valid order.
    # Orders that fail here land in dispatch_by_order_id immediately.
    # Orders that pass are held in solo_data for phase 2 pairing.
    # ---------------------------------------------------------------
    solo_data: Dict[str, dict] = {}

    for fallback_index, order in enumerate(orders):
        order_id = order.get("order_id") or f"unknown_{fallback_index}"

        validation_error = _validate_dispatch_order(order)
        if validation_error:
            logger.warning("dispatch: order %s failed validation — %s", order_id, validation_error)
            dispatch_by_order_id[order_id] = _failed_entry(
                order_id, order.get("origin_store_id"), f"dispatch_failed: {validation_error}"
            )
            continue

        try:
            store_address = STORE_ADDRESSES[order["origin_store_id"]]
        except Exception as e:
            logger.warning("dispatch: order %s store lookup failed — %s", order_id, e)
            dispatch_by_order_id[order_id] = _failed_entry(
                order_id, order.get("origin_store_id"),
                f"dispatch_failed: store lookup error: {str(e)}",
            )
            continue

        try:
            travel_minutes = maps_client.get_drive_minutes(
                origin=store_address,
                destination=order["delivery_address"],
            )
        except Exception as e:
            logger.warning("dispatch: order %s maps lookup failed (%s → %s) — %s",
                           order_id, store_address, order["delivery_address"], e)
            dispatch_by_order_id[order_id] = _failed_entry(
                order_id, order.get("origin_store_id"),
                f"dispatch_failed: maps lookup error: {str(e)}",
            )
            continue

        try:
            window_start = (order.get("delivery_window") or {}).get("start") or order["deliver_at"]
            solo = compute_solo_timing(
                date_str=order["date"],
                window_start=window_start,
                travel_minutes=travel_minutes,
            )
        except Exception as e:
            logger.warning("dispatch: order %s solo timing failed (date=%s deliver_by=%s window_start=%s) — %s",
                           order_id, order.get("date"), order.get("deliver_at"), window_start, e)
            dispatch_by_order_id[order_id] = _failed_entry(
                order_id, order.get("origin_store_id"),
                f"dispatch_failed: solo timing error: {str(e)}",
                travel_minutes=travel_minutes,
            )
            continue

        solo_data[order_id] = {
            "order": order,
            "store_address": store_address,
            "travel_minutes": travel_minutes,
            "solo": solo,
        }

# ---------------------------------------------------------------
# Phase 2: score all feasible pairs, assign best ones first.
# 
# Step 1 - try every combination of two valid orders and collect all feasible pairs with their route result
# Step 2 - sort pairs by score: less drive time is better
# Step 3 - walk sorted pairs; skip if either order already claimed by a better pair
# Step 4 - solo fallback for anything still unmatched.
# ---------------------------------------------------------------


    feasible_pairs = []
    valid_order_ids = list(solo_data.keys()) 

    for order_id_a, order_id_b in combinations(valid_order_ids, 2):
        order_a = solo_data[order_id_a]["order"]
        order_b = solo_data[order_id_b]["order"]

        if order_a["origin_store_id"] != order_b["origin_store_id"]:
            continue
        if order_a["date"] != order_b["date"]:
            continue

        try:
            pair_result = compute_pair_route_plan(order_a, order_b)
        except Exception:
            continue
        if not pair_result.get("feasible"):
            continue

        route_result = pair_result["route_result"]
        total_drive = route_result.get("total_drive_minutes", 9999)
        total_late = sum(s["minutes_late"] for s in route_result["stops"])

        feasible_pairs.append({
            "order_id_a": order_id_a,
            "order_id_b": order_id_b,
            "pair_result": pair_result,
            "score": (total_late, total_drive),
        })

    feasible_pairs.sort(key=lambda p: p["score"])

    paired_ids: set = set()
    driver_index = 0

    for pair in feasible_pairs:
        order_id_a = pair["order_id_a"]
        order_id_b = pair["order_id_b"]

        if order_id_a in paired_ids or order_id_b in paired_ids:
            continue

        route_result = pair["pair_result"]["route_result"]
        driver_name = next_driver_name(driver_index)
        driver_index += 1
        route_group_id = f"route_{order_id_a}_{order_id_b}"
        origin_store_id = solo_data[order_id_a]["order"]["origin_store_id"]

        for stop in route_result["stops"]:
            stop_order_id = stop["order_id"]
            dispatch_by_order_id[stop_order_id] = {
                "order_id": stop_order_id,
                "origin_store_id": origin_store_id,
                "route_group_id": route_group_id,
                "route_stop_index": stop["stop_index"],
                "assigned_driver": driver_name,
                "driver_departure_time": route_result.get("depart_store_at"),
                "pickup_at": route_result.get("pickup_at"),
                "kitchen_ready_time": route_result.get("kitchen_ready_at"),
                "travel_minutes": solo_data[stop_order_id]["travel_minutes"],
                "total_drive_minutes": route_result.get("total_drive_minutes"),
                "flags": route_result.get("flags", []),
            }

        paired_ids.add(order_id_a)
        paired_ids.add(order_id_b)
    
    # Solo fallback for any order not claimed by a pair
    for order_id in valid_order_ids:
        if order_id in paired_ids:
            continue

        order = solo_data[order_id]["order"]
        solo = solo_data[order_id]["solo"]

        try:
            driver_name = next_driver_name(driver_index)
        except Exception as e:
            driver_name = None
            flags = [f"dispatch_warning: driver assignment error: {str(e)}"]
        else:
            flags = []

        driver_index += 1

        dispatch_by_order_id[order_id] = {
            "order_id": order_id,
            "origin_store_id": order["origin_store_id"],
            "route_group_id": f"route_{order_id}",
            "route_stop_index": 1,
            "assigned_driver": driver_name,
            "driver_departure_time": solo.get("depart_store_at"),
            "pickup_at": solo.get("pickup_at"),
            "kitchen_ready_time": solo.get("kitchen_ready_at"),
            "travel_minutes": solo.get("travel_minutes"),
            "total_drive_minutes": solo.get("travel_minutes"),
            "flags": flags,
        }
    
    return dispatch_by_order_id
