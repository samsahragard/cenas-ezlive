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
    from app.models import Driver, Order, OrderItem
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
    assert data["counts"]["upload_file_refs"] == 1
    assert data["counts"]["upload_files_available"] == 1
    assert data["upload_files"][0]["file_b64"] == "aW1hZ2UtYnl0ZXM="
