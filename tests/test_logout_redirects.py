from __future__ import annotations

import pytest


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    from app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    return app


def _assert_phone_login_redirect(resp):
    location = resp.headers.get("Location", "")
    assert resp.status_code in (301, 302)
    assert "/keypad-login" in location
    assert "_clear=1" in location
    assert "/partner-login" not in location


def test_keypad_logout_clears_site_auth_and_returns_phone_login(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth_ok"] = True
        sess["partner_auth_ok"] = True

    resp = c.get("/keypad-logout")
    _assert_phone_login_redirect(resp)
    with c.session_transaction() as sess:
        assert "auth_ok" not in sess
        assert "partner_auth_ok" not in sess


def test_legacy_logout_returns_phone_login_not_shared_password(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth_ok"] = True
        sess["partner_auth_ok"] = True

    resp = c.get("/logout")
    _assert_phone_login_redirect(resp)
    with c.session_transaction() as sess:
        assert "auth_ok" not in sess
        assert "partner_auth_ok" not in sess


def test_driver_logout_clears_site_auth_and_returns_phone_login(app, db_session, monkeypatch):
    from app.models import Driver

    driver = Driver(
        id=44,
        name="Driver",
        location="tomball",
        active=True,
        session_version=1,
    )
    db_session.add(driver)
    db_session.commit()
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)

    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth_ok"] = True
        sess["driver_id"] = 44
        sess["driver_name"] = "Driver"
        sess["driver_session_version"] = 1

    resp = c.get("/driver/logout")
    _assert_phone_login_redirect(resp)
    with c.session_transaction() as sess:
        assert "auth_ok" not in sess
        assert "driver_id" not in sess
        assert "driver_name" not in sess
        assert "driver_session_version" not in sess


def test_auth_only_root_goes_to_phone_login_not_partner_gate(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth_ok"] = True

    resp = c.get("/")
    _assert_phone_login_redirect(resp)
