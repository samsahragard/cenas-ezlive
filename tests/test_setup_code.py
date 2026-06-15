"""Dual-channel PIN reset (Sam 2026-06-07): the Team Roster PIN-reset mints ONE
single-use EmployeeSetupToken that backs BOTH the emailed LINK and a short
MANAGER-DISPLAYED 6-digit CODE. Whichever the employee uses FIRST sets the PIN;
the OTHER stops working. No SMS (retired in the 2026-05-30 email pivot).

This suite proves the shared-token invariants:
  * reset returns a 6-digit setup_code;
  * FIRST-WINS A: completing via the LINK kills the CODE (same reset);
  * FIRST-WINS B: completing via the CODE kills the LINK (same reset);
  * a wrong code is rejected; after MAX_LOGIN_ATTEMPTS the token locks (429);
  * a NEW reset invalidates the prior unused token (old link + old code both die);
  * identifier scoping: employee A's code can NEVER set employee B's passcode.

Boot with ALLOW_DEV_SECRET=1 (set below) so create_app builds the dev secret.
Reuses the in-memory db_session fixture (tests/conftest.py) + the SessionLocal-
binding + partner-login pattern from tests/test_section_placement.py. The
completion paths go through a Flask test client (they open a real request context
+ set the employee session); the resolver invariants are checked directly on the
helpers (no request context needed)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

os.environ.setdefault("ALLOW_DEV_SECRET", "1")  # create_app needs a dev secret

import pytest
from werkzeug.security import generate_password_hash

from app.models import (Driver, Employee, EmployeePosition, EmployeeSetupToken,
                        EmployeeStoreAssignment, Position, User)
from app.web import employee_setup as setup_mod


# ============================================================
# Fixture: Flask app bound to the in-memory session (no SMTP)
# ============================================================
@pytest.fixture
def app_bound(db_session, monkeypatch):
    """create_app bound to the in-memory db_session across every module the
    setup/reset paths touch (incl. employee_setup, whose send_setup_invite opens
    its own session). SMTP is a no-op so the test is hermetic."""
    from app import create_app
    from app import db as appdb
    from app.web import schedules_v2 as sv2_mod
    from app.web import schedules_v2_roster as roster_mod
    from app.web import employee_auth as auth_mod
    from app.web import store_routes as store_mod
    from app.web import keypad_auth as keypad_mod

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    sess = lambda: db_session
    for mod in (appdb, sv2_mod, roster_mod, setup_mod, auth_mod, store_mod, keypad_mod):
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", sess, raising=False)
    import app.services.brief_email as be
    monkeypatch.setattr(be, "_smtp_send", lambda *a, **k: None, raising=False)
    return flask_app, db_session


def _make_employee(db, *, full_name="Test Emp", email="emp@test.local",
                   phone="2815550100"):
    """An active employee with a login email (send_setup_invite needs an email)."""
    e = Employee(full_name=full_name, email=email, phone=phone, active=True,
                 session_version=1)
    db.add(e)
    db.commit()
    return e


def _position_id(db, name):
    pos = db.query(Position).filter(Position.name == name).first()
    if pos is None:
        pos = Position(name=name, store_key=None)
        db.add(pos)
        db.flush()
    return pos.id


def _live_token(db, emp_id):
    """The single live (unused, newest) token row for an employee, else None."""
    return (db.query(EmployeeSetupToken)
              .filter(EmployeeSetupToken.employee_id == emp_id,
                      EmployeeSetupToken.used.is_(False))
              .order_by(EmployeeSetupToken.id.desc())
              .first())


# ============================================================
# 1. Reset returns a 6-digit code (and stores only its hash)
# ============================================================
def test_send_setup_invite_returns_six_digit_code(app_bound):
    _flask, db = app_bound
    emp = _make_employee(db)

    invite = setup_mod.send_setup_invite(emp.id)
    assert isinstance(invite, dict)
    assert set(invite) == {"token", "code"}
    code = invite["code"]
    assert code.isdigit() and len(code) == setup_mod.SETUP_CODE_LEN == 5

    # The raw code is NEVER stored: only sha256(code) lands in code_hash.
    row = _live_token(db, emp.id)
    assert row is not None
    assert row.code_hash == setup_mod._sha(code)
    assert row.code_hash != code
    assert row.code_attempts == 0


def test_reset_endpoint_includes_setup_code(app_bound):
    """POST .../reset-pin returns ok + a 6-digit setup_code for the manager."""
    flask_app, db = app_bound
    emp = _make_employee(db)
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s["partner_auth_ok"] = True
        s["auth_ok"] = True
        s["user_id"] = 1
        s["user_session_version"] = 1
    # Seed the partner User the require_level gate reads.
    db.add(User(id=1, full_name="P", email="p@test.local",
                passcode_hash=generate_password_hash("12345"),
                permission_level="partner", store_scope=None, active=True,
                first_login_done=True, session_version=1))
    db.commit()

    r = client.post("/dos/schedules-v2/employees/%d/reset-pin" % emp.id)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    assert body["setup_code"].isdigit() and len(body["setup_code"]) == 5


# ============================================================
# 2. FIRST-WINS A: LINK completes -> the CODE (same reset) dies
# ============================================================
def test_first_wins_link_then_code_dead(app_bound):
    flask_app, db = app_bound
    emp = _make_employee(db)
    invite = setup_mod.send_setup_invite(emp.id)
    token, code = invite["token"], invite["code"]

    client = flask_app.test_client()
    r = client.post("/employee/setup/%s/complete" % token,
                    json={"passcode": "13579", "phone": emp.phone,
                          "full_name": emp.full_name})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ok"] is True

    # The SAME row is consumed -> the code no longer resolves.
    e2, row2 = setup_mod._resolve_setup_by_code(db, emp.email, code)
    assert (e2, row2) == (None, None)


# ============================================================
# 3. FIRST-WINS B: CODE completes -> the LINK (same reset) dies
# ============================================================
def test_first_wins_code_then_link_dead(app_bound):
    flask_app, db = app_bound
    emp = _make_employee(db)
    invite = setup_mod.send_setup_invite(emp.id)
    token, code = invite["token"], invite["code"]

    client = flask_app.test_client()
    r = client.post("/employee/setup/code/complete",
                    json={"identifier": emp.email, "code": code,
                          "passcode": "24680", "phone": emp.phone,
                          "full_name": emp.full_name})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ok"] is True

    # The SAME row is consumed -> the link token no longer resolves.
    e2, row2 = setup_mod._resolve_setup_token(db, token)
    assert (e2, row2) == (None, None)
    # And a second link-complete is rejected (410 used/expired).
    r2 = client.post("/employee/setup/%s/complete" % token,
                     json={"passcode": "11111", "phone": emp.phone})
    assert r2.status_code == 410


# ============================================================
# 4. Wrong code rejected; after MAX attempts -> locked (429)
# ============================================================
def test_wrong_code_rejected_then_lockout(app_bound):
    flask_app, db = app_bound
    emp = _make_employee(db)
    invite = setup_mod.send_setup_invite(emp.id)
    good = invite["code"]
    bad = "000000" if good != "000000" else "111111"

    client = flask_app.test_client()
    # MAX_LOGIN_ATTEMPTS wrong guesses -> 410 each (generic), counter climbs.
    for _ in range(setup_mod.MAX_LOGIN_ATTEMPTS):
        r = client.post("/employee/setup/code/complete",
                        json={"identifier": emp.email, "code": bad,
                              "passcode": "13579", "phone": emp.phone})
        assert r.status_code == 410, r.get_data(as_text=True)

    row = _live_token(db, emp.id)
    assert row is not None and row.code_attempts >= setup_mod.MAX_LOGIN_ATTEMPTS

    # Now even the CORRECT code is locked out (429), and the resolver refuses it.
    r_locked = client.post("/employee/setup/code/complete",
                           json={"identifier": emp.email, "code": good,
                                 "passcode": "13579", "phone": emp.phone})
    assert r_locked.status_code == 429, r_locked.get_data(as_text=True)
    e2, row2 = setup_mod._resolve_setup_by_code(db, emp.email, good)
    assert (e2, row2) == (None, None)


def test_resolver_wrong_code_returns_none_increments(app_bound):
    """Direct-helper check: a wrong code returns (None,None) and bumps the counter;
    the right code still resolves while under the cap."""
    _flask, db = app_bound
    emp = _make_employee(db)
    invite = setup_mod.send_setup_invite(emp.id)
    good = invite["code"]
    bad = "999999" if good != "999999" else "888888"

    e1, r1 = setup_mod._resolve_setup_by_code(db, emp.email, bad)
    assert (e1, r1) == (None, None)
    assert _live_token(db, emp.id).code_attempts == 1
    # The right code resolves to the same employee/row while under the cap.
    e2, r2 = setup_mod._resolve_setup_by_code(db, emp.email, good)
    assert e2 is not None and e2.id == emp.id and r2 is not None


# ============================================================
# 5. A NEW reset invalidates the prior unused token (old link + old code both dead)
# ============================================================
def test_new_reset_invalidates_prior_token(app_bound):
    _flask, db = app_bound
    emp = _make_employee(db)
    first = setup_mod.send_setup_invite(emp.id)
    old_token, old_code = first["token"], first["code"]

    second = setup_mod.send_setup_invite(emp.id)
    new_token, new_code = second["token"], second["code"]
    assert new_token != old_token and new_code != old_code

    # Old link + old code both dead.
    assert setup_mod._resolve_setup_token(db, old_token) == (None, None)
    assert setup_mod._resolve_setup_by_code(db, emp.email, old_code) == (None, None)
    # Only the NEW reset is live.
    e_t, r_t = setup_mod._resolve_setup_token(db, new_token)
    assert e_t is not None and e_t.id == emp.id
    e_c, r_c = setup_mod._resolve_setup_by_code(db, emp.email, new_code)
    assert e_c is not None and e_c.id == emp.id and r_c.id == r_t.id
    # Exactly one unused row remains.
    unused = (db.query(EmployeeSetupToken)
                .filter(EmployeeSetupToken.employee_id == emp.id,
                        EmployeeSetupToken.used.is_(False))
                .count())
    assert unused == 1


# ============================================================
# 6. Identifier scoping: A's code cannot set B's passcode
# ============================================================
def test_code_is_identifier_scoped_no_cross_employee(app_bound):
    flask_app, db = app_bound
    emp_a = _make_employee(db, full_name="Alice A", email="alice@test.local",
                           phone="2815550111")
    emp_b = _make_employee(db, full_name="Bob B", email="bob@test.local",
                           phone="2815550222")
    invite_a = setup_mod.send_setup_invite(emp_a.id)
    code_a = invite_a["code"]
    setup_mod.send_setup_invite(emp_b.id)  # B has its own (different) reset

    # A's code under B's identifier must NOT resolve (no cross-employee use).
    e, row = setup_mod._resolve_setup_by_code(db, emp_b.email, code_a)
    assert (e, row) == (None, None)

    # And via the endpoint: A's code + B's identifier -> 410, B unchanged.
    client = flask_app.test_client()
    r = client.post("/employee/setup/code/complete",
                    json={"identifier": emp_b.email, "code": code_a,
                          "passcode": "13579", "phone": emp_b.phone})
    assert r.status_code == 410, r.get_data(as_text=True)
    # Re-query fresh (the resolver's attempt-counter commit expired the ORM
    # instances); B's PIN was never set by A's code.
    b_fresh = db.query(Employee).filter_by(id=emp_b.id).one()
    assert b_fresh.passcode_hash is None


# ============================================================
# 7. LOGIN WITH THE CODE (Sam 2026-06-07): the employee enters email/phone + the
#    manager's reset CODE at /employee/login/passcode and is signed in.
# ============================================================
def test_login_with_reset_code_signs_in_and_consumes_token(app_bound):
    """A valid reset code logs the employee in (no separate page needed), becomes
    their passcode, and consumes the shared token so the emailed link dies."""
    flask_app, db = app_bound
    emp = _make_employee(db, full_name="Cara C", email="cara@test.local",
                         phone="2815550133")
    code = setup_mod.send_setup_invite(emp.id)["code"]
    assert _live_token(db, emp.id) is not None  # a live token exists pre-login

    c = flask_app.test_client()
    r = c.post("/employee/login/passcode",
               json={"identifier": "cara@test.local", "passcode": code})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json().get("ok") is True

    # the shared token is now consumed -> the emailed setup link is dead (first-wins)
    assert _live_token(db, emp.id) is None
    # the code is now the employee's passcode -> logging in with it again works
    r2 = c.post("/employee/login/passcode",
                json={"identifier": "cara@test.local", "passcode": code})
    assert r2.status_code == 200


def test_employee_passcode_login_for_km_returns_manager_profile(app_bound):
    flask_app, db = app_bound
    emp = Employee(
        full_name="Gina KM",
        phone="5557771111",
        email="gina-session@test.local",
        active=True,
        session_version=1,
        passcode_hash=generate_password_hash("44556"),
    )
    db.add(emp)
    db.flush()
    km_id = _position_id(db, "KM")
    db.add(EmployeeStoreAssignment(employee_id=emp.id, store_key="tomball"))
    db.add(EmployeePosition(employee_id=emp.id, position_id=km_id, store_key="tomball"))
    db.commit()

    c = flask_app.test_client()
    r = c.post(
        "/employee/login/passcode",
        json={"identifier": "5557771111", "passcode": "44556"},
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ok"] is True
    assert r.get_json()["next"] == "/dos/today"
    with c.session_transaction() as s:
        assert s.get("user_id") is not None
        assert s.get("employee_id") is None
        assert s.get("driver_id") is None


def test_login_wrong_value_rejected(app_bound):
    """A value that is neither the passcode nor a valid reset code -> 401."""
    flask_app, db = app_bound
    emp = _make_employee(db, full_name="Dan D", email="dan@test.local",
                         phone="2815550144")
    setup_mod.send_setup_invite(emp.id)
    c = flask_app.test_client()
    r = c.post("/employee/login/passcode",
               json={"identifier": "dan@test.local", "passcode": "00000"})
    assert r.status_code == 401


# ============================================================
# 8. MANAGER reset (Sam 2026-06-07): a roster employee linked to a manager User
#    must get the code set as the passcode on the LINKED USER too, since managers
#    sign in via /keypad-login against User.passcode (not Employee.passcode).
# ============================================================
def test_reset_sets_code_as_passcode_on_employee_and_linked_user(app_bound):
    from werkzeug.security import check_password_hash
    flask_app, db = app_bound
    # partner (id=1) FIRST so it owns id=1 (the session below sets user_id=1).
    db.add(User(id=1, full_name="P", email="p@test.local",
                passcode_hash=generate_password_hash("12345"),
                permission_level="partner", store_scope=None, active=True,
                first_login_done=True, session_version=1))
    db.flush()
    mgr_user = User(full_name="Gina KM", email="ginakm@test.local",
                    phone="2815550199",
                    passcode_hash=generate_password_hash("11111"),
                    permission_level="km", store_scope="tomball", active=True,
                    first_login_done=True, session_version=1)
    db.add(mgr_user)
    db.flush()
    emp = Employee(full_name="Gina KM", email="ginakm@test.local",
                   phone="2815550199",
                   passcode_hash=generate_password_hash("11111"),
                   user_id=mgr_user.id, active=True, session_version=1)
    db.add(emp)
    db.commit()

    client = flask_app.test_client()
    with client.session_transaction() as s:
        s["partner_auth_ok"] = True
        s["auth_ok"] = True
        s["user_id"] = 1
        s["user_session_version"] = 1

    r = client.post("/dos/schedules-v2/employees/%d/reset-pin" % emp.id)
    assert r.status_code == 200, r.get_data(as_text=True)
    code = r.get_json()["setup_code"]

    u = db.query(User).filter_by(id=mgr_user.id).one()
    e = db.query(Employee).filter_by(id=emp.id).one()
    # the code is now the passcode on BOTH the linked User and the Employee...
    assert check_password_hash(u.passcode_hash, code), "code must work at keypad (User.passcode)"
    assert check_password_hash(e.passcode_hash, code)
    # ...and the OLD pin is dead (the reset actually reset).
    assert not check_password_hash(u.passcode_hash, "11111")


# ============================================================
# 9. KEYPAD login with the reset code (Sam 2026-06-07): a NO-User, never-set-up
#    employee (e.g. Gina) signs in at the NUMBER-PAD keypad with phone + the code.
# ============================================================
def test_keypad_login_with_reset_code_for_no_user_employee(app_bound):
    flask_app, db = app_bound
    db.add(User(id=1, full_name="P", email="p@test.local",
                passcode_hash=generate_password_hash("12345"),
                permission_level="partner", store_scope=None, active=True,
                first_login_done=True, session_version=1))
    # no User link, NO passcode (never completed setup) -- Gina's exact state.
    emp = Employee(full_name="Gina NoUser", email="ginanouser@test.local",
                   phone="4752769760", active=True, session_version=1)
    db.add(emp)
    db.commit()

    client = flask_app.test_client()
    with client.session_transaction() as s:
        s["partner_auth_ok"] = True
        s["auth_ok"] = True
        s["user_id"] = 1
        s["user_session_version"] = 1
    r = client.post("/dos/schedules-v2/employees/%d/reset-pin" % emp.id)
    assert r.status_code == 200, r.get_data(as_text=True)
    code = r.get_json()["setup_code"]

    # NUMBER-PAD keypad: phone + the reset code -> signed in (Path 4 code path).
    fresh = flask_app.test_client()
    r2 = fresh.post("/keypad-login", json={"phone": "4752769760", "pin": code})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert r2.get_json().get("ok") is True
    # a wrong value at the keypad is still rejected.
    bad = flask_app.test_client()
    r3 = bad.post("/keypad-login", json={"phone": "4752769760", "pin": "99999"})
    assert r3.status_code == 401


def test_keypad_employee_reset_code_works_when_driver_same_phone_is_locked(app_bound):
    """Gina regression: the main keypad must not let a stale/locked Driver row
    with the same phone block a valid Team Roster employee reset code."""
    flask_app, db = app_bound
    db.add(User(id=1, full_name="P", email="p@test.local",
                passcode_hash=generate_password_hash("12345"),
                permission_level="partner", store_scope=None, active=True,
                first_login_done=True, session_version=1))
    emp = Employee(full_name="Gina Paola Buritica Amaya",
                   email="gina.employee@test.local",
                   phone="4752769760", active=True, session_version=1)
    driver = Driver(name="Gina Driver", location="tomball", phone="(475) 276-9760",
                    active=True, status="active",
                    passcode_hash=generate_password_hash("22222"),
                    first_login_done=True, session_version=1,
                    failed_attempts=6,
                    lockout_until=datetime.utcnow() + timedelta(minutes=5))
    db.add_all([emp, driver])
    db.commit()

    client = flask_app.test_client()
    with client.session_transaction() as s:
        s["partner_auth_ok"] = True
        s["auth_ok"] = True
        s["user_id"] = 1
        s["user_session_version"] = 1
    r = client.post("/dos/schedules-v2/employees/%d/reset-pin" % emp.id)
    assert r.status_code == 200, r.get_data(as_text=True)
    code = r.get_json()["setup_code"]

    fresh = flask_app.test_client()
    r2 = fresh.post("/keypad-login", json={"phone": "4752769760", "pin": code})
    assert r2.status_code == 200, r2.get_data(as_text=True)
    assert r2.get_json().get("ok") is True
    with fresh.session_transaction() as s:
        assert s.get("employee_id") == emp.id
        assert s.get("driver_id") is None
