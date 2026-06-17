from __future__ import annotations

import os

os.environ.setdefault("ALLOW_DEV_SECRET", "1")

import pytest

from app.models import DeliveryRequest, Driver, Order
from app.web import driver_system as driver_mod


@pytest.fixture
def app_bound(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb
    from app.web import keypad_auth

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    sess = lambda: db_session
    monkeypatch.setattr(appdb, "SessionLocal", sess, raising=False)
    monkeypatch.setattr(driver_mod, "SessionLocal", sess, raising=False)
    monkeypatch.setattr(keypad_auth, "SessionLocal", sess, raising=False)
    return flask_app, db_session


def _driver(db, *, location="tomball", home_store_id=None):
    driver = Driver(
        name=f"{location.title()} Driver",
        location=location,
        home_store_id=home_store_id,
        active=True,
        status="active",
        first_login_done=True,
        session_version=1,
    )
    db.add(driver)
    db.commit()
    return driver


def _order(db, *, store_id, ext, address, status="available"):
    order = Order(
        external_order_id=ext,
        status=status,
        origin_store_id=store_id,
        reported_store_id=store_id,
        pickup_kitchen=driver_mod._order_store_slug(Order(origin_store_id=store_id)),
        delivery_date="2099-01-01",
        deliver_at="11:00 AM",
        delivery_address=address,
        pickup_miles=2.0,
    )
    db.add(order)
    db.commit()
    return order


def test_tomball_driver_market_hides_copperfield_orders(app_bound):
    flask_app, db = app_bound
    driver = _driver(db, location="tomball")
    _order(db, store_id="store_2", ext="TOM-1", address="VISIBLE_TOMBALL_DROP")
    _order(db, store_id="store_1", ext="COP-1", address="HIDDEN_COPPERFIELD_DROP")

    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["driver_id"] = driver.id
        session["driver_name"] = driver.name
        session["driver_location"] = driver.location
        session["driver_session_version"] = driver.session_version
        session["auth_ok"] = True

    resp = client.get("/ez-market")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "VISIBLE_TOMBALL_DROP" in html
    assert "HIDDEN_COPPERFIELD_DROP" not in html


def test_copperfield_driver_market_hides_tomball_orders(app_bound):
    flask_app, db = app_bound
    driver = _driver(db, location="copperfield")
    _order(db, store_id="store_1", ext="COP-1", address="VISIBLE_COPPERFIELD_DROP")
    _order(db, store_id="store_2", ext="TOM-1", address="HIDDEN_TOMBALL_DROP")

    client = flask_app.test_client()
    with client.session_transaction() as session:
        session["driver_id"] = driver.id
        session["driver_name"] = driver.name
        session["driver_location"] = driver.location
        session["driver_session_version"] = driver.session_version
        session["auth_ok"] = True

    resp = client.get("/ez-market")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "VISIBLE_COPPERFIELD_DROP" in html
    assert "HIDDEN_TOMBALL_DROP" not in html


def test_public_market2_shows_both_stores_without_functional_actions(app_bound):
    flask_app, db = app_bound
    _order(db, store_id="store_2", ext="TOM-PUBLIC", address="VISIBLE_TOMBALL_PUBLIC")
    _order(db, store_id="store_1", ext="COP-PUBLIC", address="VISIBLE_COPPERFIELD_PUBLIC", status="approved")

    client = flask_app.test_client()
    resp = client.get("/ez-market2")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "VISIBLE_TOMBALL_PUBLIC" in html
    assert "VISIBLE_COPPERFIELD_PUBLIC" in html
    assert "My queue" not in html
    assert "History" not in html
    assert 'action="/ez-market/request/' not in html
    assert 'action="/ez-market/cancel-request/' not in html
    assert "Request delivery" in html
    assert "Not open for bidding (status: approved)" in html
    for label in ("Profile", "Orders", "Ez Market", "Pay"):
        assert label in html
    assert 'href="#"' in html
    assert 'aria-disabled="true"' in html


def test_cross_store_request_is_blocked_server_side(app_bound):
    flask_app, db = app_bound
    driver = _driver(db, location="tomball")
    copperfield_order = _order(
        db,
        store_id="store_1",
        ext="COP-REQUEST",
        address="HIDDEN_COPPERFIELD_DROP",
    )

    with flask_app.test_request_context(
        f"/ez-market/request/{copperfield_order.id}",
        method="POST",
    ):
        from flask import session

        session["driver_id"] = driver.id
        resp = driver_mod.ez_market_request(copperfield_order.id)

    assert resp.status_code == 302
    assert db.query(DeliveryRequest).count() == 0
    assert db.get(Order, copperfield_order.id).status == "available"


def test_same_store_request_still_works(app_bound):
    flask_app, db = app_bound
    driver = _driver(db, location="tomball")
    tomball_order = _order(
        db,
        store_id="store_2",
        ext="TOM-REQUEST",
        address="VISIBLE_TOMBALL_DROP",
    )

    with flask_app.test_request_context(
        f"/ez-market/request/{tomball_order.id}",
        method="POST",
    ):
        from flask import session

        session["driver_id"] = driver.id
        resp = driver_mod.ez_market_request(tomball_order.id)

    assert resp.status_code == 302
    assert db.get(Order, tomball_order.id).status == "requested"
    req = db.query(DeliveryRequest).filter_by(
        delivery_id=tomball_order.id,
        driver_id=driver.id,
    ).one()
    assert req.status == "pending"


def test_cross_store_warning_endpoint_is_blocked(app_bound):
    flask_app, db = app_bound
    driver = _driver(db, location="tomball")
    copperfield_order = _order(
        db,
        store_id="store_1",
        ext="COP-WARNING",
        address="HIDDEN_COPPERFIELD_DROP",
    )

    with flask_app.test_request_context(
        f"/ez-market/request-warning?delivery_id={copperfield_order.id}"
    ):
        from flask import session

        session["driver_id"] = driver.id
        resp = driver_mod.ez_market_request_warning()

    body, status = resp
    assert status == 403
    assert body.get_json() == {
        "ok": False,
        "error": "delivery belongs to another store",
    }
