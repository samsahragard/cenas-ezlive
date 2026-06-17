"""Driver-system routes: Ez Market / Ez Manage / My Profile / Pay History.

Pages defined per SPEC.md §4-7 and §16. Calls into:
  - app.services.delivery_lifecycle for state transitions
  - app.services.driver_scoring for the My Profile score breakdown

Auth gating:
  - Driver-facing pages (/ez-market, /my-profile, /pay-history) require
    a driver session (session['driver_id'] set by /driver/login).
  - Manager-facing pages (/ez-manage) require a keypad-authenticated User
    with one of the management dashboard roles.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from typing import Callable

from flask import (
    Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template,
    request, session, url_for,
)
from sqlalchemy import desc, or_

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
from app.services.delivery_pay_projection import projected_driver_pay

logger = logging.getLogger(__name__)

driver_system_bp = Blueprint("driver_system", __name__)

MANAGER_ROLES = {
    "partner",
    "corporate",
    "corporate_chef",
    "gm",
    "manager",
    "km",
    "assistant_km",
    "foh_manager",
    "expo",
}
APP_TZ = "America/Chicago"
SAME_DAY_ASSIGNED_STATUSES = ["approved", "picked_up", "en_route", "delivered"]


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


def _order_time_value(order: Order) -> datetime | None:
    if order.delivery_window_start:
        return order.delivery_window_start
    if not order.delivery_date or not order.deliver_at:
        return None
    match = re.search(r"(\d{1,2}:\d{2})\s*([AP]M)", order.deliver_at, re.IGNORECASE)
    if not match:
        return None
    text = f"{order.delivery_date} {match.group(1)} {match.group(2).upper()}"
    try:
        return datetime.strptime(text, "%Y-%m-%d %I:%M %p")
    except ValueError:
        return None


def _order_time_label(order: Order) -> str:
    return order.deliver_at or "time unknown"


def _gap_label(new_order: Order, existing_order: Order) -> str:
    new_time = _order_time_value(new_order)
    existing_time = _order_time_value(existing_order)
    if not new_time or not existing_time:
        return "same day"
    minutes = int(round(abs((new_time - existing_time).total_seconds()) / 60))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} apart"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"{hours} hour{'s' if hours != 1 else ''} apart"
    return f"{hours} hr {mins} min apart"


def _same_day_warning_for_driver(
    db,
    driver_id: int,
    new_order: Order,
    *,
    include_pending: bool,
    include_assigned: bool = True,
) -> dict:
    """Return advisory same-day warning data; never blocks assignment/request."""
    delivery_date = new_order.delivery_date
    if not delivery_date:
        return {"warning_needed": False}

    candidates: list[tuple[Order, str]] = []
    if include_assigned:
        assigned = (
            db.query(Order)
            .filter(Order.assigned_driver_id == driver_id)
            .filter(Order.delivery_date == delivery_date)
            .filter(Order.status.in_(SAME_DAY_ASSIGNED_STATUSES))
            .filter(Order.id != new_order.id)
            .order_by(Order.deliver_at.asc(), Order.id.asc())
            .all()
        )
        candidates.extend((o, "assigned delivery") for o in assigned)

    if include_pending:
        pending_reqs = (
            db.query(DeliveryRequest)
            .filter(DeliveryRequest.driver_id == driver_id)
            .filter(DeliveryRequest.status == "pending")
            .all()
        )
        for req in pending_reqs:
            if req.delivery_id == new_order.id:
                continue
            pending_order = db.get(Order, req.delivery_id)
            if pending_order and pending_order.delivery_date == delivery_date:
                candidates.append((pending_order, "pending request"))

    if not candidates:
        return {"warning_needed": False}

    new_time = _order_time_value(new_order)

    def sort_key(item: tuple[Order, str]) -> tuple[int, str, int]:
        existing, _ = item
        existing_time = _order_time_value(existing)
        if new_time and existing_time:
            return (
                int(abs((new_time - existing_time).total_seconds()) // 60),
                existing.deliver_at or "",
                existing.id,
            )
        return (999999, existing.deliver_at or "", existing.id)

    existing, existing_kind = sorted(candidates, key=sort_key)[0]
    gap = _gap_label(new_order, existing)
    return {
        "warning_needed": True,
        "stack_needed": True,
        "blocked": False,
        "feasible": True,
        "reason": "driver has another delivery on this date",
        "message": (
            f"Heads up: another {existing_kind} is already "
            f"on {delivery_date}. It is {gap}. Do you want to continue?"
        ),
        "gap_label": gap,
        "new_delivery_id": new_order.id,
        "new_external_order_id": new_order.external_order_id,
        "new_delivery_date": new_order.delivery_date,
        "new_deliver_at": _order_time_label(new_order),
        "existing_kind": existing_kind,
        "existing_delivery_id": existing.id,
        "existing_external_order_id": existing.external_order_id,
        "existing_delivery_date": existing.delivery_date,
        "existing_deliver_at": _order_time_label(existing),
        "existing_status": existing.status,
    }


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


def _local_today() -> date:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(APP_TZ)).date()
    except Exception:
        return date.today()


def _utc_bounds_for_local_day(day: date) -> tuple[datetime, datetime]:
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(APP_TZ)
        start = datetime(day.year, day.month, day.day, tzinfo=tz)
        end = start + timedelta(days=1)
        return (
            start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None),
        )
    except Exception:
        start = datetime.combine(day, datetime.min.time())
        return start, start + timedelta(days=1)


def _fmt_local_dt(dt: datetime | None) -> str:
    if not dt:
        return "time unknown"
    try:
        from zoneinfo import ZoneInfo

        local = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(APP_TZ))
    except Exception:
        local = dt
    return local.strftime("%I:%M %p").lstrip("0")


def _potential_today(db, driver_id: int, today: date) -> float:
    today_iso = today.isoformat()
    orders = (
        db.query(Order)
        .filter(Order.assigned_driver_id == driver_id)
        .filter(Order.status.in_(["approved", "picked_up", "en_route", "delivered"]))
        .filter(Order.delivery_date == today_iso)
        .all()
    )
    return round(sum(projected_driver_pay(o) for o in orders), 2)


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
    a number. Assumes the driver will track the delivery, so under-20-mile
    trips show the $35 minimum."""
    return projected_driver_pay(order)


# origin_store_id -> kitchen slug used by ezcater_miles.KITCHEN_ADDRESSES
_ORIGIN_TO_KITCHEN = {
    "store_1": "copperfield",
    "store_3": "copperfield",
    "store_2": "tomball",
    "store_4": "tomball",
}
_STORE_ORIGIN_IDS = {
    "copperfield": ("store_1", "store_3"),
    "tomball": ("store_2", "store_4"),
}


def _store_slug(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    collapsed = re.sub(r"[\s\-]+", "_", text)
    aliases = {
        "1": "copperfield",
        "ck1": "copperfield",
        "store_1": "copperfield",
        "store_3": "copperfield",
        "uno": "copperfield",
        "uno_mas": "copperfield",
        "copperfield": "copperfield",
        "2": "tomball",
        "ck2": "tomball",
        "store_2": "tomball",
        "store_4": "tomball",
        "dos": "tomball",
        "dos_mas": "tomball",
        "tomball": "tomball",
    }
    if collapsed in aliases:
        return aliases[collapsed]
    if any(token in text for token in ("#1", "15650", "fm 529", "fm529", "3733", "westheimer", "copperfield")):
        return "copperfield"
    if any(token in text for token in ("#2", "27727", "tomball", "2162", "spring stuebner")):
        return "tomball"
    return None


def _driver_store_slug(driver: Driver | None) -> str | None:
    if not driver:
        return None
    return _store_slug(driver.home_store_id) or _store_slug(driver.location)


def _order_store_slug(order: Order | None) -> str | None:
    if not order:
        return None
    for value in (
        order.origin_store_id,
        order.reported_store_id,
        order.pickup_kitchen,
        order.reported_store,
    ):
        slug = _store_slug(value)
        if slug:
            return slug
    return None


def _order_store_filter(store_slug: str):
    origins = _STORE_ORIGIN_IDS.get(store_slug, ())
    identifiers = (*origins, store_slug)
    reported_needles = {
        "copperfield": ("copperfield", "15650", "fm 529", "fm529", "3733", "westheimer", "#1", "#3"),
        "tomball": ("tomball", "27727", "2162", "spring stuebner", "#2", "#4"),
    }.get(store_slug, ())
    return or_(
        Order.origin_store_id.in_(identifiers),
        Order.reported_store_id.in_(identifiers),
        Order.pickup_kitchen.in_(identifiers),
        *[Order.reported_store.ilike(f"%{needle}%") for needle in reported_needles],
    )


def _order_matches_store(order: Order | None, store_slug: str | None) -> bool:
    return bool(store_slug and _order_store_slug(order) == store_slug)


def _driver_can_see_order(driver: Driver | None, order: Order | None) -> bool:
    return bool(driver and order and _order_matches_store(order, _driver_store_slug(driver)))

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
    orders = (
        db.query(Order)
        .filter(Order.assigned_driver_id == driver_id)
        .filter(Order.status.in_(["approved", "picked_up", "en_route", "delivered"]))
        .filter(Order.delivery_date >= period_start.isoformat())
        .filter(Order.delivery_date <= period_end.isoformat())
        .all()
    )
    return round(sum(projected_driver_pay(o) for o in orders), 2)


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
        # of status (today + future, excluding cancelled). Driver views are
        # scoped to their home store; manager/partner read-only views stay
        # cross-store. The legacy status='available' bid-pool gate + per-tier
        # premium cap are dropped here; the future request flow layers on top.
        avail_q = (
            db.query(Order)
            .filter(Order.delivery_date >= today.isoformat())
            .filter(Order.status != "cancelled")
            .order_by(Order.delivery_date.asc(),
                      Order.deliver_at.asc().nullslast())
        )
        driver_store = _driver_store_slug(driver)
        if driver:
            if driver_store:
                avail_q = avail_q.filter(_order_store_filter(driver_store))
                available = [
                    o for o in avail_q.limit(200).all()
                    if _order_matches_store(o, driver_store)
                ]
            else:
                available = []
        else:
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
            if driver_store:
                my_pending_reqs = [
                    r for r in my_pending_reqs
                    if _order_matches_store(db.get(Order, r.delivery_id), driver_store)
                ]
            else:
                my_pending_reqs = []
            my_active = (
                db.query(Order)
                .filter(Order.assigned_driver_id == driver.id)
                .filter(Order.status.in_(["approved", "picked_up", "en_route"]))
                .all()
            )
            my_active = [o for o in my_active if _order_matches_store(o, driver_store)]
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
            my_history = [o for o in my_history if _order_matches_store(o, driver_store)]

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
        from app.services.ezcater_management_presenter import compact_order_card
        available_groups = group_orders_by_date(available)
        projected_payouts = {
            o.id: _project_payout(o)
            for o in [*available, *my_active, *my_history]
        }

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
            "compact_order_card": compact_order_card,
        }
        return render_template("ez_market.html", **ctx)
    finally:
        db.close()


@driver_system_bp.route("/ez-market2", methods=["GET"])
def ez_market_public_demo():
    """Public read-only copy of Ez Market for driver recruiting.

    Shows the same live upcoming cross-store market information, but does not
    require login and does not expose any functional request/queue/pay actions.
    """
    today = date.today()
    db = SessionLocal()
    try:
        available = (
            db.query(Order)
            .filter(Order.delivery_date >= today.isoformat())
            .filter(Order.status != "cancelled")
            .order_by(Order.delivery_date.asc(), Order.deliver_at.asc().nullslast())
            .limit(200)
            .all()
        )

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

        from app.services.orders_query import group_orders_by_date
        from app.services.ezcater_management_presenter import compact_order_card

        available_groups = group_orders_by_date(available)
        projected_payouts = {o.id: _project_payout(o) for o in available}

        return render_template(
            "ez_market.html",
            active="ez_market",
            driver=None,
            viewer_is_driver=True,
            viewer_name="there",
            available=available,
            available_groups=available_groups,
            projected_payouts=projected_payouts,
            competing=competing,
            my_pending_reqs=[],
            my_active=[],
            my_history=[],
            unread_notifications=[],
            stat_potential_today=None,
            stat_my_queue=None,
            stat_potential_week=None,
            current_tier=None,
            compact_order_card=compact_order_card,
            public_demo=True,
        )
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
        if not _driver_can_see_order(driver, order):
            flash("That delivery belongs to another store.", "error")
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


@driver_system_bp.route("/ez-market/request-warning", methods=["GET"])
@require_driver
def ez_market_request_warning():
    driver = _current_driver()
    if not driver:
        return jsonify({"ok": False, "error": "not signed in"}), 401
    try:
        delivery_id = int(request.args.get("delivery_id", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad params"}), 400
    db = SessionLocal()
    try:
        order = db.get(Order, delivery_id)
        if not order:
            return jsonify({"ok": False, "error": "delivery not found"}), 404
        if not _driver_can_see_order(driver, order):
            return jsonify({"ok": False, "error": "delivery belongs to another store"}), 403
        warning = _same_day_warning_for_driver(
            db,
            driver.id,
            order,
            include_pending=True,
            include_assigned=True,
        )
        return jsonify({"ok": True, **warning})
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
    today = _local_today()
    today_start, tomorrow_start = _utc_bounds_for_local_day(today)
    db = SessionLocal()
    try:
        from app.services.ezcater_management_presenter import compact_order_card
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
            g_["payout"] = _project_payout(g_["order"])
            g_["rows"].sort(key=lambda row: (
                tier_rank.get(row["driver"].current_tier or "new", 9),
                -(row["driver"].current_score or 0),
            ))
            if g_["rows"]:
                g_["rows"][0]["recommended"] = True

        approved_orders = (
            db.query(Order)
            .filter(Order.status.in_(["approved", "picked_up", "en_route", "delivered"]))
            .filter(Order.approved_at.isnot(None))
            .filter(Order.approved_at >= today_start)
            .filter(Order.approved_at < tomorrow_start)
            .order_by(Order.approved_at.desc())
            .all()
        )
        from app.models import DriverAssignmentJob
        approved_rows = []
        for o in approved_orders:
            approved_driver = db.get(Driver, o.assigned_driver_id) if o.assigned_driver_id else None
            approved_by = db.get(User, o.approved_by_user_id) if o.approved_by_user_id else None
            job = None
            if o.external_order_id:
                job = (
                    db.query(DriverAssignmentJob)
                    .filter(DriverAssignmentJob.order_id == o.external_order_id)
                    .order_by(DriverAssignmentJob.created_at.desc())
                    .first()
                )
            approved_rows.append({
                "order": o,
                "driver": approved_driver,
                "approved_by": approved_by,
                "assignment_job": job,
                "approved_at_label": _fmt_local_dt(o.approved_at),
                "payout": _project_payout(o),
            })
        ctx = {
            "active": "ez_manage",
            "groups": list(groups.values()),
            "pending_count": len(groups),
            "approved_today_count": len(approved_rows),
            "approved_rows": approved_rows,
            "compact_order_card": compact_order_card,
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
    dispatch_payload = None
    try:
        req = db.get(DeliveryRequest, request_id)
        if not req:
            abort(404)
        order = db.get(Order, req.delivery_id)
        driver = db.get(Driver, req.driver_id)
        if not order or not driver:
            abort(404)
        try:
            from app.services.driver_assignment_jobs import (
                AssignmentAlreadyInProgress,
                create_assignment_job,
                resolve_ezcater_driver_name,
            )

            current_ez_driver = order.ezcater_driver_name
            ez_driver_name = resolve_ezcater_driver_name(db, driver, order)
            lifecycle.approve_request(db, order, driver, g.current_user.id)
            order.assigned_driver = driver.name
            order.ezcater_driver_name = ez_driver_name
            assignment_note = ""
            if order.external_order_id:
                try:
                    job = create_assignment_job(
                        db,
                        order_id=order.external_order_id,
                        current_driver=current_ez_driver,
                        new_driver=ez_driver_name,
                    )
                    dispatch_payload = {
                        "job_id": job.job_id,
                        "order_id": job.order_id,
                        "current_driver": job.current_driver,
                        "new_driver": job.new_driver,
                    }
                    assignment_note = " pwck assignment queued."
                except AssignmentAlreadyInProgress:
                    assignment_note = " pwck assignment already in progress."
            else:
                assignment_note = " No ezCater assignment queued: missing external order id."
            db.commit()
            if dispatch_payload:
                from app.services.driver_assignment_jobs import wake_assignment_gateway

                wake_assignment_gateway(**dispatch_payload)
            flash(
                f"Approved {ez_driver_name} for {order.external_order_id or order.id}."
                f"{assignment_note}",
                "ok",
            )
        except lifecycle.IllegalTransition as e:
            db.rollback()
            flash(f"Couldn't approve: {e}", "error")
        except Exception as e:
            db.rollback()
            logger.exception("ez_manage_approve failed for request %s", request_id)
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
    """Ajax endpoint for the same-day reminder modal."""
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
        warning = _same_day_warning_for_driver(
            db,
            driver.id,
            new_o,
            include_pending=False,
            include_assigned=True,
        )
        if not warning.get("warning_needed"):
            return jsonify({"ok": True, "stack_needed": False})
        return jsonify({
            "ok": True,
            "stack_needed": True,
            **warning,
            "active_delivery_id": warning.get("existing_delivery_id"),
            "active_external_order_id": warning.get("existing_external_order_id"),
            "active_delivery_date": warning.get("existing_delivery_date"),
        })
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


def _driver_can_view_route(db, driver: Driver, order: Order) -> bool:
    if order.assigned_driver_id == driver.id:
        return True
    from app.services.ezcater_payroll import normalize_driver_name

    if normalize_driver_name(order.ezcater_driver_name) == normalize_driver_name(driver.name):
        return True
    from app.models import EzcaterTrackingPoint

    return (
        db.query(EzcaterTrackingPoint.id)
        .filter(EzcaterTrackingPoint.order_id == order.id)
        .filter(EzcaterTrackingPoint.driver_id == driver.id)
        .first()
        is not None
    )


@driver_system_bp.route("/pay-history/route/<int:delivery_id>", methods=["GET"])
@require_driver
def pay_history_route(delivery_id: int):
    driver = _current_driver()
    if not driver:
        return redirect(url_for("driver.driver_login"))
    from app.services.ezcater_route_history import route_summary_for_order

    db = SessionLocal()
    try:
        order = db.get(Order, delivery_id)
        if not order or not _driver_can_view_route(db, driver, order):
            abort(404)
        summary = route_summary_for_order(db, delivery_id)
        return render_template(
            "ezcater_route_playback.html",
            active="pay_history",
            order=order,
            driver=driver,
            summary=summary,
            route_track_url=url_for("driver_system.pay_history_route_track", delivery_id=delivery_id),
            back_url=url_for("driver_system.pay_history"),
            location_labels={},
            viewer="driver",
        )
    finally:
        db.close()


@driver_system_bp.route("/pay-history/route/<int:delivery_id>/track.json", methods=["GET"])
@require_driver
def pay_history_route_track(delivery_id: int):
    driver = _current_driver()
    if not driver:
        return jsonify({"error": "not signed in"}), 401
    from app.services.ezcater_route_history import route_point_dicts, route_summary_for_order

    db = SessionLocal()
    try:
        order = db.get(Order, delivery_id)
        if not order or not _driver_can_view_route(db, driver, order):
            return jsonify({"error": "not found"}), 404
        summary = route_summary_for_order(db, delivery_id)
        return jsonify({
            "order_id": delivery_id,
            "order_number": order.external_order_id,
            "tracking_uuid": order.delivery_tracking_id,
            "summary": {
                "point_count": summary.point_count,
                "distance_miles": round(summary.distance_miles, 3),
                "extra_miles_over_20": round(summary.extra_miles_over_20, 3),
                "duration_minutes": summary.duration_minutes,
                "started_at": summary.started_at.isoformat() + "Z" if summary.started_at else None,
                "ended_at": summary.ended_at.isoformat() + "Z" if summary.ended_at else None,
                "driver_id": summary.driver_id,
                "driver_name": summary.driver_name,
                "status_key": summary.status_key,
            },
            "points": route_point_dicts(db, delivery_id),
        })
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
    """Kick the Toast snapshot refresh (Sam #2845). Token-gated via CRON_TOKEN.
    The bulk pull is HEAVY (Toast calls per employee), so it runs in a daemon
    thread and this returns IMMEDIATELY (202) -- a synchronous full sync can blow
    the gateway timeout -> 502 and tie up a worker. Lets samai wire a Render cron
    as belt-and-suspenders + nudge a refresh on demand; the in-app poller does
    this automatically every ~15 min regardless. snapshots_now lets a caller
    confirm the table is populating across calls. Optional ?store=tomball|
    copperfield. Optional ?profiles=inline creates/links Toast-only app profiles
    before returning; otherwise profile reconciliation runs in the background
    sync thread. Accepts Authorization: Bearer, X-Cron-Token, or ?token= ."""
    import os
    import threading
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    from app.services.toast_sync import sync_toast_snapshots, snapshot_status
    store = (request.args.get("store") or "").strip() or None
    profile_mode = (request.args.get("profiles") or "").strip().lower()
    profiles = {"queued": True}
    reconcile_in_thread = True
    if profile_mode == "inline":
        from app.services.toast_employee_profiles import reconcile_toast_employee_profiles
        profiles = reconcile_toast_employee_profiles(only_store=store)
        reconcile_in_thread = False
    threading.Thread(target=sync_toast_snapshots,
                     kwargs={"only_store": store, "reconcile_profiles": reconcile_in_thread},
                     name="toast-sync-cron", daemon=True).start()
    return jsonify({"ok": True, "started": True, "store": store or "all",
                    "profiles": profiles, "status": snapshot_status()}), 202


@driver_system_bp.route("/cron/import-schedules", methods=["POST"])
def cron_import_schedules():
    """One-time historical schedule import (Sam #2872). Token-gated via CRON_TOKEN.
    POST body {"records":[{iso_date,start,end,job,store,name}, ...]} -> loaded via
    schedule_import.import_historical (DB-only, fast). Idempotent: re-running
    replaces the import-created weeks and NEVER clobbers a manager-made schedule.
    Returns the import summary. Accepts Authorization: Bearer / X-Cron-Token / ?token=."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    payload = request.get_json(silent=True) or {}
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        return jsonify({"ok": False, "error": 'POST {"records":[...]} required'}), 400
    from app.services.schedule_import import import_historical
    db = SessionLocal()
    try:
        summary = import_historical(records, db)
        return jsonify({"ok": True, **summary}), 200
    finally:
        db.close()


@driver_system_bp.route("/cron/schedule-peek", methods=["GET"])
def cron_schedule_peek():
    """Diagnostic (Sam #2872): token-gated read of schedules + shift counts for a
    store, so the import can be verified without a manager login. ?store=dos|uno|
    tomball|copperfield. Optional ?week=YYYY-MM-DD runs the EXACT board query
    (store_key + week_start) so we can confirm what the week-view would see."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    from datetime import date as _date
    from app.models import Schedule, Shift
    store = (request.args.get("store") or "tomball").strip().lower()
    store = {"dos": "tomball", "uno": "copperfield"}.get(store, store)
    db = SessionLocal()
    try:
        rows = (db.query(Schedule).filter_by(store_key=store)
                  .order_by(Schedule.week_start).all())
        weeks = []
        for s in rows:
            n = db.query(Shift).filter_by(schedule_id=s.id).count()
            weeks.append({"week_start": s.week_start.isoformat(), "status": s.status,
                          "created_by": s.created_by, "shifts": n})
        out = {"ok": True, "store": store, "schedule_count": len(rows), "weeks": weeks}
        wk = (request.args.get("week") or "").strip()
        if wk:
            try:
                ws = _date.fromisoformat(wk)
                sch = db.query(Schedule).filter_by(store_key=store, week_start=ws).first()
                out["board_query"] = {
                    "week": wk, "found": sch is not None,
                    "shifts": (db.query(Shift).filter_by(schedule_id=sch.id).count() if sch else 0),
                }
            except Exception as ex:
                out["board_query"] = {"week": wk, "error": str(ex)}
        return jsonify(out), 200
    finally:
        db.close()


@driver_system_bp.route("/cron/roster-peek", methods=["GET"])
def cron_roster_peek():
    """Diagnostic (Sam #2890): token-gated read of the app roster + Toast links, so
    the Link reconciliation can be planned without a manager login. Each Employee:
    name, active, stores, and confirmed CenaToastLink(s) -> spot system accounts,
    duplicates, and bad links."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    from app.models import Employee, EmployeeStoreAssignment, CenaToastLink
    db = SessionLocal()
    try:
        stores = {}
        for a in db.query(EmployeeStoreAssignment).all():
            stores.setdefault(a.employee_id, []).append(a.store_key)
        links = {}
        for l in db.query(CenaToastLink).all():
            links.setdefault(l.cena_employee_id, []).append(
                {"store": l.store_key, "toast_id": l.toast_id, "toast_name": l.toast_name})
        out = []
        for e in db.query(Employee).order_by(Employee.full_name).all():
            out.append({"id": e.id, "name": e.full_name, "active": e.active,
                        "stores": stores.get(e.id, []), "links": links.get(e.id, [])})
        return jsonify({"ok": True, "count": len(out), "employees": out}), 200
    finally:
        db.close()


@driver_system_bp.route("/cron/roster-action", methods=["POST"])
def cron_roster_action():
    """Token-gated roster reconciliation actions (Sam #2890). REVERSIBLE ONLY:
    'deactivate' sets Employee.active=False (re-addable; never hard-deletes);
    'unlink' deletes a CenaToastLink row (re-linkable). Body: {action, ...}."""
    import os
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    from app.models import Employee, CenaToastLink
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip()
    db = SessionLocal()
    try:
        if action == "deactivate":
            e = db.query(Employee).filter_by(id=body.get("employee_id")).first()
            if not e:
                return jsonify({"ok": False, "error": "no such employee"}), 404
            e.active = False
            try:
                e.session_version = (e.session_version or 0) + 1
            except Exception:
                pass
            db.commit()
            return jsonify({"ok": True, "deactivated": {"id": e.id, "name": e.full_name}}), 200
        if action == "unlink":
            q = db.query(CenaToastLink)
            if body.get("link_id"):
                q = q.filter_by(id=body["link_id"])
            elif body.get("cena_employee_id"):
                q = q.filter_by(cena_employee_id=body["cena_employee_id"])
                if body.get("store"):
                    q = q.filter_by(store_key=body["store"])
            else:
                return jsonify({"ok": False, "error": "link_id or cena_employee_id required"}), 400
            rows = q.all()
            removed = [{"id": r.id, "toast_name": r.toast_name} for r in rows]
            for r in rows:
                db.delete(r)
            db.commit()
            return jsonify({"ok": True, "unlinked": removed}), 200
        return jsonify({"ok": False, "error": "action must be deactivate|unlink"}), 400
    finally:
        db.close()


@driver_system_bp.route("/cron/perf-push", methods=["POST"])
def cron_perf_push():
    """Phase 3 (Sam #2938/#2941): token-gated receiver for the CK-local perf DB
    push. CK (Mini_IT13 = source of truth) POSTs SANITIZED per-period rows for one
    employee; we upsert PerfPeriodCache. Stores ONLY known keys -- employee-visible
    'service' -> service_json, INTERNAL 'attribution' -> attribution_json (a column
    the employee payload NEVER reads). No sales field is accepted or stored."""
    import os
    from datetime import datetime as _dt
    if _extract_cron_token() != os.getenv("CRON_TOKEN"):
        abort(403)
    from app.models import PerfPeriodCache
    body = request.get_json(silent=True) or {}
    emp = body.get("employee") or {}
    cid = emp.get("cena_employee_id")
    if not cid:
        return jsonify({"ok": False, "error": "employee.cena_employee_id required"}), 400
    db = SessionLocal()
    written = 0
    try:
        for p in (body.get("periods") or []):
            per = (p.get("period") or "").strip()
            if not per:
                continue
            row = (db.query(PerfPeriodCache)
                     .filter_by(cena_employee_id=cid, period=per).first())
            if row is None:
                row = PerfPeriodCache(cena_employee_id=cid, period=per)
                db.add(row)
            row.toast_id = emp.get("toast_id")
            row.store_key = emp.get("store_key")
            row.period_start = p.get("period_start")
            row.period_end = p.get("period_end")
            row.total_hours = float(p.get("total_hours") or 0)
            row.reg_hours = float(p.get("reg_hours") or 0)
            row.ot_hours = float(p.get("ot_hours") or 0)
            row.base_pay = float(p.get("base_pay") or 0)
            row.tips = float(p.get("tips") or 0)
            svc = p.get("service")
            row.service_json = svc if isinstance(svc, dict) else {}
            attr = p.get("attribution")
            row.attribution_json = attr if isinstance(attr, dict) else None
            row.computed_at = p.get("computed_at")
            row.synced_at = _dt.utcnow()
            written += 1
        # per-shift (Sam #2938 / samai #2954) -- same sanitize discipline; attribution -> internal
        from app.models import PerfShiftCache
        shift_written = 0
        shifts = body.get("shifts") or []
        if shifts:
            db.query(PerfShiftCache).filter_by(cena_employee_id=cid).delete()
            for sh in shifts:
                row = PerfShiftCache(cena_employee_id=cid, clock_in=sh.get("clock_in"))
                row.toast_id = emp.get("toast_id")
                row.store_key = emp.get("store_key")
                row.business_date = sh.get("business_date")
                row.clock_out = sh.get("clock_out")
                row.reg_hours = float(sh.get("reg_hours") or 0)
                row.ot_hours = float(sh.get("ot_hours") or 0)
                row.total_hours = float(sh.get("total_hours") or 0)
                row.base_pay = float(sh.get("base_pay") or 0)
                row.tips = float(sh.get("tips") or 0)
                row.tips_declared = bool(sh.get("tips_declared", True))   # N4
                row.needs_review = bool(sh.get("needs_review", False))    # N5 (employee-visible flag)
                row.review_reason = sh.get("review_reason")
                attr = sh.get("attribution")
                row.attribution_json = attr if isinstance(attr, dict) else None
                db.add(row)
                shift_written += 1
        db.commit()
        return jsonify({"ok": True, "cena_employee_id": cid,
                        "periods_written": written, "shifts_written": shift_written}), 200
    finally:
        db.close()


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
