from __future__ import annotations

from datetime import datetime, timedelta

from app.models import Driver, EzcaterTrackingPoint, Order


def _body(lat=30.08, lng=-95.62, name="CK #2 - Tatiana Campos", status="driver_en_route_to_dropoff"):
    return {
        "data": {
            "drivers": [{
                "name": name,
                "currentStatus": {"key": status},
                "currentLocation": {"latitude": lat, "longitude": lng},
            }]
        }
    }


def test_record_tracking_sample_links_driver_and_summarizes_route(db_session):
    from app.services.ezcater_route_history import record_tracking_sample, route_summary_for_order

    driver = Driver(name="Tatiana Campos", location="tomball")
    order = Order(
        external_order_id="UX7-ARR",
        delivery_tracking_id="c2d614a5-1111-2222-3333-444444444444",
        origin_store_id="store_2",
        ezcater_driver_name="CK #2 - Tatiana Campos",
        status="en_route",
        delivery_date="2026-06-09",
    )
    db_session.add_all([driver, order])
    db_session.commit()

    t0 = datetime(2026, 6, 9, 15, 0, 0)
    first = record_tracking_sample(db_session, order, _body(), captured_at=t0)
    duplicate = record_tracking_sample(db_session, order, _body(), captured_at=t0 + timedelta(seconds=10))
    second = record_tracking_sample(
        db_session,
        order,
        _body(lat=30.18, lng=-95.50),
        captured_at=t0 + timedelta(seconds=90),
    )
    db_session.commit()

    assert first is not None
    assert duplicate is None
    assert second is not None
    assert order.assigned_driver_id == driver.id
    assert order.tracking_status == "Tracked"
    assert db_session.query(EzcaterTrackingPoint).filter_by(order_id=order.id).count() == 2

    summary = route_summary_for_order(db_session, order.id)
    assert summary.point_count == 2
    assert summary.driver_id == driver.id
    assert summary.duration_minutes == 1
    assert summary.distance_miles > 0


def test_poll_one_records_route_point_from_ezcater_body(db_session, monkeypatch):
    from app.services import ezcater_live_tracker

    driver = Driver(name="Tatiana Campos", location="tomball")
    order = Order(
        external_order_id="UX7-ARR",
        delivery_tracking_id="c2d614a5-1111-2222-3333-444444444444",
        origin_store_id="store_2",
        status="en_route",
        delivery_date="2026-06-09",
    )
    db_session.add_all([driver, order])
    db_session.commit()

    monkeypatch.setattr(ezcater_live_tracker, "fetch_state", lambda uuid, refresh_only=True: _body())

    body = ezcater_live_tracker.poll_one(order)
    db_session.commit()

    assert body is not None
    assert order.ezcater_driver_lat == 30.08
    assert order.ezcater_driver_lng == -95.62
    assert order.tracking_status == "Tracked"
    point = db_session.query(EzcaterTrackingPoint).filter_by(order_id=order.id).one()
    assert point.driver_id == driver.id
    assert point.provider_status_key == "driver_en_route_to_dropoff"
