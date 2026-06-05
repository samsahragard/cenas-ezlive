from __future__ import annotations

from app.models import Order


def test_sync_tracking_updates_saves_uuid_and_detects_started(db_session, monkeypatch):
    from app.services import ezcater_tracking_sync as sync_mod

    order = Order(external_order_id="ABC-123", status="confirmed")
    db_session.add(order)
    db_session.commit()

    def fake_poll_one(row):
        row.ezcater_status_key = "driver_en_route_to_dropoff"
        row.ezcater_driver_lat = 29.76
        row.ezcater_driver_lng = -95.37
        return {
            "data": {
                "drivers": [{
                    "currentStatus": {"key": "driver_en_route_to_dropoff"},
                    "currentLocation": {"latitude": 29.76, "longitude": -95.37},
                }]
            }
        }

    monkeypatch.setattr(sync_mod, "poll_one", fake_poll_one)

    result = sync_mod.sync_tracking_updates(
        [{
            "order_number": "ABC123",
            "tracking_url": "https://delivery-tracking.ezcater.com/delivery/"
                            "1cd95cb7-83b2-4f4c-9429-9f3ec634eed6",
        }],
        session_factory=lambda: db_session,
    )

    assert result["saved"] == 1
    assert result["new"] == 1
    assert result["polled"] == 1
    assert result["started"] == 1
    assert result["orders"][0]["status_key"] == "driver_en_route_to_dropoff"
    assert order.delivery_tracking_id == "1cd95cb7-83b2-4f4c-9429-9f3ec634eed6"


def test_sync_tracking_updates_skips_missing_uuid_and_missing_order(db_session):
    from app.services import ezcater_tracking_sync as sync_mod

    result = sync_mod.sync_tracking_updates(
        [
            {"order_number": "ABC-123", "tracking_url": "not a uuid"},
            {
                "order_number": "DEF-456",
                "tracking_url": "https://delivery-tracking.ezcater.com/delivery/"
                                "2cd95cb7-83b2-4f4c-9429-9f3ec634eed6",
            },
        ],
        session_factory=lambda: db_session,
    )

    assert result["saved"] == 0
    assert result["skipped"] == [{
        "order_number": "ABC-123",
        "reason": "missing_order_number_or_tracking_uuid",
    }]
    assert result["not_found"] == ["DEF-456"]


def test_machine_endpoint_requires_token(db_session, monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")
    monkeypatch.setenv("CENA_GATEWAY_TOKEN", "testtoken")
    monkeypatch.setenv("SAM_CHAT_USER_ID", "1")

    import app.db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)

    from app.services import ezcater_tracking_sync as sync_mod
    monkeypatch.setattr(sync_mod, "poll_one", lambda row: {"data": {"status": "expired"}})

    import app.web.cena as cena_mod
    monkeypatch.setattr(cena_mod, "SessionLocal", lambda: db_session)

    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()

    missing = client.post("/sam/cena/run-ezcater-tracking-sync", json={"updates": []})
    assert missing.status_code == 403

    bad_shape = client.post(
        "/sam/cena/run-ezcater-tracking-sync",
        json={"updates": {}},
        headers={"X-Cena-Token": "testtoken"},
    )
    assert bad_shape.status_code == 400

    order = Order(external_order_id="TST-001", status="confirmed")
    db_session.add(order)
    db_session.commit()

    ok = client.post(
        "/sam/cena/run-ezcater-tracking-sync",
        json={"updates": [{
            "order_number": "TST001",
            "tracking_url": "https://delivery-tracking.ezcater.com/delivery/"
                            "3cd95cb7-83b2-4f4c-9429-9f3ec634eed6",
        }]},
        headers={"X-Cena-Token": "testtoken"},
    )
    assert ok.status_code == 200
    data = ok.get_json()
    assert data["ok"] is True
    assert data["saved"] == 1
    assert order.delivery_tracking_id == "3cd95cb7-83b2-4f4c-9429-9f3ec634eed6"
