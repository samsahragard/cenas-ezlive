from __future__ import annotations

from werkzeug.security import generate_password_hash


def test_profile_lab_is_explicit_corporate_read_only_surface():
    init_source = open("app/__init__.py", encoding="utf-8").read()
    route_source = open("app/web/corporate_profile_lab.py", encoding="utf-8").read()

    assert "profile_lab_bp" in init_source
    assert '"/partner/profile-lab"' in route_source
    assert '"/corporate/profile-lab"' in route_source
    assert '@require_level("corporate")' in route_source
    assert "methods=[\"POST\"]" not in route_source
    assert "session[\"employee_id\"]" not in route_source
    assert "session[\"driver_id\"]" not in route_source
    assert "from app.web.driver_system" not in route_source
    assert "require_driver" not in route_source
    assert "driver_system_bp" not in route_source


def test_profile_lab_templates_do_not_expose_auth_secret_fields():
    for path in (
        "app/templates/corporate_profile_lab.html",
        "app/templates/corporate_profile_lab_detail.html",
    ):
        template = open(path, encoding="utf-8").read()
        for forbidden in (
            "passcode",
            "password_hash",
            "passcode_hash",
            "toast_id",
            "eligible_sales",
            "cashSales",
            "nonCashSales",
            "GUID",
            "guid",
            "address",
        ):
            assert forbidden not in template


def test_profile_lab_routes_do_not_switch_employee_or_driver_sessions(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb
    from app.models import Driver, Employee, EmployeeStoreAssignment, User
    from app.web import corporate_profile_lab as lab_mod
    from app.web import employee_my_profile_page as profile_mod
    from app.web import keypad_auth as keypad_mod

    partner = User(
        id=1,
        full_name="test partner",
        email="partner@test.local",
        passcode_hash=generate_password_hash("12345"),
        permission_level="partner",
        store_scope=None,
        active=True,
        first_login_done=True,
        session_version=1,
    )
    emp = Employee(id=7, full_name="Yadira Test", active=True)
    store = EmployeeStoreAssignment(employee_id=7, store_key="tomball")
    driver = Driver(id=3, name="Driver Test", location="tomball", active=True, status="active")
    db_session.add_all([partner, emp, store, driver])
    db_session.commit()

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(lab_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(profile_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(keypad_mod, "SessionLocal", lambda: db_session)

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["partner_auth_ok"] = True
        sess["auth_ok"] = True
        sess["user_id"] = 1
        sess["user_session_version"] = 1

    assert client.get("/partner/profile-lab").status_code == 200
    assert client.get("/partner/profile-lab/employee/7").status_code == 200
    assert client.get("/partner/profile-lab/driver/3").status_code == 200

    with client.session_transaction() as sess:
        assert sess.get("employee_id") is None
        assert sess.get("driver_id") is None
        assert sess.get("user_id") == 1
