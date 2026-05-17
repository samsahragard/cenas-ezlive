"""Delivery state-machine helpers.

Each public function applies one state transition per SPEC.md §11. They
write the appropriate Order.status, stamp the relevant timestamps, fire
side effects (potential_payout snapshot, lifetime_delivery_count++,
Cancellation row, etc.), and raise on illegal transitions.

The helpers are intentionally side-effect-aware but transport-naive —
they don't decide who's authorized or HOW the trigger arrived (HTTP
POST vs cron). The route handlers + cron jobs that call them own
permission checks. Tests / scripts can use them directly with a Session.

State machine (Order.status values):
    new          legacy ingest default; not part of the driver-bid flow
    available    open for bidding (driver-bid orders enter here)
    requested    at least one driver has requested
    approved     manager picked a driver
    picked_up    driver marked pickup at the kitchen
    en_route     driver left for the customer
    delivered    photo proof uploaded, marked delivered
    cancelled    cancelled (by manager 'decline-all' or driver cancel after approval)
    no_show      cron-detected miss; triggers Driver.status='terminated'

Transition map intentionally minimal — anything not listed raises.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import (
    Cancellation,
    DeliveryRequest,
    Driver,
    DriverNotification,
    Order,
)
from app.services.ezcater_payroll import compute_one as compute_pay_one

logger = logging.getLogger(__name__)


# ---- helpers ----

class IllegalTransition(Exception):
    """Raised when the requested transition isn't allowed from the current status."""


_VALID_FROM = {
    # Note: 'processed' was a pre-bid-system job-completion marker
    # written by the ingest pipeline. Decommissioned in migration 24
    # / commit 992f986 (Sam #1646 + samai #1645) — ingest now writes
    # 'available' directly. If you see 'processed' in current code,
    # it's either historical context or a regression worth fixing.
    "open_for_bidding":        {"new", None, ""},         # ingest → available
    "request":                 {"available", "requested"},
    "approve":                 {"available", "requested"},
    "back_to_bidding":         {"requested"},
    "decline_all":             {"requested"},
    "mark_picked_up":          {"approved"},
    "mark_en_route":           {"picked_up"},
    "mark_delivered":          {"en_route", "picked_up"},
    "driver_cancel":           {"approved"},
    "no_show":                 {"approved"},
}


def _check(transition: str, current: str | None) -> None:
    allowed = _VALID_FROM.get(transition)
    if allowed is None:
        raise IllegalTransition(f"unknown transition '{transition}'")
    if current not in allowed:
        raise IllegalTransition(
            f"can't {transition} from status={current!r} (allowed: {sorted(s for s in allowed if s)})"
        )


def _snapshot_potential_payout(order: Order) -> float | None:
    """Compute potential_payout once and store it on the Order. Idempotent —
    if already populated, returns the stored value without recomputing.

    Uses the existing ezcater_payroll.compute_one so the formula stays
    in one place. Returns None if we don't yet have enough data to compute
    (e.g., the order hasn't been ingested far enough for tracking_status)."""
    if order.potential_payout is not None:
        return order.potential_payout
    try:
        pay = compute_pay_one(order, five_star=False)
        order.potential_payout = pay.total
        return pay.total
    except Exception:
        logger.exception("potential_payout snapshot failed for order id=%s", order.id)
        return None


# ---- transitions ----

def open_for_bidding(db: Session, order: Order) -> None:
    """Move a freshly-ingested order into the bid pool. Called from the
    webhook ingest pipeline after the order has a valid delivery_window."""
    _check("open_for_bidding", order.status)
    order.status = "available"
    _snapshot_potential_payout(order)


def request_delivery(db: Session, order: Order, driver: Driver) -> DeliveryRequest:
    """Driver requests this delivery. Creates a DeliveryRequest row and
    flips Order.status to 'requested' if not already there.
    Idempotent on (delivery, driver) per the UniqueConstraint — re-requests
    raise via DB rather than silently double-counting."""
    _check("request", order.status)
    req = DeliveryRequest(
        delivery_id=order.id,
        driver_id=driver.id,
        requested_at=datetime.utcnow(),
        status="pending",
    )
    db.add(req)
    order.status = "requested"
    return req


# ---- Issue B (Sam #1591 + samai #1599) — same-day conflict helpers ----
#
# Per samai #1599 spec:
#   - time-only conflict (location dropped — drivers do back-to-back
#     from same store all the time)
#   - "different date → no conflict" gate handled upstream by checking
#     order.delivery_window_start.date() vs the pending request's date
#   - first-of-day auto-allows (caller skips conflict check on count=0)


def _order_time_window(order: Order) -> tuple[datetime, datetime] | None:
    """Best-effort (start, end) for an order's delivery window.
    Returns None if the order has no usable time signal — caller treats
    as 'no conflict possible' (degrades gracefully on legacy rows).
    """
    if order.delivery_window_start and order.delivery_window_end:
        return order.delivery_window_start, order.delivery_window_end
    if order.delivery_window_start:
        # 15-min window centered on the start time (samai #1599 default
        # when only the single time is present).
        s = order.delivery_window_start
        return s - timedelta(minutes=7, seconds=30), s + timedelta(minutes=7, seconds=30)
    return None


def find_conflicting_request(
    db: Session, driver_id: int, new_order: Order
) -> DeliveryRequest | None:
    """If any of `driver_id`'s pending requests has a time window that
    overlaps `new_order`'s window, return the first such DeliveryRequest.
    Else None.
    """
    new_window = _order_time_window(new_order)
    if new_window is None:
        return None
    new_start, new_end = new_window
    pending = (
        db.query(DeliveryRequest)
        .filter(DeliveryRequest.driver_id == driver_id)
        .filter(DeliveryRequest.status == "pending")
        .all()
    )
    for r in pending:
        if r.delivery_id == new_order.id:
            continue
        existing = db.get(Order, r.delivery_id)
        if existing is None:
            continue
        existing_window = _order_time_window(existing)
        if existing_window is None:
            continue
        es, ee = existing_window
        if es < new_end and new_start < ee:
            return r
    return None


def approve_request(
    db: Session, order: Order, driver: Driver, decided_by_user_id: int | None
) -> None:
    """Manager approves a driver's request. Stamps the order with the
    approved driver, approver, and timestamp; sets the matching
    DeliveryRequest row to 'approved' and any other open requests on this
    order to 'declined'."""
    _check("approve", order.status)
    now = datetime.utcnow()
    order.status = "approved"
    order.assigned_driver_id = driver.id
    order.approved_by_user_id = decided_by_user_id
    order.approved_at = now
    _snapshot_potential_payout(order)
    # Mark the winning request 'approved' and decline siblings on this order.
    sib_reqs = (
        db.query(DeliveryRequest)
        .filter(DeliveryRequest.delivery_id == order.id)
        .filter(DeliveryRequest.status == "pending")
        .all()
    )
    for r in sib_reqs:
        if r.driver_id == driver.id:
            r.status = "approved"
        else:
            r.status = "declined"
            # Issue B / samai #1599: notify the losing driver inline at
            # /ez-market. Persisted (not flash) so it survives logout.
            db.add(DriverNotification(
                driver_id=r.driver_id,
                kind="order_taken_by_other",
                message=(f"Another driver was given order "
                         f"#{order.id} ({order.client or 'unnamed'}, "
                         f"{order.delivery_date or 'no date'})."),
                related_delivery_id=order.id,
            ))
        r.decided_at = now
        r.decided_by_user_id = decided_by_user_id


def back_to_bidding(db: Session, order: Order, decided_by_user_id: int | None) -> None:
    """Manager re-opens an order; all pending requests get declined and
    the order returns to the bid pool."""
    _check("back_to_bidding", order.status)
    now = datetime.utcnow()
    pending = (
        db.query(DeliveryRequest)
        .filter(DeliveryRequest.delivery_id == order.id)
        .filter(DeliveryRequest.status == "pending")
        .all()
    )
    for r in pending:
        r.status = "declined"
        r.decided_at = now
        r.decided_by_user_id = decided_by_user_id
    order.status = "available"


def decline_all(db: Session, order: Order, decided_by_user_id: int | None) -> None:
    """Manager declines all requesters and cancels the order."""
    _check("decline_all", order.status)
    now = datetime.utcnow()
    pending = (
        db.query(DeliveryRequest)
        .filter(DeliveryRequest.delivery_id == order.id)
        .filter(DeliveryRequest.status == "pending")
        .all()
    )
    for r in pending:
        r.status = "declined"
        r.decided_at = now
        r.decided_by_user_id = decided_by_user_id
    order.status = "cancelled"


def mark_picked_up(db: Session, order: Order) -> None:
    _check("mark_picked_up", order.status)
    order.status = "picked_up"
    order.pickup_actual_at = datetime.utcnow()


def mark_en_route(db: Session, order: Order) -> None:
    _check("mark_en_route", order.status)
    order.status = "en_route"
    order.en_route_at = datetime.utcnow()


def mark_delivered(
    db: Session,
    order: Order,
    setup_photo_url: str | None = None,
) -> None:
    """Driver marks delivery complete. Increments driver lifetime count.
    setup_photo_url is optional but counted in the photo-proof scoring metric."""
    _check("mark_delivered", order.status)
    now = datetime.utcnow()
    order.status = "delivered"
    order.delivered_actual_at = now
    if setup_photo_url:
        order.setup_photo_url = setup_photo_url
        order.setup_photo_uploaded_at = now
    if order.assigned_driver_id:
        driver = db.get(Driver, order.assigned_driver_id)
        if driver:
            driver.lifetime_delivery_count = (driver.lifetime_delivery_count or 0) + 1


def driver_cancel(db: Session, order: Order, reason: str | None) -> Cancellation:
    """Driver cancels an approved delivery. Logs a Cancellation row (drives
    the 30-day / 90-day threshold rules) and returns the order to the bid pool."""
    _check("driver_cancel", order.status)
    if order.assigned_driver_id is None:
        raise IllegalTransition("driver_cancel requires assigned_driver_id")
    cx = Cancellation(
        delivery_id=order.id,
        driver_id=order.assigned_driver_id,
        cancelled_at=datetime.utcnow(),
        reason=reason,
        cancelled_by="driver",
    )
    db.add(cx)
    order.status = "available"
    order.assigned_driver_id = None
    order.approved_at = None
    order.approved_by_user_id = None
    return cx


# ---- no-show detection ----

NO_SHOW_GRACE_MINUTES = 10


def detect_no_shows(db: Session) -> list[Order]:
    """Cron entry point. Finds approved orders whose pickup time has passed
    by NO_SHOW_GRACE_MINUTES without a pickup_actual_at, flips them to
    no_show, and terminates the assigned driver. Returns the list of
    flagged orders."""
    cutoff = datetime.utcnow() - timedelta(minutes=NO_SHOW_GRACE_MINUTES)
    # Order.deliver_at is a String("HH:MM AM/PM") in legacy data; we use
    # delivery_window_start (DateTime) as the canonical pickup-due time.
    candidates = (
        db.query(Order)
        .filter(Order.status == "approved")
        .filter(Order.delivery_window_start.isnot(None))
        .filter(Order.delivery_window_start < cutoff)
        .filter(Order.pickup_actual_at.is_(None))
        .all()
    )
    flagged: list[Order] = []
    for o in candidates:
        try:
            _check("no_show", o.status)
        except IllegalTransition:
            continue
        o.status = "no_show"
        if o.assigned_driver_id:
            driver = db.get(Driver, o.assigned_driver_id)
            if driver and driver.status == "active":
                driver.status = "terminated"
                driver.terminated_at = datetime.utcnow()
                driver.termination_reason = f"no_show on delivery id={o.id}"
                logger.info(
                    "no_show termination driver_id=%s delivery_id=%s",
                    driver.id, o.id,
                )
        flagged.append(o)
    return flagged
