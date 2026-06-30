from flask import Flask


def test_driver_local_export_is_token_gated(db_session, monkeypatch, tmp_path):
    from app.web.driverdc_export_routes import driverdc_export_bp

    monkeypatch.setenv("DRIVERDC_EXPORT_TOKEN", "secret")
    monkeypatch.setenv("DRIVER_ORDER_UPLOADS_DIR", str(tmp_path / "uploads"))
    app = Flask(__name__)
    app.register_blueprint(driverdc_export_bp)

    with app.test_client() as client:
        assert client.get("/cron/driver-local-export").status_code == 403
        assert client.get("/cron/driver-local-export?token=wrong").status_code == 403


def test_driver_local_export_returns_driver_rows_and_upload_bytes(db_session, monkeypatch, tmp_path):
    from app.models import Driver, DriverEvent, DriverFile, Order, OrderItem
    import app.web.driverdc_export_routes as export_mod
    from app.web.driverdc_export_routes import driverdc_export_bp

    monkeypatch.setenv("DRIVERDC_EXPORT_TOKEN", "secret")
    upload_root = tmp_path / "uploads"
    monkeypatch.setenv("DRIVER_ORDER_UPLOADS_DIR", str(upload_root))
    monkeypatch.setattr(export_mod, "SessionLocal", lambda: db_session)

    driver = Driver(id=7, name="Test Driver", location="Tomball", email="driver@example.com")
    order = Order(
        id=42,
        external_order_id="ABC-123",
        assigned_driver_id=7,
        assigned_driver="Test Driver",
        status="delivered",
        setup_photo_url="/driver/order-uploads/42/delivery/delivery-proof.jpg",
    )
    db_session.add_all([
        driver,
        order,
        OrderItem(order_id=42, raw_alias="Fajitas", item_key="fajitas", qty=2),
        DriverFile(
            driver_id=7,
            order_id=42,
            kind="delivery",
            filename="delivery-proof.jpg",
            public_route="/driver/order-uploads/42/delivery/delivery-proof.jpg",
            exists=True,
            source="test",
        ),
        DriverEvent(
            driver_id=7,
            order_id=42,
            event_type="delivery_photo_uploaded",
            source="test",
            payload_json={"url": "/driver/order-uploads/42/delivery/delivery-proof.jpg"},
        ),
    ])
    target = upload_root / "42" / "delivery"
    target.mkdir(parents=True)
    (target / "delivery-proof.jpg").write_bytes(b"image-bytes")
    db_session.commit()

    app = Flask(__name__)
    app.register_blueprint(driverdc_export_bp)

    with app.test_client() as client:
        resp = client.get("/cron/driver-local-export?token=secret")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["contract"] == "driver-local-v1-complete"
    assert data["tables"]["drivers"][0]["id"] == 7
    assert data["tables"]["orders"][0]["external_order_id"] == "ABC-123"
    assert data["tables"]["order_items"][0]["raw_alias"] == "Fajitas"
    assert data["tables"]["driver_file"][0]["kind"] == "delivery"
    assert data["tables"]["driver_event"][0]["event_type"] == "delivery_photo_uploaded"
    assert data["counts"]["upload_file_refs"] == 1
    assert data["counts"]["upload_files_available"] == 1
    assert data["counts"]["driver_file_rows"] == 1
    assert data["counts"]["driver_event_rows"] == 1
    assert data["upload_files"][0]["file_b64"] == "aW1hZ2UtYnl0ZXM="


def test_driver_file_backfill_preserves_missing_legacy_reference(db_session, monkeypatch, tmp_path):
    from app.models import DriverFile, Order
    from app.services.driver_profile_audit import backfill_driver_files_from_orders

    monkeypatch.setenv("DRIVER_ORDER_UPLOADS_DIR", str(tmp_path / "uploads"))

    order = Order(
        id=99,
        external_order_id="MISS-99",
        assigned_driver_id=5,
        setup_photo_url="/driver/order-uploads/99/delivery/missing.jpg",
    )
    db_session.add(order)
    db_session.commit()

    result = backfill_driver_files_from_orders(db_session)
    db_session.commit()

    assert result == {"file_refs": 1, "available": 0}
    row = db_session.query(DriverFile).one()
    assert row.driver_id == 5
    assert row.order_id == 99
    assert row.kind == "delivery"
    assert row.exists is False
    assert row.public_route == "/driver/order-uploads/99/delivery/missing.jpg"
