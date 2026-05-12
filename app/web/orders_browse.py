"""Browse persisted orders by store + date, drill into per-order or
per-day combined views."""
from __future__ import annotations

import io
import json
import logging
import re
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
from app.services.ezcater_known_drivers_seed import seed_roster
from app.infra.export_xlsx import export_view_grids_to_xlsx

logger = logging.getLogger(__name__)
browse = Blueprint("orders_browse", __name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _drivers_for_kitchen(prefix: int) -> list[dict]:
    """Seed drivers whose ck_prefix matches the kitchen, plus the ambiguous
    NULL-prefix entries (Angelica Truss + James Paddie) which appear in both."""
    return [d for d in seed_roster()
            if d.get("ck_prefix") == prefix or d.get("ck_prefix") is None]


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
            drivers_ck1=_drivers_for_kitchen(1),
            drivers_ck2=_drivers_for_kitchen(2),
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


_ASSIGN_MUTATION = """
mutation Assign($input: CourierAssignInput!) {
  courierAssign(input: $input) {
    delivery { id }
    userErrors {
      __typename
      ... on UserError { message }
      ... on DeliveryValidationError { message }
    }
  }
}
"""


# origin_store_id → CK kitchen prefix used to slug the courier id below.
# Stores 1+3 sit on the Copperfield kitchen ("CK #1"); 2+4 on Tomball ("CK #2").
_KITCHEN_PREFIX_FOR_STORE = {
    "store_1": 1, "store_3": 1,
    "store_2": 2, "store_4": 2,
}


def _slug_driver_id(name: str, ck_prefix: int | None) -> str:
    """Build a stable ezCater courier id from a known-driver name + kitchen.
    Mirrors the shape of the auto-assigned 'sam-ck-2' / 'masood-ck-1' so we
    have a single id format across both flows. NULL ck_prefix (Angelica +
    James) gets a '-ck-0' suffix when the order's kitchen is unknown."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    suffix = str(ck_prefix) if ck_prefix in (1, 2) else "0"
    return f"{base}-ck-{suffix}" if base else f"unknown-ck-{suffix}"


def _courier_dict_from_seed(driver: dict, ck_prefix: int | None) -> dict:
    """Build the courier dict expected by the ezCater courierAssign mutation.
    Same shape as SAM / MASOOD in ezcater_webhook.py."""
    name = (driver.get("name") or "").strip()
    parts = name.split(maxsplit=1)
    first = parts[0] if parts else name
    last = parts[1] if len(parts) > 1 else ""
    digits = "".join(c for c in (driver.get("phone_e164") or "") if c.isdigit())
    phone = f"+1{digits}" if digits else ""
    return {
        "id": _slug_driver_id(name, ck_prefix),
        "firstName": first,
        "lastName": last,
        "phone": phone,
        "providerSource": "IN_HOUSE",
    }


def _try_assign(delivery_id: str, courier: dict) -> tuple[bool, str]:
    res = _ezcater_gql(_ASSIGN_MUTATION,
                       {"input": {"deliveryId": delivery_id, "courier": courier}})
    if "_http_error" in res:
        return False, f"ezCater API HTTP {res['_http_error']}: {res.get('_body', '')[:120]}"
    if "errors" in res:
        msgs = "; ".join(e.get("message", "?") for e in res["errors"])[:300]
        return False, f"ezCater error: {msgs}"
    payload = (res.get("data") or {}).get("courierAssign") or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        msgs = "; ".join(e.get("message", "?") for e in user_errors if isinstance(e, dict))[:300]
        return False, f"assign rejected: {msgs}"
    return True, ""


_DELIVERY_LOOKUP_QUERY = """
query OrderDeliveryLookup($id: ID!) {
  order(id: $id) {
    uuid
    orderNumber
    deliveryId
    caterer { name uuid }
  }
}
"""


def _fetch_delivery_id_for_order(external_order_id: str) -> str | None:
    """Look up the ezCater deliveryId for an order_number using api.ezcater.com.
    Tries both the as-stored form (with dash) and the dash-stripped form
    since ezCater's `order(id:)` lookup accepts only the no-dash variant.
    Returns the deliveryId UUID or None.
    """
    candidates = [external_order_id]
    if "-" in external_order_id:
        candidates.append(external_order_id.replace("-", ""))
    for cand in candidates:
        res = _ezcater_gql(_DELIVERY_LOOKUP_QUERY, {"id": cand})
        order = (res.get("data") or {}).get("order") or {}
        delivery_id = order.get("deliveryId")
        if delivery_id:
            return delivery_id
    return None


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


def _ensure_delivery_id(order: Order, db) -> tuple[bool, str | None]:
    """If the Order row is missing external_delivery_id (xlsx-imported,
    Cenas-Fajitas, pre-webhook), look it up via api.ezcater.com and backfill.
    Returns (ok, error_msg)."""
    if order.external_delivery_id:
        return True, None
    looked_up = _fetch_delivery_id_for_order(order.external_order_id)
    if not looked_up:
        return False, ("ezCater API didn't return a deliveryId for this order. "
                       "It may not exist on ezCater's side, or our API token "
                       "isn't authorized for this caterer.")
    order.external_delivery_id = looked_up
    db.commit()
    logger.info("backfilled external_delivery_id=%s for order %s",
                looked_up, order.external_order_id)
    return True, None


def _seed_match_by_name(name: str) -> dict | None:
    if not name:
        return None
    target = name.strip()
    return next((d for d in seed_roster() if d.get("name") == target), None)


@browse.route("/orders/view/<external_order_id>/assign-driver", methods=["POST"])
def assign_driver(external_order_id: str):
    """Push a known seed driver to ezCater via courierAssign + record the
    name on the Order row. The driver dict mirrors what the webhook builds
    for Sam / Masood on first ingest, so ezCater treats it the same way."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "missing driver name"}), 400
    match = _seed_match_by_name(name)
    if not match:
        return jsonify({"ok": False, "error": f"driver not in known roster: {name}"}), 400

    db = next(get_db())
    try:
        order = db.query(Order).filter_by(external_order_id=external_order_id).first()
        if not order:
            return jsonify({"ok": False, "error": "order not found"}), 404
        ok, err = _ensure_delivery_id(order, db)
        if not ok:
            return jsonify({"ok": False, "error": err}), 400

        # Disambiguate Angelica + James (NULL ck_prefix) using the order's
        # kitchen so the id ends in -ck-1 or -ck-2 instead of -ck-0.
        kitchen_prefix = _KITCHEN_PREFIX_FOR_STORE.get(order.origin_store_id)
        ck_prefix = match["ck_prefix"] if match.get("ck_prefix") in (1, 2) else kitchen_prefix
        courier = _courier_dict_from_seed(match, ck_prefix)

        ok, err = _try_assign(order.external_delivery_id, courier)
        if not ok:
            return jsonify({"ok": False, "error": err}), 502

        order.assigned_driver = name
        db.commit()
        logger.info("assigned courier %s (%s) to delivery %s (order %s)",
                    courier["id"], name, order.external_delivery_id, external_order_id)
        return jsonify({
            "ok": True,
            "assigned": name,
            "courier_id": courier["id"],
            "delivery_id": order.external_delivery_id,
        })
    finally:
        db.close()


@browse.route("/orders/view/<external_order_id>/unassign-courier", methods=["POST"])
def unassign_courier(external_order_id: str):
    """Free up the ezCater portal driver field. Tries — in order — the
    courier id slugged from Order.assigned_driver (if a manager-assigned
    driver is on the row), then the auto-assigned Sam/Masood ids, then the
    pre-2026-05-08 legacy variants, then a brute fallback of all four
    Sam/Masood ids for orders whose origin store isn't in our static map.

    On success, also clears Order.assigned_driver so the row's driver column
    flips back to the Assign-Driver dropdown."""
    db = next(get_db())
    try:
        order = db.query(Order).filter_by(external_order_id=external_order_id).first()
        if not order:
            return jsonify({"ok": False, "error": "order not found"}), 404
        ok, err = _ensure_delivery_id(order, db)
        if not ok:
            return jsonify({"ok": False, "error": err}), 400

        candidates: list[str] = []

        # 1. Manager-assigned driver (if any): slug back to the courier id we
        #    pushed to ezCater in assign_driver. Try this first because it's
        #    what's currently on the delivery in 99% of post-Assign flows.
        if order.assigned_driver:
            match = _seed_match_by_name(order.assigned_driver)
            kitchen_prefix = _KITCHEN_PREFIX_FOR_STORE.get(order.origin_store_id)
            ck_prefix = (match["ck_prefix"] if match and match.get("ck_prefix") in (1, 2)
                         else kitchen_prefix)
            candidates.append(_slug_driver_id(order.assigned_driver, ck_prefix))

        # 2. Sam / Masood auto-assignment (new ids).
        primary = _COURIER_ID_FOR_STORE.get(order.origin_store_id)
        legacy = _LEGACY_COURIER_ID_FOR_STORE.get(order.origin_store_id)
        if primary:
            candidates.append(primary)
            if legacy and legacy != primary:
                candidates.append(legacy)
        else:
            candidates.extend(["sam-ck-1", "sam-ck-2", "masood-ck-1", "masood-ck-2"])
            logger.info("origin_store_id=%r not in static map; trying all couriers",
                        order.origin_store_id)

        # De-dup while preserving order.
        seen = set()
        deduped = [c for c in candidates if not (c in seen or seen.add(c))]

        ok, err, unassigned = False, "", None
        for cid in deduped:
            ok, err = _try_unassign(order.external_delivery_id, cid)
            if ok:
                unassigned = cid
                break
            logger.info("unassign of %s failed (%s); trying next courier", cid, err[:80])
        if not ok:
            return jsonify({"ok": False, "error": err}), 502

        # Clear local assignment so the row UI flips back to the dropdown.
        prior = order.assigned_driver
        order.assigned_driver = None
        db.commit()

        logger.info("unassigned courier %s from delivery %s (order %s, prior assigned_driver=%r)",
                    unassigned, order.external_delivery_id, external_order_id, prior)
        return jsonify({
            "ok": True,
            "unassigned": unassigned,
            "delivery_id": order.external_delivery_id,
            "note": "Refresh the ezCater portal — the driver field should now be open for manual assignment.",
        })
    finally:
        db.close()
