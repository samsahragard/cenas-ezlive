from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models import EzcaterTrackingPoint, Order


def test_public_order_hides_expired_tracker_coordinates():
    from app.services import ezcater_tracking_watch as watch

    due = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    public = watch._public_order({
        "uuid": "track-expired",
        "order_number": "ABC-123",
        "status_key": "expired",
        "lat": 29.76,
        "lng": -95.37,
        "deliver_at": due,
    })

    assert public["lat"] is None
    assert public["lng"] is None
    assert public["risk"]["level"] == "watch"


def test_public_order_hides_delivered_tracker_coordinates():
    from app.services import ezcater_tracking_watch as watch

    public = watch._public_order({
        "uuid": "track-delivered",
        "order_number": "ABC-456",
        "status_key": "delivered",
        "lat": 29.76,
        "lng": -95.37,
    })

    assert public["lat"] is None
    assert public["lng"] is None
    assert public["risk"] == {"level": "ok", "text": "tracking ended"}


def test_public_order_hides_driver_dropped_off_tracker_coordinates():
    from app.services import ezcater_tracking_watch as watch

    public = watch._public_order({
        "uuid": "track-dropped-off",
        "order_number": "ABC-654",
        "status_key": "driver_dropped_off",
        "lat": 29.94,
        "lng": -95.11,
    })

    assert public["lat"] is None
    assert public["lng"] is None
    assert public["risk"] == {"level": "ok", "text": "tracking ended"}


def test_list_app_orders_hides_terminal_tracker_coordinates(db_session, monkeypatch):
    from app.services import ezcater_tracking_watch as watch

    order = Order(
        external_order_id="ABC-789",
        delivery_date=datetime.now().strftime("%Y-%m-%d"),
        status="confirmed",
        delivery_tracking_id="track-terminal",
        ezcater_status_key="completed",
        ezcater_driver_lat=29.76,
        ezcater_driver_lng=-95.37,
    )
    db_session.add(order)
    db_session.commit()
    monkeypatch.setattr(watch, "SessionLocal", lambda: db_session)

    rows = watch.list_app_orders()

    assert rows[0]["tracker"]["lat"] is None
    assert rows[0]["tracker"]["lng"] is None


def test_live_poll_records_app_order_route_history(db_session, monkeypatch):
    from app.services import ezcater_live_tracker
    from app.services import ezcater_tracking_watch as watch

    order = Order(
        external_order_id="ABC-321",
        delivery_date=datetime.now().strftime("%Y-%m-%d"),
        status="confirmed",
        delivery_tracking_id="track-live",
    )
    db_session.add(order)
    db_session.commit()
    monkeypatch.setattr(watch, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        ezcater_live_tracker,
        "fetch_state",
        lambda tracking_uuid, refresh_only=True: {
            "data": {
                "drivers": [{
                    "name": "CK #2 - Anibal Medina",
                    "currentStatus": {"key": "driver_en_route_to_dropoff"},
                    "currentLocation": {"latitude": 29.76, "longitude": -95.37},
                }]
            }
        },
    )

    rows = watch.list_app_orders(live_poll=True, record_route_history=True)

    assert rows[0]["tracker"]["lat"] == 29.76
    assert rows[0]["tracker"]["lng"] == -95.37
    assert order.tracking_status == "Tracked"
    point = db_session.query(EzcaterTrackingPoint).filter_by(order_id=order.id).one()
    assert point.tracking_uuid == "track-live"
    assert point.driver_name == "CK #2 - Anibal Medina"


def test_live_map_template_removes_unapproved_marker_keys():
    template = Path("app/templates/ezcater_tracking_watch.html").read_text(encoding="utf-8")

    assert "const activeMarkerKeys = new Set();" in template
    assert "if (isTerminalTracker(o)) return;" in template
    assert "if (!activeMarkerKeys.has(key))" in template
    assert "knownOrderKeys.has(orderKey(o.order_number))" in template
    assert "color:#1f2937;line-height:1.35" in template
