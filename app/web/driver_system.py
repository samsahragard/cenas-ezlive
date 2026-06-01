"""Driver-system routes: Ez Market / Ez Manage / My Profile / Pay History.

Pages defined per SPEC.md §4-7 and §16. Calls into:
  - app.services.delivery_lifecycle for state transitions
  - app.services.driver_scoring for the My Profile score breakdown
  - app.services.routing_service.compute_pair_route_plan for the stack modal

Auth gating:
  - Driver-facing pages (/ez-market, /my-profile, /pay-history) require
    a driver session (session['driver_id'] set by /driver/login).
  - Manager-facing pages (/ez-manage) require a keypad-authenticated User
    with permission_level in {partner, corporate, gm, manager}.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from functools import wraps
from typing import Callable

from flask import (
    Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template,
    request, session, url_for,
)
from sqlalchemy import desc

from app.db import SessionLocal
from app.models import (
    Cancellation,
    DeliveryRequest,
    Driver,
    DriverNotification,
    DriverScore,
    Order,
    PayCheck,
    User,
)
from app.services import delivery_lifecycle as lifecycle
from app.services import driver_scoring as scoring

logger = logging.getLogger(__name__)

driver_system_bp = Blueprint("driver_system", __name__)

MANAGER_ROLES = {"partner", "corporate", "gm", "manager"}


# ---- auth helpers ----

def _current_driver() -> Driver | None:
    """Return the logged-in Driver row, or None if not signed in."""
    driver_id = session.get("driver_id")
    if not driver_id:
        return None
    db = SessionLocal()
    try:
        return db.get(Driver, driver_id)
    finally:
        db.close()


def require_driver(fn: Callable):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("driver_id"):
            return redirect(url_for("driver.driver_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


def require_manager(fn: Callable):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = getattr(g, "current_user", None)
        if not user or user.permission_level not in MANAGER_ROLES:
            return redirect(url_for("keypad_auth.login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


# ---- shared helpers ----

def _format_load(db, driver_id: int, today: date) -> str:
    """SPEC §5: '0 today' / '3 today (1 in progress)' / '2 today (done)' format."""
    today_iso = today.isoformat()
    delivered = (
        db.query(Order)
        .filter(Order.assigned_driver_id == driver_id)
        .filter(Order.status == "delivered")
        .filter(Order.delivery_date == today_iso)
        .count()
    )
    in_progress = (
        db.query(Order)
        .filter(Order.assigned_driver_id == driver_id)
        .filter(Order.status.in_(["approved", "picked_up", "en_route"]))
        .filter(Order.delivery_date == today_iso)
        .count()
    )
    total = delivered + in_progress
    if total == 0:
        return "0 today"
    if in_progress == 0:
        return f"{total} today (done)"
    return f"{total} today ({in_progress} in progress)"


def _potential_today(db, driver_id: int, today: date) -> float:
    today_iso = today.isoformat()
    rows = (
        db.query(Order.potential_payout)
        .filter(Order.assigned_driver_id == driver_id)
        .filter(Order.status.in_(["approved", "picked_up", "en_route", "delivered"]))
        .filter(Order.delivery_date == today_iso)
        .all()
    )
    return round(sum(r[0] or 0 for r in rows), 2)


def _my_queue_count(db, driver_id: int) -> int:
    pending = (
        db.query(DeliveryRequest)
        .filter(DeliveryRequest.driver_id == driver_id)
        .filter(DeliveryRequest.status == "pending")
        .count()
    )
    active = (
        db.query(Order)
        .filter(Order.assigned_driver_id == driver_id)
        .filter(Order.status.in_(["approved", "picked_up", "en_route"]))
        .count()
    )
    return pending + active


def _project_payout(order: Order) -> float:
    """Best-case potential payout for an unbid order so the card always shows
    a number. Assumes the driver will track the delivery; same formula as
    ezcater_payroll.compute_one with tracking forced on. Returns the stored
    potential_payout if it's already been snapshotted (open_for_bidding)."""
    if order.potential_payout is not None:
        return order.potential_payout
    from app.services.ezcater_payroll import (
        BASE_PER_DELIVERY, BONUS_TRACKED, PER_MILE_OVER_20, MILES_THRESHOLD,
    )
    miles = order.pickup_miles or 0.0
    extra_miles = max(0.0, miles - MILES_THRESHOLD)
    bonus_miles = round(extra_miles * PER_MILE_OVER_20, 2)
    return round(BASE_PER_DELIVERY + BONUS_TRACKED + bonus_miles, 2)


# origin_store_id -> kitchen slug used by ezcater_miles.KITCHEN_ADDRESSES
_ORIGIN_TO_KITCHEN = {
    "store_1": "copperfield",
    "store_3": "copperfield",
    "store_2": "tomball",
    "store_4": "tomball",
}

# Migration 11_payroll_backfill (commit fed513b, 2026-05-10 20:04:39 CDT =
# 2026-05-11 01:04:39 UTC) backfilled pickup_miles from ezCater's XLSX
# Delivery Performance Report. ezCater computes miles from the storefront-
# of-record (ghost address for store_3/store_4), so those legacy values
# are wrong under Sam's physical-kitchen routing model (policy locked in
# services/ezcater_miles.py + samai #1488). Any Order with created_at
# AT OR BEFORE this cutoff is a candidate for one-time lazy recompute on
# next visible view. Buffer of ~1h past the commit accounts for deploy
# swap + clock skew.
_LEGACY_MILES_CUTOFF = datetime(2026, 5, 11, 2, 0, 0)

# Per-process set of order ids already recomputed this run. Cache resets
# on app restart; the first post-restart view of each legacy order pays
# the Routes-API cost once, subsequent views skip. Idempotent — same
# inputs return the same Routes result, so a re-recompute is correct just
# wasteful.
_recomputed_legacy_ids: set[int] = set()


def _ensure_miles_for_visible(db, orders: list[Order], cap: int = 8) -> None:
    """Backfill pickup_kitchen + pickup_miles via Google Routes API for any
    orders that are missing the data (Sam 2026-05-12: 'correct location so
    we can properly pay the driver'). Capped per render to keep latency +
    API cost predictable — subsequent refreshes catch up the rest.

    Also lazily re-computes pickup_miles for pre-cutoff legacy orders whose
    stored miles came from migration 11's ezCater XLSX backfill (untrusted
    per Sam's 2026-05-15 policy). See _LEGACY_MILES_CUTOFF +
    _recomputed_legacy_ids for the gating.
    """
    from app.services.ezcater_miles import compute_one_way_miles
    # Fill in pickup_kitchen from origin_store_id where possible (cheap).
    for o in orders:
        if not o.pickup_kitchen and o.origin_store_id in _ORIGIN_TO_KITCHEN:
            o.pickup_kitchen = _ORIGIN_TO_KITCHEN[o.origin_store_id]
    # Pick the orders still missing miles + with the inputs needed to compute.
    needs_call = [
        o for o in orders
        if o.pickup_miles is None and o.pickup_kitchen and o.delivery_address
    ]
    # Track ids the missing-miles loop fed to Routes this view so the legacy
    # loop below doesn't re-call Routes for the same row in the same call
    # (samai FLAG 3, 2026-05-15 — eliminates the legacy-NULL double-call).
    just_computed_ids: set[int] = set()
    for o in needs_call[:cap]:
        miles = compute_one_way_miles(o.pickup_kitchen, o.delivery_address)
        if miles is not None:
            o.pickup_miles = miles
        just_computed_ids.add(o.id)
    # Lazy legacy recompute (samai spec, 2026-05-15). Same Routes-API call,
    # different gate: orders created at-or-before the migration-11 cutoff
    # with a kitchen + delivery address, not yet re-computed this process,
    # AND not just freshly computed by the missing-miles loop above.
    legacy_needs_recompute = [
        o for o in orders
        if (o.created_at is not None
            and o.created_at <= _LEGACY_MILES_CUTOFF
            and o.pickup_kitchen
            and o.delivery_address
            and o.id not in just_computed_ids
            and o.id not in _recomputed_legacy_ids)
    ]
    for o in legacy_needs_recompute[:cap]:
        miles = compute_one_way_miles(o.pickup_kitchen, o.delivery_address)
        if miles is not None:
            o.pickup_miles = miles
        # Mark recomputed regardless of API success — a None means Routes
        # was unreachable; retry on next process restart, not on every view.
        _recomputed_legacy_ids.add(o.id)
    db.commit()


def _potential_week(db, driver_id: int, today: date) -> float:
    """Running sum across the current bi-weekly pay period."""
    # Reuse the ezcater_payroll anchor math for period bounds.
    from app.services.ezcater_payroll import period_containing
    period_start, period_end, _ = period_containing(today)
    rows = (
        db.query(Order.potential_payout)
        .filter(Order.assigned_driver_id == driver_id)
        .filter(Order.status.in_(["approved", "picked_up", "en_route", "delivered"]))
        .filter(Order.delivery_date >= period_start.isoformat())
        .filter(Order.delivery_date <= period_end.isoformat())
        .all()
    )
    return round(sum(r[0] or 0 for r in rows), 2)


# ============================================================
# Ez Market — driver bid board (§4)
# ============================================================

@driver_system_bp.route("/ez-market", methods=["GET"])
def ez_market():
    # Per Sam 2026-05-12: Ez Market is viewable by drivers AND by any
    # non-driver permission (partner / corporate / gm / manager / expo) —
    # they get a read-only view (no Request buttons, no personal stats).
    driver = _current_driver()
    keypad_user = getattr(g, "current_user", None)
    if not driver and not keypad_user:
        return redirect(url_for("keypad_auth.login", next=request.path))
    today = date.today()
    db = SessionLocal()
    try:
        # Per Sam 2026-05-12: all upcoming orders flow into Ez Market regardless
        # of status — same listing as /orders/<location> (today + future,
        # excluding cancelled), but cross-store. The legacy status='available'
        # bid-pool gate + per-tier premium cap are dropped here; the future
        # request flow will layer on top.
        avail_q = (
            db.query(Order)
            .filter(Order.delivery_date >= today.isoformat())
            .filter(Order.status != "cancelled")
            .order_by(Order.delivery_date.asc(),
                      Order.deliver_at.asc().nullslast())
        )
        available = avail_q.limit(200).all()
        my_pending_reqs = []
        my_active = []
        my_history = []
        if driver:
            # Filter out orders this driver already has a pending request on
            existing_pending = {
                r.delivery_id for r in
                db.query(DeliveryRequest)
                  .filter(DeliveryRequest.driver_id == driver.id)
                  .filter(DeliveryRequest.status == "pending")
                  .all()
            }
            available = [o for o in available if o.id not in existing_pending]
            # My Queue = my pending requests + my active deliveries
            my_pending_reqs = (
                db.query(DeliveryRequest)
                .filter(DeliveryRequest.driver_id == driver.id)
                .filter(DeliveryRequest.status == "pending")
                .all()
            )
            my_active = (
                db.query(Order)
                .filter(Order.assigned_driver_id == driver.id)
                .filter(Order.status.in_(["approved", "picked_up", "en_route"]))
                .all()
            )
            # History — last 30 days delivered
            thirty_ago = (today - timedelta(days=30)).isoformat()
            my_history = (
                db.query(Order)
                .filter(Order.assigned_driver_id == driver.id)
                .filter(Order.status == "delivered")
                .filter(Order.delivery_date >= thirty_ago)
                .order_by(desc(Order.delivery_date))
                .limit(50)
                .all()
            )

        # Competition count per available order (number of other drivers requesting)
        competing = {}
        if available:
            ids = [o.id for o in available]
            from sqlalchemy import func as _f
            rows = (
                db.query(DeliveryRequest.delivery_id, _f.count(DeliveryRequest.id))
                .filter(DeliveryRequest.delivery_id.in_(ids))
                .filter(DeliveryRequest.status == "pending")
                .group_by(DeliveryRequest.delivery_id)
                .all()
            )
            competing = dict(rows)

        # Backfill miles via Google Routes API for any visible orders that
        # are missing them — accurate miles drive the payout formula
        # (Sam 2026-05-12: 'correct location so we can properly pay the
        # driver'). Capped per render so a single page load is bounded.
        _ensure_miles_for_visible(db, available, cap=8)

        # Group available by delivery_date with the same helper the
        # /orders/<location> page uses, and projected payouts for the card
        # display (Sam 2026-05-12: every card needs a $ figure visible).
        from app.services.orders_query import group_orders_by_date
        available_groups = group_orders_by_date(available)
        projected_payouts = {o.id: _project_payout(o) for o in available}

        # Header greeting — show the viewer's name regardless of role
        # (Sam 2026-05-12: "for a partner it would just say my name").
        viewer_name = "there"
        if driver and driver.name:
            viewer_name = driver.name.split()[0]
        elif keypad_user and getattr(keypad_user, "full_name", None):
            viewer_name = keypad_user.full_name.split()[0]
        elif session.get("partner_auth_ok"):
            # Legacy partner-password gate (no keypad user). Look up the
            # partner User row for the display name.
            partner_user = (
                db.query(User)
                .filter(User.permission_level == "partner")
                .first()
            )
            if partner_user and partner_user.full_name:
                viewer_name = partner_user.full_name.split()[0]

        # Issue B / samai #1599: unread driver notifications (persisted
        # so they survive logout). Most-recent-N pattern; template
        # surfaces inline cards + a count badge.
        unread_notifications = (
            db.query(DriverNotification)
            .filter(DriverNotification.driver_id == driver.id)
            .filter(DriverNotification.read_at.is_(None))
            .order_by(DriverNotification.created_at.desc())
            .limit(20)
            .all()
        ) if driver else []

        ctx = {
            "active": "ez_market",
            "driver": driver,
            "viewer_is_driver": bool(driver),
            "viewer_name": viewer_name,
            "available": available,
            "available_groups": available_groups,
            "projected_payouts": projected_payouts,
            "competing": competing,
            "my_pending_reqs": my_pending_reqs,
            "my_active": my_active,
            "my_history": my_history,
            "unread_notifications": unread_notifications,
            # None means "n/a" — template renders the category label but
            # substitutes 'n/a' for the value (Sam 2026-05-12).
            "stat_potential_today": _potential_today(db, driver.id, today) if driver else None,
            "stat_my_queue": _my_queue_count(db, driver.id) if driver else None,
            "stat_potential_week": _potential_week(db, driver.id, today) if driver else None,
            "current_tier": (driver.current_tier or "new") if driver else None,
        }
        return render_template("ez_market.html", **ctx)
    finally:
        db.close()


@driver_system_bp.route("/ez-market/request/<int:delivery_id>", methods=["POST"])
@require_driver
def ez_market_request(delivery_id: int):
    driver = _current_driver()
    if not driver:
        return redirect(url_for("driver.driver_login"))
    db = SessionLocal()
    try:
        order = db.get(Order, delivery_id)
        if not order:
            abort(404)
        # Tier cap check
        tier_caps = {"new": 1, "trusted": 2, "rockstar": 3, "top_rockstar": 5}
        cap = tier_caps.get(driver.current_tier or "new", 1)
        pending_count = (
            db.query(DeliveryRequest)
            .filter(DeliveryRequest.driver_id == driver.id)
            .filter(DeliveryRequest.status == "pending")
            .count()
        )
        if pending_count >= cap:
            flash(f"Max {cap} pending requests ({(driver.current_tier or 'new').replace('_', ' ').title()} tier).",
                  "error")
            return redirect(url_for("driver_system.ez_market"))
        # Issue B (samai #1599): same-day time-window conflict against
        # this driver's existing pending requests. First-of-day auto-
        # allows (pending_count==0 is handled by the cap check above being
        # a no-op for the first request). For 2nd+, refuse and surface
        # which existing request conflicts so the driver can cancel it
        # before re-requesting.
        if pending_count > 0:
            clash = lifecycle.find_conflicting_request(db, driver.id, order)
            if clash is not None:
                flash(
                    f"This conflicts with your pending request for "
                    f"order #{clash.delivery_id}. Cancel that request "
                    f"first, then try this one again.",
                    "error",
                )
                return redirect(url_for("driver_system.ez_market"))
        try:
            lifecycle.request_delivery(db, order, driver)
            db.commit()
            flash("Request submitted — manager will review.", "ok")
        except lifecycle.IllegalTransition as e:
            db.rollback()
            flash(f"Can't request this order: {e}", "error")
        except Exception:
            db.rollback()
            logger.exception("request_delivery failed")
            flash("Request failed — try again or refresh.", "error")
        return redirect(url_for("driver_system.ez_market"))
    finally:
        db.close()


@driver_system_bp.route("/ez-market/cancel-request/<int:request_id>", methods=["POST"])
@require_driver
def ez_market_cancel_request(request_id: int):
    driver = _current_driver()
    if not driver:
        return redirect(url_for("driver.driver_login"))
    db = SessionLocal()
    try:
        req = db.get(DeliveryRequest, request_id)
        if not req or req.driver_id != driver.id:
            abort(404)
        if req.status != "pending":
            flash("Request is no longer pending.", "error")
        else:
            req.status = "cancelled_by_driver"
            req.decided_at = datetime.utcnow()
            # If no other pending requests on this delivery, drop status back to available
            order = db.get(Order, req.delivery_id)
            if order and order.status == "requested":
                other_pending = (
                    db.query(DeliveryRequest)
                    .filter(DeliveryRequest.delivery_id == order.id)
                    .filter(DeliveryRequest.status == "pending")
                    .filter(DeliveryRequest.id != req.id)
                    .count()
                )
                if other_pending == 0:
                    order.status = "available"
            db.commit()
            flash("Request cancelled.", "ok")
        return redirect(url_for("driver_system.ez_market"))
    finally:
        db.close()


# ============================================================
# Issue B — driver notification mark-read
# ============================================================

@driver_system_bp.route("/ez-market/notifications/<int:notif_id>/dismiss",
                        methods=["POST"])
@require_driver
def ez_market_dismiss_notification(notif_id: int):
    """Mark one DriverNotification row as read. Idempotent: re-marking
    a read row is a no-op. Driver can only mark their own."""
    driver = _current_driver()
    if not driver:
        return redirect(url_for("driver.driver_login"))
    db = SessionLocal()
    try:
        n = db.get(DriverNotification, notif_id)
        if not n or n.driver_id != driver.id:
            abort(404)
        if n.read_at is None:
            n.read_at = datetime.utcnow()
            db.commit()
        return redirect(url_for("driver_system.ez_market"))
    finally:
        db.close()


# ============================================================
# Ez Manage — manager approval queue (§5)
# ============================================================

@driver_system_bp.route("/ez-manage", methods=["GET"])
@require_manager
def ez_manage():
    today = date.today()
    db = SessionLocal()
    try:
        # Group pending requests by delivery
        pending_reqs = (
            db.query(DeliveryRequest)
            .filter(DeliveryRequest.status == "pending")
            .order_by(DeliveryRequest.requested_at.asc())
            .all()
        )
        # Build delivery → [(driver, req, stats)] map
        groups: dict[int, dict] = {}
        for r in pending_reqs:
            order = db.get(Order, r.delivery_id)
            if not order:
                continue
            d = db.get(Driver, r.driver_id)
            if not d:
                continue
            grp = groups.setdefault(order.id, {"order": order, "rows": []})
            grp["rows"].append({
                "request": r,
                "driver": d,
                "load_str": _format_load(db, d.id, today),
            })
        # Sort each delivery's rows by recommendation rank
        tier_rank = {"top_rockstar": 0, "rockstar": 1, "trusted": 2, "new": 3}
        for g_ in groups.values():
            g_["rows"].sort(key=lambda row: (
                tier_rank.get(row["driver"].current_tier or "new", 9),
                -(row["driver"].current_score or 0),
            ))
            if g_["rows"]:
                g_["rows"][0]["recommended"] = True

        # Today's approved count for header
        approved_today = (
            db.query(Order)
            .filter(Order.status.in_(["approved", "picked_up", "en_route", "delivered"]))
            .filter(Order.delivery_date == today.isoformat())
            .filter(Order.approved_at.isnot(None))
            .count()
        )
        ctx = {
            "active": "ez_manage",
            "groups": list(groups.values()),
            "pending_count": len(groups),
            "approved_today_count": approved_today,
        }
        return render_template("ez_manage.html", **ctx)
    finally:
        db.close()


# JSON poll endpoint for the live pending badge in the manager sidebar
# (Cena #1758 req 4 + samai #1762 #1 / aick #1764 (4) — pending count
# visible at all times to managers, not just on the /ez-manage page).
# Front end polls every 30s and updates the badge. Returns 401 JSON on
# unauth instead of the decorator's 302-to-login so XHR can render
# 'auth lost' cleanly.
@driver_system_bp.route("/ez-manage/pending-count.json", methods=["GET"])
def ez_manage_pending_count():
    user = getattr(g, "current_user", None)
    if not user or user.permission_level not in MANAGER_ROLES:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    db = SessionLocal()
    try:
        # Same scoping as the page: ALL pending DeliveryRequests
        # regardless of order location, cross-store. If per-location
        # scoping becomes a product requirement later, filter by
        # Order.reported_store_id against user.assigned_store_id.
        n = (
            db.query(DeliveryRequest)
            .filter(DeliveryRequest.status == "pending")
            .count()
        )
        return jsonify({
            "ok": True,
            "pending_count": n,
            "ts": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        })
    finally:
        db.close()


@driver_system_bp.route("/ez-manage/approve/<int:request_id>", methods=["POST"])
@require_manager
def ez_manage_approve(request_id: int):
    db = SessionLocal()
    try:
        req = db.get(DeliveryRequest, request_id)
        if not req:
            abort(404)
        order = db.get(Order, req.delivery_id)
        driver = db.get(Driver, req.driver_id)
        if not order or not driver:
            abort(404)
        try:
            lifecycle.approve_request(db, order, driver, g.current_user.id)
            db.commit()
            flash(f"Approved {driver.name} for {order.external_order_id or order.id}.", "ok")
        except lifecycle.IllegalTransition as e:
            db.rollback()
            flash(f"Couldn't approve: {e}", "error")
        return redirect(url_for("driver_system.ez_manage"))
    finally:
        db.close()


@driver_system_bp.route("/ez-manage/decline/<int:request_id>", methods=["POST"])
@require_manager
def ez_manage_decline(request_id: int):
    db = SessionLocal()
    try:
        req = db.get(DeliveryRequest, request_id)
        if not req or req.status != "pending":
            abort(404)
        req.status = "declined"
        req.decided_at = datetime.utcnow()
        req.decided_by_user_id = g.current_user.id
        # If this was the last pending on the delivery, return to available
        other_pending = (
            db.query(DeliveryRequest)
            .filter(DeliveryRequest.delivery_id == req.delivery_id)
            .filter(DeliveryRequest.status == "pending")
            .filter(DeliveryRequest.id != req.id)
            .count()
        )
        order = db.get(Order, req.delivery_id)
        if other_pending == 0 and order and order.status == "requested":
            order.status = "available"
        # samai #1770: tell the declined driver. Without this they'd
        # find out only by reloading /ez-market and noticing the
        # request gone from their queue.
        db.add(DriverNotification(
            driver_id=req.driver_id,
            kind="declined_by_manager",
            message=(f"Manager declined your request for order "
                     f"#{req.delivery_id}"
                     + (f" ({order.client or 'unnamed'}, "
                        f"{order.delivery_date or 'no date'})"
                        if order else "")
                     + "."),
            related_delivery_id=req.delivery_id,
        ))
        db.commit()
        flash("Request declined.", "ok")
        return redirect(url_for("driver_system.ez_manage"))
    finally:
        db.close()


@driver_system_bp.route("/ez-manage/back-to-bidding/<int:delivery_id>", methods=["POST"])
@require_manager
def ez_manage_back_to_bidding(delivery_id: int):
    db = SessionLocal()
    try:
        order = db.get(Order, delivery_id)
        if not order:
            abort(404)
        try:
            lifecycle.back_to_bidding(db, order, g.current_user.id)
            db.commit()
            flash("Order reopened for bidding.", "ok")
        except lifecycle.IllegalTransition as e:
            db.rollback()
            flash(f"Couldn't reopen: {e}", "error")
        return redirect(url_for("driver_system.ez_manage"))
    finally:
        db.close()


@driver_system_bp.route("/ez-manage/decline-all/<int:delivery_id>", methods=["POST"])
@require_manager
def ez_manage_decline_all(delivery_id: int):
    db = SessionLocal()
    try:
        order = db.get(Order, delivery_id)
        if not order:
            abort(404)
        try:
            lifecycle.decline_all(db, order, g.current_user.id)
            db.commit()
            flash("All requests declined and order cancelled.", "ok")
        except lifecycle.IllegalTransition as e:
            db.rollback()
            flash(f"Couldn't decline-all: {e}", "error")
        return redirect(url_for("driver_system.ez_manage"))
    finally:
        db.close()


@driver_system_bp.route("/ez-manage/feasibility-check", methods=["GET"])
@require_manager
def ez_manage_feasibility_check():
    """Ajax endpoint for the stack confirmation modal — runs the pair
    feasibility check between a driver's active delivery and a candidate
    new delivery."""
    try:
        driver_id = int(request.args.get("driver_id", 0))
        new_id = int(request.args.get("new_delivery_id", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad params"}), 400
    db = SessionLocal()
    try:
        driver = db.get(Driver, driver_id)
        new_o = db.get(Order, new_id)
        if not driver or not new_o:
            return jsonify({"ok": False, "error": "driver or delivery not found"}), 404
        active = (
            db.query(Order)
            .filter(Order.assigned_driver_id == driver.id)
            .filter(Order.status.in_(["approved", "picked_up", "en_route"]))
            .first()
        )
        if not active:
            return jsonify({"ok": True, "stack_needed": False})
        if active.origin_store_id != new_o.origin_store_id:
            return jsonify({"ok": True, "stack_needed": True, "feasible": False,
                            "reason": "different origin stores"})
        if active.delivery_date != new_o.delivery_date:
            return jsonify({"ok": True, "stack_needed": True, "feasible": False,
                            "reason": "different delivery dates"})
        # Map to dispatch_planner's expected dict shape
        def _to_planner(o: Order) -> dict:
            return {
                "order_id": str(o.id),
                "origin_store_id": o.origin_store_id,
                "delivery_address": o.delivery_address,
                "date": o.delivery_date,
                "deliver_at": o.deliver_at,
                "delivery_window": o.delivery_window,
            }
        from app.services.routing_service import compute_pair_route_plan
        result = compute_pair_route_plan(_to_planner(active), _to_planner(new_o))
        return jsonify({"ok": True, "stack_needed": True, "feasible": result.get("feasible", False),
                        "result": result})
    finally:
        db.close()


# ============================================================
# My Profile — driver standing page (§7)
# ============================================================

@driver_system_bp.route("/my-profile", methods=["GET"])
@require_driver
def my_profile():
    driver = _current_driver()
    if not driver:
        return redirect(url_for("driver.driver_login"))
    today = date.today()
    db = SessionLocal()
    try:
        # Most-recent DriverScore for breakdown
        latest = (
            db.query(DriverScore)
            .filter(DriverScore.driver_id == driver.id)
            .order_by(desc(DriverScore.computed_at))
            .first()
        )
        ctx = {
            "active": "my_profile",
            "driver": driver,
            "score_row": latest,
            "current_tier": driver.current_tier or "new",
            "stat_potential_today": _potential_today(db, driver.id, today),
            "stat_my_queue": _my_queue_count(db, driver.id),
            "stat_potential_week": _potential_week(db, driver.id, today),
            "score": driver.current_score or 0,
        }
        return render_template("my_profile.html", **ctx)
    finally:
        db.close()


# ============================================================
# Pay History (§7, §10)
# ============================================================

@driver_system_bp.route("/pay-history", methods=["GET"])
@require_driver
def pay_history():
    driver = _current_driver()
    if not driver:
        return redirect(url_for("driver.driver_login"))
    from app.services.ezcater_payroll import paycheck_history
    db = SessionLocal()
    try:
        checks = (
            db.query(PayCheck)
            .filter(PayCheck.driver_id == driver.id)
            .order_by(desc(PayCheck.pay_period_end))
            .limit(24)
            .all()
        )
        # Per-delivery breakdown for the current + recent periods (Sam #1492):
        # the same E/D/V-miles table the office sees, on the driver's own login.
        history = paycheck_history(driver.name, periods=6)
        return render_template("pay_history.html",
                               active="pay_history",
                               driver=driver,
                               checks=checks,
                               history=history)
    finally:
        db.close()


@driver_system_bp.route("/pay-history/<int:delivery_id>/flag", methods=["POST"])
@require_driver
def pay_history_flag(delivery_id: int):
    driver = _current_driver()
    if not driver:
        return redirect(url_for("driver.driver_login"))
    db = SessionLocal()
    try:
        order = db.get(Order, delivery_id)
        if not order or order.assigned_driver_id != driver.id:
            abort(404)
        # Minimal: add a flag to the order's flags JSON. Surfaces in a
        # future admin queue. Full UX for resolution comes later.
        flags = list(order.flags or [])
        flags.append({
            "kind": "pay_discrepancy",
            "flagged_at": datetime.utcnow().isoformat(),
            "by_driver_id": driver.id,
            "note": (request.form.get("note") or "")[:500],
        })
        order.flags = flags
        db.commit()
        flash("Flagged. We'll review and reach out.", "ok")
        return redirect(url_for("driver_system.pay_history"))
    finally:
        db.close()


# ============================================================
# Cron entrypoints (no-show detection, nightly scoring)
# ============================================================

def _extract_cron_token() -> str | None:
    """Pull the CRON token from any of three places: an Authorization: Bearer
    header (Phase 0 spec), an X-Cron-Token header (legacy), or a ?token=
    query param. Returns the raw token string or None."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Cron-Token") or request.args.get("token")


@driver_system_bp.route("/cron/no-show-sweep", methods=["POST"])
def cron_no_show_sweep():
    """Trigger no-show detection. Token-gated via CRON_TOKEN env var.
    Accepts Authorization: Bearer (spec), X-Cron-Token, or ?token= ."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    db = SessionLocal()
    try:
        flagged = lifecycle.detect_no_shows(db)
        db.commit()
        return jsonify({"ok": True, "flagged_count": len(flagged)})
    finally:
        db.close()


@driver_system_bp.route("/cron/recompute-scores", methods=["POST"])
def cron_recompute_scores():
    """Trigger nightly driver-score recompute. Token-gated via CRON_TOKEN env var.
    Accepts Authorization: Bearer (spec), X-Cron-Token, or ?token= ."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    result = scoring.recompute_all_driver_scores()
    return jsonify({"ok": True, **result})


# ============================================================
# Anomaly bucket runner (Phase 1 / Block 1)
# Five cron buckets call POST /cron/anomaly-eval?bucket=<b> on their
# schedule. The handler delegates to anomaly_engine.run_bucket which
# runs every rule registered to that bucket.
# ============================================================

_VALID_ANOMALY_BUCKETS = {"every_5m", "every_15m", "hourly", "daily", "weekly"}


@driver_system_bp.route("/cron/anomaly-eval", methods=["POST"])
def cron_anomaly_eval():
    """Run all rules in a single bucket. Token-gated via CRON_TOKEN env var.
    Accepts Authorization: Bearer (spec), X-Cron-Token, or ?token= ."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    bucket = (request.args.get("bucket") or "").strip()
    if bucket not in _VALID_ANOMALY_BUCKETS:
        return jsonify({
            "ok": False,
            "error": f"bad bucket; expected one of {sorted(_VALID_ANOMALY_BUCKETS)}",
        }), 400
    from app.services.anomaly_engine import run_bucket
    summary = run_bucket(bucket)
    return jsonify({"ok": True, **summary})


@driver_system_bp.route("/cron/toast-sync", methods=["POST"])
def cron_toast_sync():
    """Bulk-refresh the Toast employee snapshots (Sam #2845). Token-gated via
    CRON_TOKEN. Drives the SAME in-app sync the background poller runs -- lets
    samai wire a Render cron as belt-and-suspenders, and lets us seed the
    snapshot immediately after a deploy. Optional ?store=tomball|copperfield to
    scope it. Accepts Authorization: Bearer (spec), X-Cron-Token, or ?token= ."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    from app.services.toast_sync import sync_toast_snapshots
    store = (request.args.get("store") or "").strip() or None
    summary = sync_toast_snapshots(only_store=store)
    return jsonify({"ok": True, **summary})


@driver_system_bp.route("/cron/anomaly-brief", methods=["POST"])
def cron_anomaly_brief():
    """Phase 1 / Block 6: compose one morning brief per enrolled
    audience for today (or ?date=YYYY-MM-DD). Token-gated like the
    other cron endpoints."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    from datetime import date as _date
    bd = request.args.get("date")
    try:
        brief_date = _date.fromisoformat(bd) if bd else _date.today()
    except ValueError:
        return jsonify({"ok": False, "error": "bad date"}), 400
    from app.services.brief_composer import compose_all_briefs
    summary = compose_all_briefs(brief_date)
    return jsonify({"ok": True, "brief_date": brief_date.isoformat(), **summary})


# ============================================================
# Task escalation scan (Phase 2 / Block 1E)
# The every_5m scheduler POSTs /cron/task-escalation. The handler
# delegates to escalation.run_escalation_scan: escalate overdue tasks
# (two tiers, capped) and expire stale sales insights.
# ============================================================

@driver_system_bp.route("/cron/task-escalation", methods=["POST"])
def cron_task_escalation():
    """Phase 2 / Block 1E: the every-5-minute escalation scan. Escalates
    overdue tasks to the owner's manager (and, after 24h, one tier
    further) and expires stale SalesInsight rows. Token-gated via
    CRON_TOKEN like the other cron endpoints; returns an inspectable
    JSON summary so a manual trigger / dry-run is legible."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    db = SessionLocal()
    try:
        from app.services.escalation import run_escalation_scan
        summary = run_escalation_scan(db)
        db.commit()
        return jsonify({"ok": True, **summary})
    finally:
        db.close()


# ============================================================
# Sales-insights synthesis (Phase 2 / Block 1F)
# A daily 5am-CT scheduler POSTs /cron/sales-insights. The handler
# delegates to sales_insights.run_sales_insights_synthesis: pull
# external intelligence, Haiku-normalize -> Opus-synthesize, write
# SalesInsight rows. The 5am-CT cron-job *resource* is created in the
# Render dashboard (via API), same as the other /cron/ schedules.
# ============================================================

@driver_system_bp.route("/cron/sales-insights", methods=["POST"])
def cron_sales_insights():
    """Phase 2 / Block 1F: the daily sales-insights synthesis. Pulls
    weather / events / traffic / outage intelligence, runs the
    Haiku-normalize -> Opus-synthesize pipeline, writes SalesInsight
    rows for the ribbon's Sales category. Token-gated via CRON_TOKEN
    like the other cron endpoints; returns an inspectable JSON summary
    (rows per category/store, raw-signal counts, fallback flag,
    estimated cost). run_sales_insights_synthesis owns its own session
    + commit (db=None path)."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    from app.services.sales_insights import run_sales_insights_synthesis
    summary = run_sales_insights_synthesis()
    return jsonify({"ok": True, **summary})


# ============================================================
# Block 1J — the six per-source ambient-signal refresh crons
# Each /cron/refresh-<source> POSTs from its own Render cron resource
# at its spec-§4 cadence; the handler delegates to
# ambient_signals.run_refresh_cron (fetch -> id-stable upsert ->
# per-source expiry sweep -> AmbientSignalRun). Cron-independent (§8):
# one failing affects nothing else. Endpoint AND Render cron resource
# are BOTH required — the 1E gap (endpoint shipped, resource never
# created) is not allowed to recur, let alone six times.
# ============================================================

def _run_ambient_refresh(source: str):
    """Shared body for the six /cron/refresh-* endpoints: CRON_TOKEN
    gate, run_refresh_cron for `source`, commit, return the run
    summary JSON. run_refresh_cron looks the adapter up by source."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    db = SessionLocal()
    try:
        from app.services.ambient_signals import run_refresh_cron
        summary = run_refresh_cron(db, source)
        db.commit()
        return jsonify({"ok": True, **summary})
    finally:
        db.close()


@driver_system_bp.route("/cron/refresh-weather", methods=["POST"])
def cron_refresh_weather():
    """Block 1J: refresh weather AmbientSignals (OpenWeatherMap + NOAA).
    Render cron resource cadence: every 2h, 6am-8pm CT."""
    return _run_ambient_refresh("weather")


@driver_system_bp.route("/cron/refresh-events", methods=["POST"])
def cron_refresh_events():
    """Block 1J: refresh events AmbientSignals (Ticketmaster + Google
    Calendar — credential-pending stub). Render cron resource cadence:
    every 4h after noon (12:00 / 16:00 / 20:00 CT)."""
    return _run_ambient_refresh("events")


@driver_system_bp.route("/cron/refresh-outages", methods=["POST"])
def cron_refresh_outages():
    """Block 1J: refresh outage AmbientSignals (CenterPoint scrape +
    Haiku-normalize). Render cron resource cadence: every 15 minutes."""
    return _run_ambient_refresh("outages")


@driver_system_bp.route("/cron/refresh-catering-pipeline", methods=["POST"])
def cron_refresh_catering_pipeline():
    """Block 1J: refresh catering-pipeline AmbientSignals (upcoming
    ScheduledEvent rows). Render cron resource cadence: 8am + 2pm CT."""
    return _run_ambient_refresh("catering_pipeline")


@driver_system_bp.route("/cron/refresh-vendor-status", methods=["POST"])
def cron_refresh_vendor_status():
    """Block 1J: refresh vendor-status AmbientSignals (Phase-3 stub —
    wiring proven, no source adapter yet). Render cron resource
    cadence: twice daily."""
    return _run_ambient_refresh("vendor_status")


@driver_system_bp.route("/cron/refresh-traffic", methods=["POST"])
def cron_refresh_traffic():
    """Block 1J: refresh traffic AmbientSignals (Google Maps —
    credential-pending stub). Render cron resource cadence: every 2h,
    business hours (8am-6pm CT)."""
    return _run_ambient_refresh("traffic")


@driver_system_bp.route("/cron/produce-ingest", methods=["POST"])
def cron_produce_ingest():
    """Produce vendor-email ingest, on demand. The INDEPENDENT hourly safety net
    behind the in-process 60s poller: recovers a dead poller thread, catches up any
    backlog the startup-baseline skipped, and alerts on staleness. Token-gated via
    CRON_TOKEN (the /cron/ prefix is EXEMPT in auth.py); fail-closed. The ingest
    sends NO vendor email - it only parses inbound price sheets into the page data."""
    import os
    _tok = os.getenv("CRON_TOKEN")
    if not _tok or _extract_cron_token() != _tok:
        abort(403)
    from app.services.produce_ingest import run_ingest_now
    return jsonify({"ok": True, **run_ingest_now()})
