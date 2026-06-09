from __future__ import annotations

import os

os.environ.setdefault("ALLOW_DEV_SECRET", "1")

import pytest

from app.models import DeliveryRequest, Driver, Order, User
from app.web import driver_system as driver_mod


@pytest.fixture
def app_bound(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    sess = lambda: db_session
    monkeypatch.setattr(appdb, "SessionLocal", sess, raising=False)
    monkeypatch.setattr(driver_mod, "SessionLocal", sess, raising=False)
    return flask_app, db_session


def _manager(db):
    user = User(
        full_name="Manager",
        email="manager@test.local",
        passcode_hash="x",
        permission_level="partner",
        store_scope=None,
        active=True,
        first_login_done=True,
        session_version=1,
    )
    db.add(user)
    db.commit()
    return user


def _driver(db):
    driver = Driver(name="Tatiana", location="tomball", active=True, status="active")
    db.add(driver)
    db.commit()
    return driver


def _order(db, *, driver_id=None, status="requested", day="2026-06-08", ext="ORD-1"):
    order = Order(
        external_order_id=ext,
        status=status,
        assigned_driver_id=driver_id,
        origin_store_id="store_2",
        delivery_date=day,
        deliver_at="11:00 AM",
        delivery_address="25250 Borough Park Dr, Spring, TX 77380",
        delivery_window={"start": "11:00 AM", "end": "11:30 AM"},
    )
    db.add(order)
    db.commit()
    return order


def test_feasibility_ignores_driver_active_delivery_on_different_date(app_bound):
    flask_app, db = app_bound
    manager = _manager(db)
    driver = _driver(db)
    _order(db, driver_id=driver.id, status="approved", day="2026-06-09", ext="TOMORROW")
    new_order = _order(db, status="requested", day="2026-06-08", ext="TODAY")

    with flask_app.test_request_context(
        f"/ez-manage/feasibility-check?driver_id={driver.id}&new_delivery_id={new_order.id}"
    ):
        from flask import g

        g.current_user = manager
        resp = driver_mod.ez_manage_feasibility_check()

    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"ok": True, "stack_needed": False}


def test_feasibility_warns_driver_active_delivery_on_same_date(app_bound):
    flask_app, db = app_bound
    manager = _manager(db)
    driver = _driver(db)
    active = _order(db, driver_id=driver.id, status="approved", day="2026-06-08", ext="ACTIVE")
    new_order = _order(db, status="requested", day="2026-06-08", ext="TODAY")

    with flask_app.test_request_context(
        f"/ez-manage/feasibility-check?driver_id={driver.id}&new_delivery_id={new_order.id}"
    ):
        from flask import g

        g.current_user = manager
        resp = driver_mod.ez_manage_feasibility_check()

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["stack_needed"] is True
    assert data["warning_needed"] is True
    assert data["blocked"] is False
    assert data["feasible"] is True
    assert data["active_delivery_id"] == active.id
    assert data["active_external_order_id"] == "ACTIVE"
    assert data["existing_external_order_id"] == "ACTIVE"
    assert data["reason"] == "driver has another delivery on this date"


def test_feasibility_warns_driver_delivered_delivery_on_same_date(app_bound):
    flask_app, db = app_bound
    manager = _manager(db)
    driver = _driver(db)
    delivered = _order(db, driver_id=driver.id, status="delivered", day="2026-06-08", ext="DONE")
    new_order = _order(db, status="requested", day="2026-06-08", ext="TODAY")

    with flask_app.test_request_context(
        f"/ez-manage/feasibility-check?driver_id={driver.id}&new_delivery_id={new_order.id}"
    ):
        from flask import g

        g.current_user = manager
        resp = driver_mod.ez_manage_feasibility_check()

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["stack_needed"] is True
    assert data["warning_needed"] is True
    assert data["blocked"] is False
    assert data["feasible"] is True
    assert data["existing_delivery_id"] == delivered.id
    assert data["existing_external_order_id"] == "DONE"


def test_approve_allows_second_same_day_active_delivery_server_side(app_bound):
    flask_app, db = app_bound
    manager = _manager(db)
    driver = _driver(db)
    _order(db, driver_id=driver.id, status="approved", day="2026-06-08", ext="ACTIVE")
    new_order = _order(db, status="requested", day="2026-06-08", ext=None)
    req = DeliveryRequest(delivery_id=new_order.id, driver_id=driver.id, status="pending")
    db.add(req)
    db.commit()

    with flask_app.test_request_context(f"/ez-manage/approve/{req.id}", method="POST"):
        from flask import g

        g.current_user = manager
        resp = driver_mod.ez_manage_approve(req.id)

    assert resp.status_code == 302
    assert new_order.status == "approved"
    assert new_order.assigned_driver_id == driver.id
    assert req.status == "approved"


def test_request_warning_reports_same_day_pending_request(app_bound):
    flask_app, db = app_bound
    driver = _driver(db)
    existing = _order(db, status="requested", day="2026-06-08", ext="EXISTING")
    db.add(DeliveryRequest(delivery_id=existing.id, driver_id=driver.id, status="pending"))
    new_order = _order(db, status="available", day="2026-06-08", ext="TODAY")
    db.commit()

    with flask_app.test_request_context(
        f"/ez-market/request-warning?delivery_id={new_order.id}"
    ):
        from flask import session

        session["driver_id"] = driver.id
        resp = driver_mod.ez_market_request_warning()

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["warning_needed"] is True
    assert data["existing_kind"] == "pending request"
    assert data["existing_external_order_id"] == "EXISTING"


def test_request_allows_second_same_day_pending_request_server_side(app_bound):
    flask_app, db = app_bound
    driver = _driver(db)
    existing = _order(db, status="requested", day="2026-06-08", ext="EXISTING")
    db.add(DeliveryRequest(delivery_id=existing.id, driver_id=driver.id, status="pending"))
    new_order = _order(db, status="available", day="2026-06-08", ext="TODAY")
    db.commit()

    with flask_app.test_request_context(
        f"/ez-market/request/{new_order.id}",
        method="POST",
    ):
        from flask import session

        session["driver_id"] = driver.id
        resp = driver_mod.ez_market_request(new_order.id)

    assert resp.status_code == 302
    saved_order = db.get(Order, new_order.id)
    assert saved_order.status == "requested"
    req = (
        db.query(DeliveryRequest)
        .filter(DeliveryRequest.delivery_id == new_order.id)
        .filter(DeliveryRequest.driver_id == driver.id)
        .first()
    )
    assert req is not None
    assert req.status == "pending"
