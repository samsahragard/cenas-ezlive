"""Phase 0 / Block 5 — smoke tests for app.services.delivery_lifecycle.

Verifies the state-machine guards (legal transitions allowed, illegal
ones raise IllegalTransition) plus a happy-path end-to-end run through
open_for_bidding → request → approve → picked_up → en_route → delivered.
"""
from __future__ import annotations

import pytest

from app.services import delivery_lifecycle as lifecycle


# ---- pure _check guard ----

@pytest.mark.parametrize("transition, current", [
    ("open_for_bidding", "new"),
    ("open_for_bidding", None),
    ("request", "available"),
    ("request", "requested"),
    ("approve", "available"),
    ("approve", "requested"),
    ("back_to_bidding", "requested"),
    ("decline_all", "requested"),
    ("mark_picked_up", "approved"),
    ("mark_en_route", "picked_up"),
    ("mark_delivered", "en_route"),
    ("mark_delivered", "picked_up"),
    ("driver_cancel", "approved"),
    ("no_show", "approved"),
])
def test_legal_transitions_dont_raise(transition, current):
    lifecycle._check(transition, current)  # should not raise


@pytest.mark.parametrize("transition, bad_current", [
    ("open_for_bidding", "approved"),      # already approved, can't reopen
    ("request",          "delivered"),     # done
    ("approve",          "cancelled"),     # cancelled
    ("mark_picked_up",   "available"),     # no driver yet
    ("mark_en_route",    "approved"),      # haven't picked up
    ("mark_delivered",   "available"),     # nobody assigned
    ("driver_cancel",    "delivered"),     # too late
])
def test_illegal_transitions_raise(transition, bad_current):
    with pytest.raises(lifecycle.IllegalTransition):
        lifecycle._check(transition, bad_current)


def test_unknown_transition_raises():
    with pytest.raises(lifecycle.IllegalTransition):
        lifecycle._check("teleport_to_mars", "available")


# ---- happy path through the full machine ----

def test_full_lifecycle_happy_path(db_session):
    from app.models import Driver, Order

    d = Driver(name="Happy Path", location="tomball",
               lifetime_delivery_count=10, active=True)
    o = Order(external_order_id="TEST-1", status="new",
              delivery_date="2026-05-13", deliver_at="12:00 PM")
    db_session.add_all([d, o])
    db_session.commit()

    lifecycle.open_for_bidding(db_session, o)
    assert o.status == "available"

    lifecycle.request_delivery(db_session, o, d)
    assert o.status == "requested"

    lifecycle.approve_request(db_session, o, d, decided_by_user_id=None)
    assert o.status == "approved"
    assert o.assigned_driver_id == d.id

    lifecycle.mark_picked_up(db_session, o)
    assert o.status == "picked_up"
    assert o.pickup_actual_at is not None

    lifecycle.mark_en_route(db_session, o)
    assert o.status == "en_route"

    lifecycle.mark_delivered(db_session, o, setup_photo_url="https://x/y.jpg")
    assert o.status == "delivered"
    # mark_delivered should have bumped the driver's lifetime count
    # in the same identity-mapped session — no need to refresh.
    assert d.lifetime_delivery_count == 11
