"""Owner view-login (Sam-directed): a shared phone + a per-employee 5-digit
code opens THAT employee's real portal. Regression coverage for the path in
app/web/keypad_auth.py (_handle_view_login + the login_submit intercept)."""
import hashlib
import importlib
import os
import tempfile

import pytest


def _prepare_temp_app(tmp):
    os.environ["ALLOW_DEV_SECRET"] = "1"
    os.environ["DATABASE_URL"] = "sqlite:///" + tmp.replace("\\", "/")
    import app.db as dbmod
    importlib.reload(dbmod)
    from app import create_app
    app = create_app()
    import app.web.keypad_auth as keypad_mod
    import app.web.employee_auth as emp_auth_mod
    importlib.reload(keypad_mod)
    importlib.reload(emp_auth_mod)
    keypad_mod.SessionLocal = dbmod.SessionLocal
    emp_auth_mod.SessionLocal = dbmod.SessionLocal
    return app, dbmod.SessionLocal


@pytest.fixture()
def app_emp():
    tmp = os.path.join(tempfile.gettempdir(), "_vl_pytest.db")
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    app, SessionLocal = _prepare_temp_app(tmp)
    from app.models import Employee
    db = SessionLocal()
    emp = Employee(full_name="Test Emp", active=True, session_version=1,
                   passcode_hash="x")
    db.add(emp)
    db.commit()
    eid = emp.id
    db.close()
    from app.web import keypad_auth
    # Map the test code "54321" -> our employee; reset throttle.
    keypad_auth._view_login_codes_cache = {
        hashlib.sha256(b"54321").hexdigest(): eid
    }
    keypad_auth._driver_view_login_codes_cache = {}
    keypad_auth._view_login_fails.clear()
    yield app, eid
    try:
        os.remove(tmp)
    except OSError:
        pass


def test_correct_code_opens_that_employee(app_emp):
    app, eid = app_emp
    c = app.test_client()
    r = c.post("/keypad-login", json={"phone": "5550000000", "pin": "54321"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    with c.session_transaction() as s:
        assert s.get("employee_id") == eid
        assert s.get("employee_name") == "Test Emp"
        assert s.get("auth_ok") is True
        # never a partner/owner session via the view-login
        assert s.get("partner_auth_ok") is None


def test_wrong_code_rejected_no_session(app_emp):
    app, _ = app_emp
    c = app.test_client()
    r = c.post("/keypad-login", json={"phone": "5550000000", "pin": "00000"})
    assert r.status_code == 401
    with c.session_transaction() as s:
        assert s.get("employee_id") is None


def test_brute_force_locks_out(app_emp):
    app, _ = app_emp
    c = app.test_client()
    last = None
    for _ in range(10):
        last = c.post("/keypad-login", json={"phone": "5550000000", "pin": "00001"})
    assert last.status_code == 429


def test_shared_phone_intercepts_before_normal_lookup(app_emp):
    # A normal (non-shared) phone must NOT hit the view-login resolver: bad
    # creds fall through to the normal driver/user path -> normal 401.
    app, _ = app_emp
    c = app.test_client()
    r = c.post("/keypad-login", json={"phone": "2815551234", "pin": "54321"})
    assert r.status_code == 401
    with c.session_transaction() as s:
        assert s.get("employee_id") is None


@pytest.fixture()
def app_linked_emp():
    tmp = os.path.join(tempfile.gettempdir(), "_vl_linked_pytest.db")
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    app, SessionLocal = _prepare_temp_app(tmp)
    from app.models import Employee, User
    db = SessionLocal()
    u = User(full_name="Mgr Partner", permission_level="partner", active=True,
             session_version=1, passcode_hash="x")
    db.add(u)
    db.commit()
    emp = Employee(full_name="Linked Emp", active=True, session_version=1,
                   passcode_hash="x", user_id=u.id)
    db.add(emp)
    db.commit()
    eid = emp.id
    db.close()
    from app.web import keypad_auth
    keypad_auth._view_login_codes_cache = {
        hashlib.sha256(b"77777").hexdigest(): eid
    }
    keypad_auth._driver_view_login_codes_cache = {}
    keypad_auth._view_login_fails.clear()
    yield app, eid
    try:
        os.remove(tmp)
    except OSError:
        pass


def test_linked_employee_opens_pure_employee_session(app_linked_emp):
    # A linked employee (Employee.user_id -> a manager User) must open as a PURE
    # employee session: the UNIFY manager-fold (user_id) is dropped so the owner
    # lands in the employee portal and never routes into /partner/* (the 403 the
    # owner hit). Lands on the dashboard, NOT the POST-only /employee/select-store.
    app, eid = app_linked_emp
    c = app.test_client()
    r = c.post("/keypad-login", json={"phone": "5550000000", "pin": "77777"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert r.get_json()["next"] == "/employee/dashboard"
    with c.session_transaction() as s:
        assert s.get("employee_id") == eid
        assert s.get("employee_name") == "Linked Emp"
        assert s.get("user_id") is None
        assert s.get("user_session_version") is None
        assert s.get("partner_auth_ok") is None


@pytest.fixture()
def app_driver_view():
    tmp = os.path.join(tempfile.gettempdir(), "_vl_driver_pytest.db")
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    app, SessionLocal = _prepare_temp_app(tmp)
    from app.models import Driver
    db = SessionLocal()
    driver = Driver(
        name="Driver One",
        location="tomball",
        active=True,
        status="active",
        session_version=4,
        first_login_done=False,
    )
    db.add(driver)
    db.commit()
    did = driver.id
    db.close()
    from app.web import keypad_auth
    keypad_auth._view_login_codes_cache = {}
    keypad_auth._driver_view_login_codes_cache = {
        hashlib.sha256(b"24680").hexdigest(): did
    }
    keypad_auth._view_login_fails.clear()
    yield app, did
    try:
        os.remove(tmp)
    except OSError:
        pass


def test_driver_view_login_opens_driver_profile_without_touching_real_pin(app_driver_view):
    app, did = app_driver_view
    c = app.test_client()
    with c.session_transaction() as s:
        s["employee_id"] = 99
        s["user_id"] = 88
        s["partner_auth_ok"] = True
    r = c.post("/keypad-login", json={"phone": "5550000000", "pin": "24680"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["next"] == "/my-profile"
    with c.session_transaction() as s:
        assert s.get("driver_id") == did
        assert s.get("driver_session_version") == 4
        assert s.get("driver_name") == "Driver One"
        assert s.get("auth_ok") is True
        assert s.get("employee_id") is None
        assert s.get("user_id") is None
        assert s.get("partner_auth_ok") is None


def test_employee_view_code_wins_if_driver_code_collides(app_emp):
    app, eid = app_emp
    from app.db import SessionLocal
    from app.models import Driver
    db = SessionLocal()
    driver = Driver(name="Collide Driver", location="tomball", active=True,
                    status="active", session_version=1)
    db.add(driver)
    db.commit()
    did = driver.id
    db.close()
    from app.web import keypad_auth
    same_hash = hashlib.sha256(b"54321").hexdigest()
    keypad_auth._driver_view_login_codes_cache = {same_hash: did}

    c = app.test_client()
    r = c.post("/keypad-login", json={"phone": "5550000000", "pin": "54321"})
    assert r.status_code == 200
    with c.session_transaction() as s:
        assert s.get("employee_id") == eid
        assert s.get("driver_id") is None
