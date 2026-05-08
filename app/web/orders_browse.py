"""Browse persisted orders by store + date, drill into per-order or
per-day combined views."""
from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from flask import Blueprint, render_template, send_file, redirect, url_for, abort, request, jsonify

from app.db import get_db
from app.models import Order
from app.services.orders_query import (
    LOCATION_TO_ORIGIN,
    LOCATION_LABELS,
    list_orders_for_location,
    group_orders_by_date,
    build_grids_for_single_order,
    build_grids_for_orders,
)
from app.infra.export_xlsx import export_view_grids_to_xlsx

logger = logging.getLogger(__name__)
browse = Blueprint("orders_browse", __name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@browse.route("/orders/<location>")
def location_orders(location: str):
    location = location.lower()
    if location not in LOCATION_TO_ORIGIN:
        abort(404)
    db = next(get_db())
    try:
        orders = list_orders_for_location(db, location)
        groups = group_orders_by_date(orders)
        return render_template(
            "orders_by_store.html",
            location=location,
            location_label=LOCATION_LABELS[location],
            groups=groups,
        )
    finally:
        db.close()


@browse.route("/orders/view/<external_order_id>")
def view_order(external_order_id: str):
    db = next(get_db())
    try:
        result = build_grids_for_single_order(db, external_order_id)
        if not result:
            abort(404, f"Order {external_order_id} not found")
        order = result["order"]
        return render_template(
            "order_view.html",
            order=order,
            grids=result["grids"],
            active_view="master",
            title=f"Order {order.external_order_id}",
            mode="single",
            external_order_id=external_order_id,
        )
    finally:
        db.close()


@browse.route("/orders/view/<external_order_id>/xlsx")
def view_order_xlsx(external_order_id: str):
    db = next(get_db())
    try:
        result = build_grids_for_single_order(db, external_order_id)
        if not result:
            abort(404)
        xlsx = export_view_grids_to_xlsx(result["grids"])
        return send_file(
            io.BytesIO(xlsx),
            as_attachment=True,
            download_name=f"order_{external_order_id}.xlsx",
            mimetype=XLSX_MIME,
        )
    finally:
        db.close()


@browse.route("/orders/<location>/<date>")
def combined_day(location: str, date: str):
    location = location.lower()
    if location not in LOCATION_TO_ORIGIN:
        abort(404)
    db = next(get_db())
    try:
        origin = LOCATION_TO_ORIGIN[location]
        orders = (
            db.query(Order)
            .filter(Order.origin_store_id == origin, Order.delivery_date == date)
            .filter(Order.status != "cancelled")
            .order_by(Order.deliver_at)
            .all()
        )
        if not orders:
            abort(404, f"No orders for {LOCATION_LABELS[location]} on {date}")
        collapse = request.args.get("collapse_empty_rows") == "1"
        result = build_grids_for_orders(db, orders, collapse_empty_rows=collapse)
        return render_template(
            "order_view.html",
            order=None,
            grids=result["grids"],
            active_view="master",
            title=f"All {LOCATION_LABELS[location]} orders — {date}",
            mode="combined",
            combined_count=len(orders),
            location=location,
            location_label=LOCATION_LABELS[location],
            date=date,
            collapse_empty_rows=collapse,
        )
    finally:
        db.close()


@browse.route("/orders/<location>/<date>/xlsx")
def combined_day_xlsx(location: str, date: str):
    location = location.lower()
    if location not in LOCATION_TO_ORIGIN:
        abort(404)
    db = next(get_db())
    try:
        origin = LOCATION_TO_ORIGIN[location]
        orders = (
            db.query(Order)
            .filter(Order.origin_store_id == origin, Order.delivery_date == date)
            .filter(Order.status != "cancelled")
            .order_by(Order.deliver_at)
            .all()
        )
        if not orders:
            abort(404)
        collapse = request.args.get("collapse_empty_rows") == "1"
        result = build_grids_for_orders(db, orders, collapse_empty_rows=collapse)
        xlsx = export_view_grids_to_xlsx(result["grids"])
        return send_file(
            io.BytesIO(xlsx),
            as_attachment=True,
            download_name=f"{location}_{date}.xlsx",
            mimetype=XLSX_MIME,
        )
    finally:
        db.close()


# --- Courier unassign action -------------------------------------------------
# When the auto-pipeline assigns Sam CK #1 / Masood CK #2 via the ezCater
# courierAssign API, ezCater's portal won't let the user unassign that courier
# through the UI. This endpoint calls courierUnassign on Cenas Kitchen's
# behalf so the portal driver field opens up for manual reassignment.

import os

EZCATER_API = "https://api.ezcater.com/graphql"
EZ_TOKEN_FILE = Path(r"C:\Users\sam\.openclaw\.secrets\ezcater_api_token.txt")


def _ez_token_unassign() -> str:
    """Token resolver: env var (Render) wins over file (AiCk)."""
    val = os.getenv("EZCATER_API_TOKEN")
    if val:
        return val.strip()
    return EZ_TOKEN_FILE.read_text(encoding="utf-8").strip()

# Mirror the mapping in ezcater_webhook.py so we can derive which courier
# was auto-assigned without storing that on the Order row.
# Sam handles Tomball (#2). Masood handles Copperfield (#1).
_COURIER_ID_FOR_STORE = {
    "store_1": "masood-ck-1", "store_3": "masood-ck-1",  # Copperfield kitchen
    "store_2": "sam-ck-2",    "store_4": "sam-ck-2",     # Tomball kitchen
}

# Pre-swap orders (ingested before 2026-05-08) had the inverted IDs assigned.
# When unassigning we try the new id first, then fall back to the old one
# if the new id wasn't actually the courier on that delivery.
_LEGACY_COURIER_ID_FOR_STORE = {
    "store_1": "sam-ck-1",     "store_3": "sam-ck-1",
    "store_2": "masood-ck-2",  "store_4": "masood-ck-2",
}


def _ezcater_gql(query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        EZCATER_API, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {_ez_token_unassign()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (cenaskitchen unassign-courier)",
            "Origin": "https://api.ezcater.com",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode(errors="replace")[:300]}


_UNASSIGN_MUTATION = """
mutation Unassign($input: CourierUnassignInput!) {
  courierUnassign(input: $input) {
    delivery { id }
    userErrors {
      __typename
      ... on UserError { message }
      ... on DeliveryValidationError { message }
    }
  }
}
"""


def _try_unassign(delivery_id: str, courier_id: str) -> tuple[bool, str]:
    """Returns (ok, error_msg). ok=True means courierUnassign returned no errors."""
    res = _ezcater_gql(_UNASSIGN_MUTATION,
                       {"input": {"deliveryId": delivery_id, "courierId": courier_id}})
    if "_http_error" in res:
        return False, f"ezCater API HTTP {res['_http_error']}: {res.get('_body', '')[:120]}"
    if "errors" in res:
        msgs = "; ".join(e.get("message", "?") for e in res["errors"])[:300]
        return False, f"ezCater error: {msgs}"
    payload = (res.get("data") or {}).get("courierUnassign") or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        msgs = "; ".join(e.get("message", "?") for e in user_errors if isinstance(e, dict))[:300]
        return False, f"unassign rejected: {msgs}"
    return True, ""


@browse.route("/orders/view/<external_order_id>/unassign-courier", methods=["POST"])
def unassign_courier(external_order_id: str):
    """Free up the ezCater portal driver field so a manager can manually
    assign a real driver. Calls courierUnassign on the in-house courier
    auto-assigned by ezcater_webhook.py.

    Tries the new courier id (sam-ck-2 / masood-ck-1) first, then falls
    back to the legacy id (sam-ck-1 / masood-ck-2) if the order pre-dates
    the 2026-05-08 swap."""
    db = next(get_db())
    try:
        order = db.query(Order).filter_by(external_order_id=external_order_id).first()
        if not order:
            return jsonify({"ok": False, "error": "order not found"}), 404
        if not order.external_delivery_id:
            return jsonify({
                "ok": False,
                "error": ("This order doesn't have a stored ezCater delivery ID. "
                          "Either it pre-dates the API ingest pipeline or wasn't "
                          "auto-assigned. Use the ezCater portal directly.")
            }), 400
        primary = _COURIER_ID_FOR_STORE.get(order.origin_store_id)
        legacy = _LEGACY_COURIER_ID_FOR_STORE.get(order.origin_store_id)
        if not primary:
            return jsonify({
                "ok": False,
                "error": f"unknown origin_store_id={order.origin_store_id!r}"
            }), 400

        ok, err = _try_unassign(order.external_delivery_id, primary)
        unassigned = primary
        if not ok and legacy and legacy != primary:
            logger.info("primary unassign of %s failed (%s); retrying with legacy id %s",
                        primary, err[:80], legacy)
            ok, err = _try_unassign(order.external_delivery_id, legacy)
            if ok:
                unassigned = legacy
        if not ok:
            return jsonify({"ok": False, "error": err}), 502

        logger.info("unassigned courier %s from delivery %s (order %s)",
                    unassigned, order.external_delivery_id, external_order_id)
        return jsonify({
            "ok": True,
            "unassigned": unassigned,
            "delivery_id": order.external_delivery_id,
            "note": "Refresh the ezCater portal — the driver field should now be open for manual assignment.",
        })
    finally:
        db.close()
