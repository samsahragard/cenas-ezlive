from __future__ import annotations

from werkzeug.security import generate_password_hash


def _app_with_auth_db(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb
    from app.web import employee_auth as employee_mod
    from app.web import keypad_auth as keypad_mod

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(keypad_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(employee_mod, "SessionLocal", lambda: db_session)
    return flask_app


def _seed_employee_driver(
    db_session,
    *,
    phone="7133333333",
    employee_pin="33333",
    driver_pin="33333",
):
    from app.models import Driver, Employee, EmployeePosition, Position

    emp = Employee(
        id=7,
        full_name="Yadira Test",
        phone=phone,
        active=True,
        passcode_hash=generate_password_hash(employee_pin),
        session_version=1,
    )
    pos = Position(id=4, name="Server", store_key="tomball")
    emp_pos = EmployeePosition(employee_id=7, position_id=4, store_key="tomball")
    driver = Driver(
        id=3,
        name="Yadira Test",
        location="tomball",
        phone=phone,
        email="yadira-driver@test.local",
        active=True,
        status="active",
        passcode_hash=generate_password_hash(driver_pin),
        first_login_done=True,
        session_version=1,
    )
    db_session.add_all([emp, pos, emp_pos, driver])
    db_session.commit()
    return emp, driver


def _link_corporate_user(db_session, *, phone="7133333333", pin="33333"):
    from app.models import Employee, User

    user = User(
        id=11,
        full_name="Yadira Manager",
        phone=phone,
        email="yadira-manager@test.local",
        active=True,
        first_login_done=True,
        permission_level="corporate",
        store_scope=None,
        passcode_hash=generate_password_hash(pin),
        session_version=1,
    )
    employee = db_session.get(Employee, 7)
    employee.user_id = 11
    db_session.add(user)
    db_session.commit()
    return user


def test_same_employee_driver_phone_gets_role_picker(db_session, monkeypatch):
    _seed_employee_driver(db_session)
    app = _app_with_auth_db(db_session, monkeypatch)
    client = app.test_client()

    response = client.post(
        "/keypad-login",
        json={"phone": "713-333-3333", "pin": "33333"},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["needs_account_pick"] is True
    labels = {choice["label"] for choice in data["choices"]}
    assert labels == {"Driver", "Tomball"}

    with client.session_transaction() as sess:
        assert sess.get("driver_id") is None
        assert sess.get("employee_id") is None

    driver_token = next(c["token"] for c in data["choices"] if c["label"] == "Driver")
    selected = client.post("/keypad-login/select-account", json={"token": driver_token})
    assert selected.status_code == 200
    assert selected.get_json()["next"] == "/my-profile"
    with client.session_transaction() as sess:
        assert sess["driver_id"] == 3
        assert sess.get("employee_id") is None
        assert sess["auth_ok"] is True


def test_picker_keeps_private_account_ids_server_side(db_session, monkeypatch):
    _seed_employee_driver(db_session)
    app = _app_with_auth_db(db_session, monkeypatch)
    client = app.test_client()

    response = client.post(
        "/keypad-login",
        json={"phone": "7133333333", "pin": "33333"},
    )

    data = response.get_json()
    assert data["needs_account_pick"] is True
    assert all("id" not in choice for choice in data["choices"])
    assert all("store_key" not in choice for choice in data["choices"])
    with client.session_transaction() as sess:
        assert sess.get("login_account_pick_id")
        assert "login_account_choices" not in sess


def test_picker_employee_choice_sets_active_store_and_clears_driver(db_session, monkeypatch):
    _seed_employee_driver(db_session)
    app = _app_with_auth_db(db_session, monkeypatch)
    client = app.test_client()

    login = client.post(
        "/keypad-login",
        json={"phone": "7133333333", "pin": "33333"},
    ).get_json()
    tomball_token = next(c["token"] for c in login["choices"] if c["label"] == "Tomball")

    selected = client.post("/keypad-login/select-account", json={"token": tomball_token})

    assert selected.status_code == 200
    assert selected.get_json()["next"] == "/employee/dashboard"
    with client.session_transaction() as sess:
        assert sess["employee_id"] == 7
        assert sess["active_store"] == "tomball"
        assert sess.get("user_id") is None
        assert sess.get("driver_id") is None
        assert sess["auth_ok"] is True


def test_employee_choice_does_not_fold_linked_corporate_user(db_session, monkeypatch):
    _seed_employee_driver(db_session)
    _link_corporate_user(db_session)
    app = _app_with_auth_db(db_session, monkeypatch)
    client = app.test_client()

    login = client.post(
        "/keypad-login",
        json={"phone": "7133333333", "pin": "33333"},
    ).get_json()
    labels = {choice["label"] for choice in login["choices"]}
    assert {"Driver", "Tomball", "Corporate"} == labels
    tomball_token = next(c["token"] for c in login["choices"] if c["label"] == "Tomball")

    selected = client.post("/keypad-login/select-account", json={"token": tomball_token})

    assert selected.status_code == 200
    assert selected.get_json()["next"] == "/employee/dashboard"
    with client.session_transaction() as sess:
        assert sess["employee_id"] == 7
        assert sess["active_store"] == "tomball"
        assert sess.get("user_id") is None
        assert sess.get("partner_auth_ok") is None


def test_driver_profile_no_longer_shadows_employee_login_when_driver_pin_differs(
    db_session,
    monkeypatch,
):
    _emp, driver = _seed_employee_driver(
        db_session,
        employee_pin="33333",
        driver_pin="44444",
    )
    app = _app_with_auth_db(db_session, monkeypatch)
    client = app.test_client()

    response = client.post(
        "/keypad-login",
        json={"phone": "7133333333", "pin": "33333"},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["next"] == "/employee/dashboard"
    assert "needs_account_pick" not in data
    assert driver.failed_attempts == 0
    with client.session_transaction() as sess:
        assert sess["employee_id"] == 7
        assert sess.get("driver_id") is None


def test_direct_driver_login_clears_stale_employee_session(db_session, monkeypatch):
    _seed_employee_driver(db_session)
    app = _app_with_auth_db(db_session, monkeypatch)
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["employee_id"] = 7
        sess["employee_session_version"] = 1
        sess["active_store"] = "tomball"
        sess["auth_ok"] = True

    response = client.post(
        "/driver/login",
        json={"phone": "7133333333", "pin": "33333"},
    )

    assert response.status_code == 200
    assert response.get_json()["next"] == "/driver/logs"
    with client.session_transaction() as sess:
        assert sess["driver_id"] == 3
        assert sess.get("employee_id") is None
        assert sess.get("employee_session_version") is None
        assert sess.get("active_store") is None
        assert sess["auth_ok"] is True


def test_pending_picker_token_rejected_after_direct_driver_login(db_session, monkeypatch):
    _seed_employee_driver(db_session)
    app = _app_with_auth_db(db_session, monkeypatch)
    client = app.test_client()

    login = client.post(
        "/keypad-login",
        json={"phone": "7133333333", "pin": "33333"},
    ).get_json()
    tomball_token = next(c["token"] for c in login["choices"] if c["label"] == "Tomball")
    with client.session_transaction() as sess:
        sess["auth_ok"] = True

    direct = client.post(
        "/driver/login",
        json={"phone": "7133333333", "pin": "33333"},
    )
    assert direct.status_code == 200
    with client.session_transaction() as sess:
        assert sess["driver_id"] == 3
        assert "login_account_pick_id" not in sess

    stale = client.post("/keypad-login/select-account", json={"token": tomball_token})

    assert stale.status_code == 401
    with client.session_transaction() as sess:
        assert sess["driver_id"] == 3
        assert sess.get("employee_id") is None


def test_pending_picker_token_rejected_after_session_version_change(db_session, monkeypatch):
    _seed_employee_driver(db_session)
    app = _app_with_auth_db(db_session, monkeypatch)
    client = app.test_client()

    login = client.post(
        "/keypad-login",
        json={"phone": "7133333333", "pin": "33333"},
    ).get_json()
    tomball_token = next(c["token"] for c in login["choices"] if c["label"] == "Tomball")

    from app.models import Employee
    employee = db_session.get(Employee, 7)
    employee.session_version = 2
    db_session.commit()

    selected = client.post("/keypad-login/select-account", json={"token": tomball_token})

    assert selected.status_code == 401
    with client.session_transaction() as sess:
        assert sess.get("employee_id") is None
        assert sess.get("driver_id") is None


def test_driver_choice_ignores_employee_next_target(db_session, monkeypatch):
    _seed_employee_driver(db_session)
    app = _app_with_auth_db(db_session, monkeypatch)
    client = app.test_client()

    login = client.post(
        "/keypad-login",
        json={
            "phone": "7133333333",
            "pin": "33333",
            "next": "/employee/dashboard",
        },
    ).get_json()
    driver_token = next(c["token"] for c in login["choices"] if c["label"] == "Driver")

    selected = client.post("/keypad-login/select-account", json={"token": driver_token})

    assert selected.status_code == 200
    assert selected.get_json()["next"] == "/my-profile"
    with client.session_transaction() as sess:
        assert sess["driver_id"] == 3
        assert sess.get("employee_id") is None


def test_symbol_pin_rejected_by_unified_and_direct_driver_login(db_session, monkeypatch):
    _seed_employee_driver(
        db_session,
        employee_pin="12#45",
        driver_pin="12#45",
    )
    app = _app_with_auth_db(db_session, monkeypatch)
    client = app.test_client()

    unified = client.post(
        "/keypad-login",
        json={"phone": "7133333333", "pin": "12#45"},
    )
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
    direct = client.post(
        "/driver/login",
        json={"phone": "7133333333", "pin": "12#45"},
    )

    assert unified.status_code == 401
    assert direct.status_code == 401
