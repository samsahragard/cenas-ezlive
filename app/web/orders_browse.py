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
    rotated_dispatch_letters,
    build_grids_for_single_order,
    build_grids_for_orders,
)
from app.infra.export_xlsx import export_view_grids_to_xlsx
# Phase 0 Block 4 follow-up (ck 2026-05-13): tag-based gate on the
# unassign-courier action. URL gets a store_scope segment so the
# decorator can resolve store-scope against the user's assignment set
# via store_arg= — same shape as @requires_permission('drivers.admin',
# store_arg='store_slug') in store_routes.py.
from app.services.permissions import requires_permission

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
        display_drivers = rotated_dispatch_letters(groups)
        return render_template(
            "orders_by_store.html",
            location=location,
            location_label=LOCATION_LABELS[location],
            groups=groups,
            display_drivers=display_drivers,
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
        # Resolve the URL store_scope segment for the unassign-courier
        # action button — "tomball" / "copperfield" / "unknown" (last
        # for Cenas Fajitas etc. that aren't in _ORIGIN_STORE_ID_TO_SCOPE
        # — only partner + corporate pass the decorator for those).
        store_scope = _ORIGIN_STORE_ID_TO_SCOPE.get(order.origin_store_id) or "unknown"
        return render_template(
            "order_view.html",
            order=order,
            grids=result["grids"],
            active_view="master",
            title=f"Order {order.external_order_id}",
            mode="single",
            external_order_id=external_order_id,
            unassign_store_scope=store_scope,
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


@browse.route("/orders/both/<date>")
def combined_day_both(date: str):
    """Combined view of Tomball + Copperfield orders for a given date.

    Per Sam #2870 (orders dashboard item 3): the existing /orders/both/<date>
    URL was 404'ing because the route only handled per-location paths
    (/orders/<location>/<date>). This adds the both-locations rollup.

    Same shape as combined_day() but filters origin_store_id IN the full
    LOCATION_TO_ORIGIN values set + sorts by (origin_store_id, deliver_at)
    so each store's block stays clustered while still showing both.
    """
    db = next(get_db())
    try:
        origin_ids = list(LOCATION_TO_ORIGIN.values())
        orders = (
            db.query(Order)
            .filter(Order.origin_store_id.in_(origin_ids),
                    Order.delivery_date == date)
            .filter(Order.status != "cancelled")
            .order_by(Order.origin_store_id, Order.deliver_at)
            .all()
        )
        if not orders:
            abort(404, f"No orders for both locations on {date}")
        collapse = request.args.get("collapse_empty_rows") == "1"
        result = build_grids_for_orders(db, orders, collapse_empty_rows=collapse)
        return render_template(
            "order_view.html",
            order=None,
            grids=result["grids"],
            active_view="master",
            title=f"All Tomball + Copperfield orders — {date}",
            mode="combined",
            combined_count=len(orders),
            location="both",
            location_label="Tomball + Copperfield",
            date=date,
            collapse_empty_rows=collapse,
        )
    finally:
        db.close()


@browse.route("/orders/both/<date>/xlsx")
def combined_day_both_xlsx(date: str):
    """xlsx export of the combined Tomball + Copperfield day view."""
    db = next(get_db())
    try:
        origin_ids = list(LOCATION_TO_ORIGIN.values())
        orders = (
            db.query(Order)
            .filter(Order.origin_store_id.in_(origin_ids),
                    Order.delivery_date == date)
            .filter(Order.status != "cancelled")
            .order_by(Order.origin_store_id, Order.deliver_at)
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
            download_name=f"both_{date}.xlsx",
            mimetype=XLSX_MIME,
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

# Map Order.origin_store_id (internal store identifier from ezCater /
# Toast) to the User.store_scope token used by the permission decorator.
# store_1 + store_3 = Copperfield kitchen; store_2 + store_4 = Tomball.
# Mirrors _COURIER_ID_FOR_STORE — extend both together if new origins
# appear.
_ORIGIN_STORE_ID_TO_SCOPE = {
    "store_1": "copperfield", "store_3": "copperfield",
    "store_2": "tomball",     "store_4": "tomball",
}

# Valid store_scope tokens accepted in the unassign-courier URL.
# "unknown" is the sentinel used for orders whose origin_store_id isn't
# in _ORIGIN_STORE_ID_TO_SCOPE (Cenas Fajitas etc.) — partner wildcard
# + corporate (no own store_scope, store check bypassed) still pass the
# decorator; everyone else is denied. Tagging it explicitly in the URL
# (rather than allowing any string) keeps the surface predictable.
_VALID_STORE_SCOPES = {"tomball", "copperfield", "unknown"}


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


def _open_for_bidding(order: Order) -> bool:
    """Flip an order into the Ez Market bid pool: status='available' plus
    delivery_window_start/_end derived from the existing time fields.

    Returns True if anything changed (caller should commit + log). Skips
    silently if the order is already past 'available' (requested, approved,
    delivered, etc.) — re-opening would lose pending state.

    Window derivation priority:
      1. existing order.delivery_window JSON dict (from ezCater payload —
         keys like {'start': iso, 'end': iso} or {'startTime', 'endTime'})
      2. order.deliver_at parsed as datetime ± 30 min cushion
      3. delivery_date midnight ± nothing (no time precision) so the
         order at least sorts by day on Ez Market
    """
    from datetime import datetime, timedelta
    # Only re-open orders in pre-bid states. Don't clobber requested/
    # approved/picked_up/etc.
    if order.status not in ("new", "available", "cancelled"):
        return False

    changed = False

    if order.status != "available":
        order.status = "available"
        changed = True

    if order.delivery_window_start is None or order.delivery_window_end is None:
        start, end = None, None
        # 1. ezCater JSON
        dw = order.delivery_window or {}
        if isinstance(dw, dict):
            for sk in ("start", "startTime", "startsAt"):
                if dw.get(sk):
                    try:
                        start = datetime.fromisoformat(str(dw[sk]).replace("Z", "+00:00"))
                        break
                    except (ValueError, TypeError):
                        pass
            for ek in ("end", "endTime", "endsAt"):
                if dw.get(ek):
                    try:
                        end = datetime.fromisoformat(str(dw[ek]).replace("Z", "+00:00"))
                        break
                    except (ValueError, TypeError):
                        pass
        # 2. deliver_at + 30 min cushion either side
        if start is None and order.deliver_at:
            for fmt_tries in (
                lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
                lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%S"),
                lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
            ):
                try:
                    mid = fmt_tries(order.deliver_at)
                    start = mid - timedelta(minutes=30)
                    end = mid + timedelta(minutes=30)
                    break
                except (ValueError, TypeError, AttributeError):
                    continue
        # 3. delivery_date midnight, no precision
        if start is None and order.delivery_date:
            try:
                start = datetime.fromisoformat(order.delivery_date)
                end = start + timedelta(hours=12)
            except (ValueError, TypeError):
                pass

        if start is not None:
            order.delivery_window_start = start
            order.delivery_window_end = end or (start + timedelta(hours=1))
            changed = True

    # NOTE: potential_payout snapshot intentionally left unset here. The
    # Ez Market template falls back to the in-template formula (base $25 +
    # tracked $10 + distance) when potential_payout is None, which keeps
    # the display correct without coupling this route to the full payroll
    # computation. A separate Phase-0 block can wire compute_one() through
    # once aick's Routes API miles-backfill (d9c58c2) has populated
    # pickup_miles for every visible order.

    return changed


@browse.route("/<store_scope>/orders/view/<external_order_id>/unassign-courier",
              methods=["POST"])
@requires_permission("orders.unassign_driver", store_arg="store_scope")
def unassign_courier(store_scope: str, external_order_id: str):
    """Free up the ezCater portal driver field so a manager can manually
    assign a real driver. Calls courierUnassign on the in-house courier
    auto-assigned by ezcater_webhook.py.

    Tries the new courier id (sam-ck-2 / masood-ck-1) first, then falls
    back to the legacy id (sam-ck-1 / masood-ck-2) if the order pre-dates
    the 2026-05-08 swap.

    URL shape (Phase 0 Block 4 follow-up 2026-05-13): the leading
    <store_scope> segment ("tomball" / "copperfield") is the user's
    assignment token, validated by @requires_permission(store_arg=...).
    After the decorator passes, the handler also verifies that the
    URL's store_scope MATCHES the order's actual origin_store_id-derived
    scope — defense against a Tomball GM crafting a Copperfield order's
    URL with their own store in the slug.
    """
    if store_scope not in _VALID_STORE_SCOPES:
        return jsonify({"ok": False, "error": "invalid store_scope"}), 404
    db = next(get_db())
    try:
        order = db.query(Order).filter_by(external_order_id=external_order_id).first()
        if not order:
            return jsonify({"ok": False, "error": "order not found"}), 404
        # Cross-check: the slug in the URL must match the order's own
        # origin. Prevents URL tampering as a side-channel around the
        # decorator's store-scope check — a user with assignment to
        # tomball couldn't unassign a copperfield order by guessing the
        # external_order_id and using their own slug; the decorator
        # would let them in, but this assert turns it into a 403.
        # Unknown-origin orders (no entry in the map) require the
        # explicit "unknown" sentinel in the URL — same gate.
        actual_scope = _ORIGIN_STORE_ID_TO_SCOPE.get(order.origin_store_id) or "unknown"
        if actual_scope != store_scope:
            return jsonify({
                "ok": False,
                "error": f"store_scope '{store_scope}' does not own this order",
            }), 403
        # If delivery_id wasn't captured at ingest time (xlsx-import orders,
        # Cenas Fajitas orders that bypass the webhook, anything pre-dating
        # the webhook flow), look it up on the fly via the api.ezcater.com
        # order(id:) query and backfill the row so subsequent operations work.
        if not order.external_delivery_id:
            looked_up = _fetch_delivery_id_for_order(external_order_id)
            if not looked_up:
                return jsonify({
                    "ok": False,
                    "error": ("ezCater API didn't return a deliveryId for this order. "
                              "It may not exist on ezCater's side, or our API token "
                              "isn't authorized for this caterer.")
                }), 400
            order.external_delivery_id = looked_up
            db.commit()
            logger.info("backfilled external_delivery_id=%s for order %s",
                        looked_up, external_order_id)
        # Determine which courier id to unassign. The static store map covers
        # Cenas Kitchen origin stores; for unmapped origins (Cenas Fajitas etc.)
        # we try every known courier id so the right one wins regardless of
        # which Cenas brand auto-assigned.
        primary = _COURIER_ID_FOR_STORE.get(order.origin_store_id)
        legacy = _LEGACY_COURIER_ID_FOR_STORE.get(order.origin_store_id)
        if primary:
            candidates = [primary] + ([legacy] if legacy and legacy != primary else [])
        else:
            candidates = ["sam-ck-1", "sam-ck-2", "masood-ck-1", "masood-ck-2"]
            logger.info("origin_store_id=%r not in static map; trying all couriers",
                        order.origin_store_id)
        ok, err, unassigned = False, "", None
        for cid in candidates:
            ok, err = _try_unassign(order.external_delivery_id, cid)
            if ok:
                unassigned = cid
                break
            logger.info("unassign of %s failed (%s); trying next courier", cid, err[:80])
        if not ok:
            return jsonify({"ok": False, "error": err}), 502

        logger.info("unassigned courier %s from delivery %s (order %s)",
                    unassigned, order.external_delivery_id, external_order_id)

        # Phase 0 Block 3 (ck, 2026-05-13): auto-open the order to the Ez
        # Market bid pool now that no driver is assigned. Option A from
        # the ck-2026-05-12 open thread: unassign-courier auto-flips
        # status='available' + populates delivery_window_start/_end so
        # the order shows up on /ez-market for drivers to request.
        _opened = _open_for_bidding(order)
        if _opened:
            db.commit()
            logger.info("opened order %s for bidding (window=%s..%s)",
                        external_order_id,
                        order.delivery_window_start, order.delivery_window_end)

        return jsonify({
            "ok": True,
            "unassigned": unassigned,
            "delivery_id": order.external_delivery_id,
            "opened_for_bidding": _opened,
            "note": ("Refresh the ezCater portal — the driver field should "
                     "now be open for manual assignment. The order is also "
                     "now visible in Ez Market for driver bidding."
                     if _opened else
                     "Refresh the ezCater portal — the driver field should "
                     "now be open for manual assignment."),
        })
    finally:
        db.close()
