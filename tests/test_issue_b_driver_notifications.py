"""Issue B (Sam #1591 + samai #1599) — DriverNotification + same-day
time-conflict regression tests for the ez-market request flow.

Covers:
  - approve_request creates DriverNotification rows for the approved driver
    and declined siblings
  - find_conflicting_request detects time-window overlap
  - find_conflicting_request returns None when windows disjoint
  - find_conflicting_request handles legacy orders without time data
  - _order_time_window uses pickup window when both start+end present
  - _order_time_window centers a 15-min window on start when only start
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models import (
    DeliveryRequest,
    Driver,
    DriverNotification,
    Order,
)
from app.services import delivery_lifecycle as lifecycle


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _make_driver(db_session, name: str, location: str = "tomball") -> Driver:
    d = Driver(name=name, location=location,
               lifetime_delivery_count=0, active=True)
    db_session.add(d)
    db_session.flush()
    return d


def _make_order(db_session, ext_id: str, *,
                start: datetime | None = None,
                end: datetime | None = None,
                status: str = "available") -> Order:
    o = Order(external_order_id=ext_id, status=status,
              delivery_date="2026-05-15", deliver_at="12:00 PM",
              delivery_window_start=start, delivery_window_end=end,
              client="Test Client")
    db_session.add(o)
    db_session.flush()
    return o


# ----------------------------------------------------------------------
# approve_request → DriverNotification rows
# ----------------------------------------------------------------------

def test_approve_request_notifies_losing_drivers(db_session):
    """When manager approves Driver A, declined siblings (B, C) each
    get a 'order_taken_by_other' DriverNotification row, and A gets an
    approval notification."""
    a = _make_driver(db_session, "winner alpha")
    b = _make_driver(db_session, "loser beta")
    c = _make_driver(db_session, "loser gamma")
    o = _make_order(db_session, "TEST-NOTIFY-1", status="requested")

    for d in (a, b, c):
        db_session.add(DeliveryRequest(
            delivery_id=o.id, driver_id=d.id, status="pending",
        ))
    db_session.flush()

    lifecycle.approve_request(db_session, o, a, decided_by_user_id=None)
    db_session.flush()

    notifs = (
        db_session.query(DriverNotification)
        .filter(DriverNotification.related_delivery_id == o.id)
        .all()
    )
    notif_driver_ids = sorted(n.driver_id for n in notifs)
    assert notif_driver_ids == sorted([a.id, b.id, c.id]), (
        f"Expected notifs for winner+losers {a.id},{b.id},{c.id}; got {notif_driver_ids!r}"
    )
    by_driver = {n.driver_id: n for n in notifs}
    assert by_driver[a.id].kind == "approved_by_manager"
    assert "You were approved for order" in by_driver[a.id].message
    for loser in (b, c):
        assert by_driver[loser.id].kind == "order_taken_by_other"
        assert "Another driver was given order" in by_driver[loser.id].message
        assert by_driver[loser.id].read_at is None


def test_approve_request_notifies_sole_approved_driver(db_session):
    """Sole requester gets an approval notification."""
    a = _make_driver(db_session, "sole requester")
    o = _make_order(db_session, "TEST-NOTIFY-2", status="requested")
    db_session.add(DeliveryRequest(
        delivery_id=o.id, driver_id=a.id, status="pending",
    ))
    db_session.flush()

    lifecycle.approve_request(db_session, o, a, decided_by_user_id=None)
    db_session.flush()

    notifs = (
        db_session.query(DriverNotification)
        .filter(DriverNotification.related_delivery_id == o.id)
        .all()
    )
    assert len(notifs) == 1
    assert notifs[0].driver_id == a.id
    assert notifs[0].kind == "approved_by_manager"
    assert "You were approved for order" in notifs[0].message
    assert notifs[0].read_at is None


# ----------------------------------------------------------------------
# find_conflicting_request
# ----------------------------------------------------------------------

def test_find_conflicting_request_detects_overlap(db_session):
    """Driver has pending request for order with [12:00, 13:00] window.
    New order has [12:30, 13:30] → overlap → conflict returned."""
    d = _make_driver(db_session, "overlap test driver")
    existing = _make_order(
        db_session, "EXIST-1",
        start=datetime(2026, 5, 15, 12, 0),
        end=datetime(2026, 5, 15, 13, 0),
        status="requested",
    )
    db_session.add(DeliveryRequest(
        delivery_id=existing.id, driver_id=d.id, status="pending",
    ))
    new_order = _make_order(
        db_session, "NEW-1",
        start=datetime(2026, 5, 15, 12, 30),
        end=datetime(2026, 5, 15, 13, 30),
    )
    db_session.flush()

    clash = lifecycle.find_conflicting_request(db_session, d.id, new_order)
    assert clash is not None
    assert clash.delivery_id == existing.id


def test_find_conflicting_request_none_when_disjoint(db_session):
    """Existing window [12:00, 13:00] + new [14:00, 15:00] → no overlap."""
    d = _make_driver(db_session, "disjoint test driver")
    existing = _make_order(
        db_session, "EXIST-2",
        start=datetime(2026, 5, 15, 12, 0),
        end=datetime(2026, 5, 15, 13, 0),
        status="requested",
    )
    db_session.add(DeliveryRequest(
        delivery_id=existing.id, driver_id=d.id, status="pending",
    ))
    new_order = _make_order(
        db_session, "NEW-2",
        start=datetime(2026, 5, 15, 14, 0),
        end=datetime(2026, 5, 15, 15, 0),
    )
    db_session.flush()

    clash = lifecycle.find_conflicting_request(db_session, d.id, new_order)
    assert clash is None


def test_find_conflicting_request_none_when_no_pending(db_session):
    """Driver has no pending requests → never a conflict."""
    d = _make_driver(db_session, "no pending test driver")
    new_order = _make_order(
        db_session, "NEW-3",
        start=datetime(2026, 5, 15, 12, 0),
        end=datetime(2026, 5, 15, 13, 0),
    )
    db_session.flush()

    clash = lifecycle.find_conflicting_request(db_session, d.id, new_order)
    assert clash is None


def test_find_conflicting_request_degrades_when_no_time_data(db_session):
    """Order with no delivery_window_start → can't infer window → no
    conflict (graceful degradation for legacy rows)."""
    d = _make_driver(db_session, "legacy test driver")
    existing = _make_order(db_session, "EXIST-3", status="requested")
    db_session.add(DeliveryRequest(
        delivery_id=existing.id, driver_id=d.id, status="pending",
    ))
    new_order = _make_order(
        db_session, "NEW-4",
        start=datetime(2026, 5, 15, 12, 0),
        end=datetime(2026, 5, 15, 13, 0),
    )
    db_session.flush()

    clash = lifecycle.find_conflicting_request(db_session, d.id, new_order)
    assert clash is None


# ----------------------------------------------------------------------
# _order_time_window helper
# ----------------------------------------------------------------------

def test_order_time_window_uses_both_when_present():
    """Both start + end → returns (start, end) verbatim."""
    s = datetime(2026, 5, 15, 12, 0)
    e = datetime(2026, 5, 15, 13, 0)
    o = Order(external_order_id="WIN-1",
              delivery_window_start=s, delivery_window_end=e)
    assert lifecycle._order_time_window(o) == (s, e)


def test_order_time_window_centers_15min_on_start_when_only_start():
    """Only start present → 15-min window centered on start (samai
    #1599 default: 7.5 min before, 7.5 min after)."""
    s = datetime(2026, 5, 15, 12, 0)
    o = Order(external_order_id="WIN-2",
              delivery_window_start=s, delivery_window_end=None)
    w = lifecycle._order_time_window(o)
    assert w is not None
    start, end = w
    assert (s - start) == timedelta(minutes=7, seconds=30)
    assert (end - s) == timedelta(minutes=7, seconds=30)


def test_order_time_window_none_when_no_signal():
    """No start, no end → None."""
    o = Order(external_order_id="WIN-3")
    assert lifecycle._order_time_window(o) is None
