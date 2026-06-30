from __future__ import annotations

from app.models import Order


def test_driver_order_upload_route_serves_persistent_file(db_session, monkeypatch, tmp_path):
    from app import create_app
    import app.web.driver_routes as driver_routes

    upload_root = tmp_path / "driver-order-uploads"
    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("DRIVER_ORDER_UPLOADS_DIR", str(upload_root))
    monkeypatch.setattr(driver_routes, "get_db", lambda: iter([db_session]))

    order = Order(id=42, external_order_id="TST-42")
    order.setup_photo_url = "/driver/order-uploads/42/delivery/delivery-proof.jpg"
    db_session.add(order)
    db_session.commit()

    target_dir = upload_root / "42" / "delivery"
    target_dir.mkdir(parents=True)
    (target_dir / "delivery-proof.jpg").write_bytes(b"proof-bytes")

    app = create_app()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
    resp = client.get("/driver/order-uploads/42/delivery/delivery-proof.jpg")

    assert resp.status_code == 200
    assert resp.data == b"proof-bytes"


def test_driver_order_upload_route_rejects_wrong_filename(db_session, monkeypatch, tmp_path):
    from app import create_app
    import app.web.driver_routes as driver_routes

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("DRIVER_ORDER_UPLOADS_DIR", str(tmp_path / "driver-order-uploads"))
    monkeypatch.setattr(driver_routes, "get_db", lambda: iter([db_session]))

    order = Order(id=43, external_order_id="TST-43")
    order.setup_photo_url = "/driver/order-uploads/43/delivery/right.jpg"
    db_session.add(order)
    db_session.commit()

    app = create_app()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
    resp = client.get("/driver/order-uploads/43/delivery/wrong.jpg")

    assert resp.status_code == 404
