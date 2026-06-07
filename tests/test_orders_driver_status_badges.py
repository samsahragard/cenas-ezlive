from __future__ import annotations

from app.models import DeliveryRequest, Driver, DriverAssignmentJob, Order
from app.web.orders_browse import _driver_status_by_order_id


def test_driver_status_badges_show_requested_approved_and_assigned(db_session):
    driver = Driver(name="Tatiana", location="tomball", active=True)
    pending = Order(id=1, external_order_id="REQ-1", status="requested")
    approved = Order(
        id=2,
        external_order_id="APP-1",
        status="approved",
        assigned_driver_id=10,
    )
    assigned = Order(id=3, external_order_id="JOB-1", status="available")
    db_session.add_all([driver, pending, approved, assigned])
    db_session.flush()
    db_session.add_all([
        DeliveryRequest(delivery_id=pending.id, driver_id=driver.id, status="pending"),
        DeliveryRequest(delivery_id=approved.id, driver_id=driver.id, status="approved"),
        DriverAssignmentJob(
            job_id="job-1",
            order_id="JOB-1",
            current_driver=None,
            new_driver="Tatiana Campos",
            status="completed",
        ),
    ])
    db_session.flush()

    badges = _driver_status_by_order_id(db_session, [pending, approved, assigned])

    assert badges[pending.id]["label"] == "1 Requested"
    assert badges[approved.id]["label"] == "EZ Approved"
    assert badges[assigned.id]["label"] == "EZ Assigned"
