from __future__ import annotations

import os

os.environ.setdefault("ALLOW_DEV_SECRET", "1")

import pytest

from app.models import DriverApplication, Order, User
from app.web import driver_system as driver_mod


@pytest.fixture
def driverapp_bound(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb
    from app.web import keypad_auth
    from app.web import store_routes as store_mod

    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(driver_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(keypad_auth, "SessionLocal", lambda: db_session)

    def _get_db():
        yield db_session

    monkeypatch.setattr(store_mod, "get_db", _get_db)

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app, db_session


def _seed_order(db, *, ext="DRV-APP-1", address="123 Main St, Houston, TX 77095"):
    order = Order(
        external_order_id=ext,
        status="available",
        origin_store_id="store_1",
        reported_store_id="store_1",
        pickup_kitchen="Copperfield",
        delivery_date="2099-01-01",
        deliver_at="10:45 AM",
        delivery_address=address,
        pickup_miles=28.0,
        headcount=20,
    )
    db.add(order)
    db.commit()
    return order


def _seed_partner(db):
    user = User(
        full_name="Sam",
        email="sam@test.local",
        phone="5550000001",
        passcode_hash="x",
        permission_level="partner",
        active=True,
        first_login_done=True,
        session_version=1,
    )
    db.add(user)
    db.commit()
    return user


def _partner_client(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
        sess["partner_auth_ok"] = True
        sess["user_id"] = user.id
        sess["user_session_version"] = user.session_version
    return client


def _application_payload(location="both", first_name="Maria"):
    return {
        "first_name": first_name,
        "last_name": "Gonzalez",
        "phone": "7135550101",
        "whatsapp": "7135550101",
        "email": f"{first_name.lower()}@example.com",
        "zip_code": "77095",
        "preferred_location": location,
        "available_days": ["Mon", "Wed", "Fri"],
        "shift_preference": "Flexible",
        "has_license": "Yes",
        "has_vehicle": "Yes",
        "has_insurance": "Yes",
        "has_smartphone": "Yes",
        "delivery_experience": "No",
        "notes": "Available for catering deliveries.",
        "consent": "1",
    }


def test_driverapp_public_page_shows_live_deliveries_without_login(driverapp_bound):
    flask_app, db = driverapp_bound
    _seed_order(db, address="123 Main St, Houston, TX 77095")

    resp = flask_app.test_client().get("/driverapp")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Drive <em>smart</em>, get paid <em>right</em>." in html
    assert "On-Time Performance Bonus" in html
    assert "Used for delivery photo uploads" in html
    assert "Tracking bonus" not in html
    assert "Deliveries Live Right Now" in html
    assert "10:45 AM" in html
    assert "$51" in html
    assert "Houston, TX 77095" in html
    assert "123 Main St" not in html
    assert "Submit Application" in html


def test_driverapp_application_submit_creates_application(driverapp_bound):
    flask_app, db = driverapp_bound

    resp = flask_app.test_client().post("/driverapp", data=_application_payload("copperfield"))

    assert resp.status_code == 302
    row = db.query(DriverApplication).one()
    assert row.full_name == "Maria Gonzalez"
    assert row.preferred_location == "copperfield"
    assert row.available_days == ["Mon", "Wed", "Fri"]
    assert row.whatsapp == "7135550101"
    assert row.consent is True


def test_driverapp_application_requires_whatsapp(driverapp_bound):
    flask_app, db = driverapp_bound
    payload = _application_payload("copperfield")
    payload["whatsapp"] = ""

    resp = flask_app.test_client().post("/driverapp", data=payload)

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "WhatsApp number is required." in html
    assert db.query(DriverApplication).count() == 0


def test_driver_admin_applications_tab_scopes_both_to_each_store(driverapp_bound):
    flask_app, db = driverapp_bound
    user = _seed_partner(db)
    db.add_all([
        DriverApplication(
            first_name="Both",
            last_name="Driver",
            full_name="Both Driver",
            phone="7135550001",
            email="both@example.com",
            preferred_location="both",
            available_days=["Mon"],
            consent=True,
        ),
        DriverApplication(
            first_name="Copper",
            last_name="Field",
            full_name="Copper Field",
            phone="7135550003",
            email="copper@example.com",
            preferred_location="copperfield",
            available_days=["Wed"],
            consent=True,
        ),
        DriverApplication(
            first_name="Tomball",
            last_name="Only",
            full_name="Tomball Only",
            phone="7135550002",
            email="tomball@example.com",
            preferred_location="tomball",
            available_days=["Tue"],
            consent=True,
        ),
    ])
    db.commit()
    client = _partner_client(flask_app, user)

    copperfield_html = client.get("/uno/drivers?status=applications").get_data(as_text=True)
    tomball_html = client.get("/dos/drivers?status=applications").get_data(as_text=True)

    assert "Applications" in copperfield_html
    assert "Both Driver" in copperfield_html
    assert "Either / Both" in copperfield_html
    assert "Copper Field" in copperfield_html
    assert "Tomball Only" not in copperfield_html
    assert "Both Driver" in tomball_html
    assert "Tomball Only" in tomball_html
    assert "Copper Field" not in tomball_html
