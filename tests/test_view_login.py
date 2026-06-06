"""Owner view-login (Sam-directed): a shared phone + a per-employee 5-digit
code opens THAT employee's real portal. Regression coverage for the path in
app/web/keypad_auth.py (_handle_view_login + the login_submit intercept)."""
import hashlib
import os
import tempfile

import pytest


@pytest.fixture()
def app_emp():
    tmp = os.path.join(tempfile.gettempdir(), "_vl_pytest.db")
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    os.environ["ALLOW_DEV_SECRET"] = "1"
    os.environ["DATABASE_URL"] = "sqlite:///" + tmp.replace("\\", "/")
    from app import create_app
    app = create_app()
    from app.db import SessionLocal
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
    os.environ["ALLOW_DEV_SECRET"] = "1"
    os.environ["DATABASE_URL"] = "sqlite:///" + tmp.replace("\\", "/")
    from app import create_app
    app = create_app()
    from app.db import SessionLocal
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
        assert s.get("user_id") is None
        assert s.get("user_session_version") is None
        assert s.get("partner_auth_ok") is None
