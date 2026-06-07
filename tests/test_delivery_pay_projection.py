from __future__ import annotations

from app.models import Driver, Order
from app.services import delivery_lifecycle as lifecycle
from app.services.delivery_pay_projection import projected_driver_pay


def test_projected_driver_pay_under_20_miles_is_35():
    order = Order(external_order_id="922-QET", pickup_miles=19.4)

    assert projected_driver_pay(order) == 35.0


def test_approval_recomputes_stale_base_only_payout(db_session):
    driver = Driver(name="Tatiana", location="tomball", active=True)
    order = Order(
        external_order_id="922-QET",
        status="requested",
        pickup_miles=19.4,
        potential_payout=25.0,
    )
    db_session.add_all([driver, order])
    db_session.flush()

    lifecycle.approve_request(db_session, order, driver, decided_by_user_id=None)

    assert order.potential_payout == 35.0
