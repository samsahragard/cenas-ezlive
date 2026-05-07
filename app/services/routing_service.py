# delivery math + build route plan (orchestrator)
from __future__ import annotations

from typing import Any, Dict

from app.config import STORE_ADDRESSES
from app.domain.delivery_timing import compute_best_two_stop_route
from app.infra.geo import GoogleMapsClient


def _failure_result(
    stage: str,
    reason: str,
    order_a: Dict[str, Any] | None = None,
    order_b: Dict[str, Any] | None = None,
    error: Exception | None = None,
) -> Dict[str, Any]:
    return {
        "success": False,
        "feasible": False,
        "stage": stage,
        "reason": reason,
        "error": str(error) if error else None,
        "order_a_id": (order_a or {}).get("order_id"),
        "order_b_id": (order_b or {}).get("order_id"),
    }


def _validate_order(order: Dict[str, Any], label: str) -> Dict[str, Any] | None:
    required_keys = [
        "order_id",
        "origin_store_id",
        "date",
        "deliver_at",
        "delivery_address",
    ]

    missing = [key for key in required_keys if not order.get(key)]
    if missing:
        return {
            "success": False,
            "feasible": False,
            "stage": "validating_inputs",
            "reason": f"{label} missing required fields: {', '.join(missing)}",
            "order_id": order.get("order_id"),
        }

    return None


def compute_pair_route_plan(order_a: dict, order_b: dict) -> dict:
    """
    Compute route feasibility and timing for a two-stop route.
    Returns structured failure data instead of raising whenever possible.
    """

    validation_error = _validate_order(order_a, "order_a")
    if validation_error:
        return validation_error

    validation_error = _validate_order(order_b, "order_b")
    if validation_error:
        return validation_error

    try:
        store_address = STORE_ADDRESSES[order_a["origin_store_id"]]
    except Exception as e:
        return _failure_result(
            stage="store_lookup",
            reason="origin store address not found",
            order_a=order_a,
            order_b=order_b,
            error=e,
        )

    try:
        maps_client = GoogleMapsClient()
    except Exception as e:
        return _failure_result(
            stage="maps_client_init",
            reason="failed to initialize Google Maps client",
            order_a=order_a,
            order_b=order_b,
            error=e,
        )

    try:
        store_to_a = maps_client.get_drive_minutes(
            origin=store_address,
            destination=order_a["delivery_address"],
        )
        store_to_b = maps_client.get_drive_minutes(
            origin=store_address,
            destination=order_b["delivery_address"],
        )
        a_to_b = maps_client.get_drive_minutes(
            origin=order_a["delivery_address"],
            destination=order_b["delivery_address"],
        )
        b_to_a = maps_client.get_drive_minutes(
            origin=order_b["delivery_address"],
            destination=order_a["delivery_address"],
        )
    except Exception as e:
        return _failure_result(
            stage="maps_distance_lookup",
            reason="failed to retrieve drive times",
            order_a=order_a,
            order_b=order_b,
            error=e,
        )

    try:
        route_result = compute_best_two_stop_route(
            origin_store_id_a=order_a["origin_store_id"],
            origin_store_id_b=order_b["origin_store_id"],
            date_str_a=order_a["date"],
            date_str_b=order_b["date"],
            order_a_id=order_a["order_id"],
            order_a_deliver_at=order_a["deliver_at"],
            order_a_window_start=((order_a.get("delivery_window") or {}).get("start") or order_a["deliver_at"]),
            store_to_a_minutes=store_to_a,
            order_b_id=order_b["order_id"],
            order_b_deliver_at=order_b["deliver_at"],
            order_b_window_start=((order_b.get("delivery_window") or {}).get("start") or order_b["deliver_at"]),
            store_to_b_minutes=store_to_b,
            a_to_b_minutes=a_to_b,
            b_to_a_minutes=b_to_a,
        )
    except Exception as e:
        return _failure_result(
            stage="route_computation",
            reason="failed to compute best two-stop route",
            order_a=order_a,
            order_b=order_b,
            error=e,
        )

    return {
        "success": True,
        "feasible": route_result.get("feasible", False),
        "stage": "completed",
        "origin_store_id": order_a["origin_store_id"],
        "store_address": store_address,
        "order_a_id": order_a["order_id"],
        "order_b_id": order_b["order_id"],
        "store_to_a": store_to_a,
        "store_to_b": store_to_b,
        "a_to_b": a_to_b,
        "b_to_a": b_to_a,
        "route_result": route_result,
    }