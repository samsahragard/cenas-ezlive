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
            return redirect(url_for("driver.driver_login_get", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


def require_manager(fn: Callable):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = getattr(g, "current_user", None)
        if not user or user.permission_level not in MANAGER_ROLES:
            return redirect(url_for("keypad_auth.keypad_login_get", next=request.path))
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
@require_driver
def ez_market():
    driver = _current_driver()
    if not driver:
        return redirect(url_for("driver.driver_login_get"))
    today = date.today()
    db = SessionLocal()
    try:
        # "Available" = status='available', no driver assigned.
        # Premium gate (<= $50 for New tier) keeps tier-locked orders hidden.
        max_premium = None
        if (driver.current_tier or "new") == scoring.TIER_NEW:
            max_premium = 50.0
        avail_q = (
            db.query(Order)
            .filter(Order.status == "available")
            .filter(Order.delivery_date >= today.isoformat())
            .order_by(Order.delivery_window_start.asc().nullslast(),
                      Order.delivery_date.asc())
        )
        if max_premium is not None:
            avail_q = avail_q.filter(
                (Order.potential_payout == None) |  # noqa: E711
                (Order.potential_payout <= max_premium)
            )
        available = avail_q.limit(50).all()
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

        ctx = {
            "active": "ez_market",
            "driver": driver,
            "available": available,
            "competing": competing,
            "my_pending_reqs": my_pending_reqs,
            "my_active": my_active,
            "my_history": my_history,
            "stat_potential_today": _potential_today(db, driver.id, today),
            "stat_my_queue": _my_queue_count(db, driver.id),
            "stat_potential_week": _potential_week(db, driver.id, today),
            "current_tier": driver.current_tier or "new",
        }
        return render_template("ez_market.html", **ctx)
    finally:
        db.close()


@driver_system_bp.route("/ez-market/request/<int:delivery_id>", methods=["POST"])
@require_driver
def ez_market_request(delivery_id: int):
    driver = _current_driver()
    if not driver:
        return redirect(url_for("driver.driver_login_get"))
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
        return redirect(url_for("driver.driver_login_get"))
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
        if other_pending == 0:
            order = db.get(Order, req.delivery_id)
            if order and order.status == "requested":
                order.status = "available"
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
        return redirect(url_for("driver.driver_login_get"))
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
        return redirect(url_for("driver.driver_login_get"))
    db = SessionLocal()
    try:
        checks = (
            db.query(PayCheck)
            .filter(PayCheck.driver_id == driver.id)
            .order_by(desc(PayCheck.pay_period_end))
            .limit(24)
            .all()
        )
        return render_template("pay_history.html",
                               active="pay_history",
                               driver=driver,
                               checks=checks)
    finally:
        db.close()


@driver_system_bp.route("/pay-history/<int:delivery_id>/flag", methods=["POST"])
@require_driver
def pay_history_flag(delivery_id: int):
    driver = _current_driver()
    if not driver:
        return redirect(url_for("driver.driver_login_get"))
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

@driver_system_bp.route("/cron/no-show-sweep", methods=["POST"])
def cron_no_show_sweep():
    """Trigger no-show detection. Token-gated via CRON_TOKEN env var."""
    import os
    token = request.headers.get("X-Cron-Token") or request.args.get("token")
    if token != os.getenv("CRON_TOKEN"):
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
    """Trigger nightly driver-score recompute. Token-gated via CRON_TOKEN env var."""
    import os
    token = request.headers.get("X-Cron-Token") or request.args.get("token")
    if token != os.getenv("CRON_TOKEN"):
        abort(403)
    result = scoring.recompute_all_driver_scores()
    return jsonify({"ok": True, **result})
