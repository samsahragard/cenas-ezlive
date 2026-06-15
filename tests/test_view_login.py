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


@pytest.fixture()
def app_expo_user():
    tmp = os.path.join(tempfile.gettempdir(), "_vl_expo_pytest.db")
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    app, SessionLocal = _prepare_temp_app(tmp)
    from app.models import User
    from werkzeug.security import generate_password_hash

    db = SessionLocal()
    user = User(
        full_name="Expo One",
        phone="5552223333",
        permission_level="expo",
        store_scope="tomball",
        active=True,
        session_version=1,
        first_login_done=True,
        passcode_hash=generate_password_hash("13579"),
    )
    db.add(user)
    db.commit()
    uid = user.id
    db.close()
    yield app, uid
    try:
        os.remove(tmp)
    except OSError:
        pass


@pytest.fixture()
def app_profile_choice(tmp_path):
    app, SessionLocal = _prepare_temp_app(str(tmp_path / "profile_choice.db"))
    yield app, SessionLocal


def test_manager_phone_login_beats_linked_employee_profile(app_profile_choice):
    app, SessionLocal = app_profile_choice
    from app.models import Employee, User
    from werkzeug.security import generate_password_hash

    db = SessionLocal()
    user = User(
        full_name="Britney Manager",
        phone="5553334444",
        permission_level="gm",
        store_scope="tomball",
        active=True,
        session_version=3,
        first_login_done=True,
        passcode_hash=generate_password_hash("24680"),
    )
    db.add(user)
    db.commit()
    emp = Employee(
        full_name="Britney Employee",
        phone="5553334444",
        active=True,
        session_version=5,
        passcode_hash=generate_password_hash("24680"),
        user_id=user.id,
    )
    db.add(emp)
    db.commit()
    uid = user.id
    db.close()

    c = app.test_client()
    r = c.post("/keypad-login", json={"phone": "5553334444", "pin": "24680"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["next"] == "/dos/today"
    with c.session_transaction() as s:
        assert s.get("user_id") == uid
        assert s.get("employee_id") is None
        assert s.get("driver_id") is None


def test_manager_driver_phone_login_returns_profile_choices(app_profile_choice):
    app, SessionLocal = app_profile_choice
    from app.models import Driver, User
    from werkzeug.security import generate_password_hash

    db = SessionLocal()
    user = User(
        full_name="Tomball GM",
        phone="5554445555",
        permission_level="gm",
        store_scope="tomball",
        active=True,
        session_version=2,
        first_login_done=True,
        passcode_hash=generate_password_hash("13579"),
    )
    driver = Driver(
        name="Tomball GM",
        location="tomball",
        phone="5554445555",
        active=True,
        status="active",
        session_version=7,
        first_login_done=True,
        passcode_hash=generate_password_hash("13579"),
    )
    db.add_all([user, driver])
    db.commit()
    db.close()

    c = app.test_client()
    r = c.post("/keypad-login", json={"phone": "5554445555", "pin": "13579"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["choose_profile"] is True
    assert [choice["profile"] for choice in body["choices"]] == ["user", "driver"]
    assert [choice["label"] for choice in body["choices"]] == ["GM", "Driver"]
    with c.session_transaction() as s:
        assert s.get("user_id") is None
        assert s.get("driver_id") is None
        assert s.get("employee_id") is None


def test_manager_driver_profile_choice_finalizes_selected_session(app_profile_choice):
    app, SessionLocal = app_profile_choice
    from app.models import Driver, User
    from werkzeug.security import generate_password_hash

    db = SessionLocal()
    user = User(
        full_name="Dual Role",
        phone="5556667777",
        permission_level="foh_manager",
        store_scope="copperfield",
        active=True,
        session_version=4,
        first_login_done=True,
        passcode_hash=generate_password_hash("86420"),
    )
    driver = Driver(
        name="Dual Role",
        location="copperfield",
        phone="5556667777",
        active=True,
        status="active",
        session_version=8,
        first_login_done=True,
        passcode_hash=generate_password_hash("86420"),
    )
    db.add_all([user, driver])
    db.commit()
    uid = user.id
    did = driver.id
    db.close()

    manager_client = app.test_client()
    r = manager_client.post(
        "/keypad-login",
        json={"phone": "5556667777", "pin": "86420", "profile": "user"},
    )
    assert r.status_code == 200
    assert r.get_json()["next"] == "/uno/today"
    with manager_client.session_transaction() as s:
        assert s.get("user_id") == uid
        assert s.get("driver_id") is None
        assert s.get("employee_id") is None

    driver_client = app.test_client()
    r = driver_client.post(
        "/keypad-login",
        json={"phone": "5556667777", "pin": "86420", "profile": "driver"},
    )
    assert r.status_code == 200
    assert r.get_json()["next"] == "/my-profile"
    with driver_client.session_transaction() as s:
        assert s.get("driver_id") == did
        assert s.get("user_id") is None
        assert s.get("employee_id") is None


def _position_id(db, name):
    from app.models import Position

    pos = db.query(Position).filter(Position.name == name).first()
    if pos is None:
        pos = Position(name=name, store_key=None)
        db.add(pos)
        db.flush()
    return pos.id


def test_unlinked_km_employee_logs_in_as_manager_profile(app_profile_choice):
    app, SessionLocal = app_profile_choice
    from app.models import Employee, EmployeePosition, EmployeeStoreAssignment, User
    from werkzeug.security import generate_password_hash

    db = SessionLocal()
    emp = Employee(
        full_name="Janet KM",
        phone="5557778888",
        email="janetkm@test.local",
        active=True,
        session_version=1,
        passcode_hash=generate_password_hash("11223"),
    )
    db.add(emp)
    db.flush()
    km_id = _position_id(db, "KM")
    db.add(EmployeeStoreAssignment(employee_id=emp.id, store_key="tomball"))
    db.add(EmployeePosition(employee_id=emp.id, position_id=km_id, store_key="tomball"))
    db.commit()
    emp_id = emp.id
    db.close()

    c = app.test_client()
    r = c.post("/keypad-login", json={"phone": "5557778888", "pin": "11223"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["next"] == "/dos/today"
    with c.session_transaction() as s:
        assert s.get("user_id") is not None
        assert s.get("employee_id") is None
        assert s.get("driver_id") is None

    db = SessionLocal()
    try:
        emp = db.get(Employee, emp_id)
        user = db.get(User, emp.user_id)
        assert user is not None
        assert user.permission_level == "km"
        assert user.store_scope == "tomball"
        assert user.phone == "5557778888"
    finally:
        db.close()


def test_unlinked_km_employee_with_driver_gets_profile_choice(app_profile_choice):
    app, SessionLocal = app_profile_choice
    from app.models import Driver, Employee, EmployeePosition, EmployeeStoreAssignment, User
    from werkzeug.security import generate_password_hash

    db = SessionLocal()
    emp = Employee(
        full_name="Gina KM",
        phone="5558889999",
        email="ginakm2@test.local",
        active=True,
        session_version=1,
        passcode_hash=generate_password_hash("33445"),
    )
    db.add(emp)
    db.flush()
    km_id = _position_id(db, "KM")
    db.add(EmployeeStoreAssignment(employee_id=emp.id, store_key="tomball"))
    db.add(EmployeePosition(employee_id=emp.id, position_id=km_id, store_key="tomball"))
    driver = Driver(
        name="Gina KM",
        location="tomball",
        phone="5558889999",
        active=True,
        status="active",
        session_version=9,
        first_login_done=True,
        passcode_hash=generate_password_hash("33445"),
    )
    db.add(driver)
    db.commit()
    emp_id = emp.id
    driver_id = driver.id
    db.close()

    c = app.test_client()
    r = c.post("/keypad-login", json={"phone": "5558889999", "pin": "33445"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["choose_profile"] is True
    assert [choice["profile"] for choice in body["choices"]] == ["user", "driver"]
    with c.session_transaction() as s:
        assert s.get("user_id") is None
        assert s.get("driver_id") is None
        assert s.get("employee_id") is None

    db = SessionLocal()
    try:
        emp = db.get(Employee, emp_id)
        user = db.get(User, emp.user_id)
        assert user is not None
        assert user.permission_level == "km"
    finally:
        db.close()

    r = c.post(
        "/keypad-login",
        json={"phone": "5558889999", "pin": "33445", "profile": "user"},
    )
    assert r.status_code == 200
    assert r.get_json()["next"] == "/dos/today"
    with c.session_transaction() as s:
        assert s.get("user_id") is not None
        assert s.get("driver_id") is None
        assert s.get("employee_id") is None

    driver_client = app.test_client()
    r = driver_client.post(
        "/keypad-login",
        json={"phone": "5558889999", "pin": "33445", "profile": "driver"},
    )
    assert r.status_code == 200
    assert r.get_json()["next"] == "/my-profile"
    with driver_client.session_transaction() as s:
        assert s.get("driver_id") == driver_id
        assert s.get("user_id") is None
        assert s.get("employee_id") is None


def test_store_root_next_lands_on_permitted_today_page(app_expo_user):
    app, _ = app_expo_user
    c = app.test_client()
    r = c.post(
        "/keypad-login",
        json={"phone": "5552223333", "pin": "13579", "next": "/dos/"},
    )
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert r.get_json()["next"] == "/dos/today"


def test_default_store_landing_uses_today_page():
    from app.models import User
    from app.web.keypad_auth import _landing_for_user

    user = User(permission_level="expo", store_scope="tomball")
    assert _landing_for_user(user) == "/dos/today"


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
