"""S5 -- PLACEMENT drives PERMISSION (team-roster store-split, Sam).

When a manager adds/assigns a team member into a (store, section), the SAME
action that writes EmployeePosition(store_key) + EmployeeStoreAssignment also
pushes the DERIVED permission onto the linked User:

  * permission_level = the highest-ROLE_RANK role among the person's positions
    that map to a section (role_buckets.section_for_position + position_role);
  * store_scope      = derived from the EmployeeStoreAssignment set (single
    store -> that store; multiple -> the sorted CSV; corporate/partner -> NULL).

This suite has two layers (per the task's "test the pure helpers + at least one
endpoint path" guidance, since the full create_app boot is heavy):

  1. PURE / DB-light derivation:
       - _highest_section_role / _derive_store_scope (no DB);
       - apply_section_placement_to_user against an in-memory db_session
         (management -> management role; hourly -> hourly role with NO company
         escalation; no-linked-User -> no-op);
       - the tier guard rejecting a 3rd partner (assert_partner_change_allowed),
         which is exactly the guard the +Add endpoint calls before a partner
         placement.
  2. ONE endpoint path: POST /dos/schedules-v2/employees/add as a partner,
     asserting the EmployeePosition(store_key) + EmployeeStoreAssignment rows
     AND the rank-gate / section-mix rejections.

Boot with ALLOW_DEV_SECRET=1 (set in the module import guard below) so
create_app can construct the Flask secret in the endpoint layer.
"""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_DEV_SECRET", "1")  # create_app needs a dev secret

import pytest
from werkzeug.security import generate_password_hash

from app.models import (Employee, EmployeePosition, EmployeeStoreAssignment,
                        Position, User)
from app.web.schedules_v2 import (_derive_store_scope, _highest_section_role,
                                  apply_section_placement_to_user)


# ============================================================
# Layer 1a -- PURE derivation (no DB)
# ============================================================
def test_highest_section_role_management_outranks_hourly():
    """A person who is GM + Server derives the MANAGEMENT role (gm), not the
    hourly one -- highest ROLE_RANK wins."""
    assert _highest_section_role(["GM", "Server"]) == "gm"


def test_highest_section_role_km_beats_foh_manager():
    """Within management, KM (rank 70) beats FOH Manager (rank 50)."""
    assert _highest_section_role(["FOH Manager", "KM"]) == "km"


def test_highest_section_role_hourly_only():
    """Only-hourly positions derive an hourly role (ties break deterministically
    on the role key: cook < server)."""
    assert _highest_section_role(["Server", "Cook"]) == "cook"


def test_highest_section_role_ignores_tier_above_and_no_section():
    """Partner (tier-above -> section None) never becomes the derived level:
    Partner+GM -> gm. Expo is now a real management role (Sam 2026-06-07) -> expo."""
    assert _highest_section_role(["Partner", "GM"]) == "gm"
    assert _highest_section_role(["Expo"]) == "expo"
    assert _highest_section_role([]) is None


def test_derive_store_scope_single_multi_and_tier_above():
    """single store -> that store; multiple -> sorted CSV; partner/corporate ->
    NULL regardless of the assigned stores; no stores -> NULL."""
    assert _derive_store_scope(["tomball"], "gm") == "tomball"
    assert _derive_store_scope(["tomball", "copperfield"], "gm") == "copperfield,tomball"
    assert _derive_store_scope(["tomball"], "corporate") is None
    assert _derive_store_scope(["tomball", "copperfield"], "partner") is None
    assert _derive_store_scope([], "gm") is None


# ============================================================
# Layer 1b -- apply_section_placement_to_user against a real (in-memory) session
# ============================================================
def _seed_positions(db):
    """Seed the canonical positions used by these tests (store_key NULL = all
    store). Returns {name: Position}."""
    names = ["GM", "Server", "Cook", "Partner"]
    out = {}
    for nm in names:
        p = Position(name=nm, store_key=None)
        db.add(p)
        out[nm] = p
    db.flush()
    return out


def _emp_with_user(db, *, level="manager", store_scope=None, email="x@test.local"):
    """An Employee linked to a User (the system-access account)."""
    u = User(full_name="T U", email=email,
             passcode_hash=generate_password_hash("12345"),
             permission_level=level, store_scope=store_scope, active=True)
    db.add(u)
    db.flush()
    e = Employee(full_name="T U", email=email, active=True, user_id=u.id)
    db.add(e)
    db.flush()
    return e, u


def _place(db, emp, *, position_names, stores):
    """Write EmployeePosition + EmployeeStoreAssignment rows for the employee,
    mirroring what the endpoint writes before deriving the placement."""
    pos = {p.name: p for p in db.query(Position).all()}
    for sk in stores:
        db.add(EmployeeStoreAssignment(employee_id=emp.id, store_key=sk))
    for nm in position_names:
        for sk in stores:
            db.add(EmployeePosition(employee_id=emp.id,
                                    position_id=pos[nm].id, store_key=sk))
    db.flush()


def test_apply_placement_management_sets_user_level_and_scope(db_session):
    """A management placement (GM @ tomball) sets the linked User's
    permission_level=gm + store_scope=tomball."""
    _seed_positions(db_session)
    emp, user = _emp_with_user(db_session, level="manager", store_scope=None)
    _place(db_session, emp, position_names=["GM"], stores=["tomball"])

    level, scope = apply_section_placement_to_user(db_session, emp, actor=user)
    db_session.flush()
    assert (level, scope) == ("gm", "tomball")
    assert user.permission_level == "gm"
    assert user.store_scope == "tomball"


def test_apply_placement_hourly_sets_hourly_role_no_escalation(db_session):
    """An hourly placement (Server @ tomball) sets permission_level to the HOURLY
    role -- NOT a management/company level. store_scope follows the stores."""
    _seed_positions(db_session)
    emp, user = _emp_with_user(db_session, level="manager", store_scope=None)
    _place(db_session, emp, position_names=["Server"], stores=["tomball"])

    level, scope = apply_section_placement_to_user(db_session, emp, actor=user)
    db_session.flush()
    assert level == "server"
    assert scope == "tomball"
    assert user.permission_level == "server"
    # No company escalation: the derived level is exactly the hourly role, not a
    # manager tier. (The hourly self-only PERMS are locked by test_hourly_selfonly.)
    assert user.permission_level not in (
        "partner", "corporate", "corporate_chef", "gm", "km",
        "assistant_km", "foh_manager")


def test_apply_placement_multi_store_scope_is_csv(db_session):
    """A management placement across both stores derives the sorted-CSV scope."""
    _seed_positions(db_session)
    emp, user = _emp_with_user(db_session, level="manager", store_scope=None)
    _place(db_session, emp, position_names=["GM"], stores=["tomball", "copperfield"])

    level, scope = apply_section_placement_to_user(db_session, emp, actor=user)
    assert level == "gm"
    assert scope == "copperfield,tomball"


def test_apply_placement_no_linked_user_is_noop(db_session):
    """When the employee has NO linked User (invite/setup pending), placement is a
    no-op -- the derived role is carried by the EmployeePosition rows for the boot
    backfill, and nothing here invents a parallel mechanism."""
    _seed_positions(db_session)
    pos = {p.name: p for p in db_session.query(Position).all()}
    e = Employee(full_name="No User", email="nouser@test.local", active=True,
                 user_id=None)
    db_session.add(e)
    db_session.flush()
    db_session.add(EmployeeStoreAssignment(employee_id=e.id, store_key="tomball"))
    db_session.add(EmployeePosition(employee_id=e.id, position_id=pos["GM"].id,
                                    store_key="tomball"))
    db_session.flush()

    level, scope = apply_section_placement_to_user(db_session, e, actor=None)
    assert (level, scope) == (None, None)


def test_apply_placement_corporate_requires_both_stores(db_session):
    """The corporate guard fires through the placement path: a (hypothetical)
    corporate placement pinned to a single store raises TierInvariantError.

    Driven directly via the guard the endpoint calls -- canonical scheduling
    positions never derive 'corporate' (Corporate is tier-above -> section None),
    so this asserts the guard contract the endpoint relies on."""
    from app.services.tier_invariants import (TierInvariantError,
                                              assert_corporate_both_stores)
    with pytest.raises(TierInvariantError):
        assert_corporate_both_stores(
            {"permission_level": "corporate", "store_scope": "tomball"})
    # both-stores corporate passes
    assert_corporate_both_stores(
        {"permission_level": "corporate", "store_scope": None}) is None


# ============================================================
# Layer 1c -- the tier guard the +Add endpoint calls for a partner placement
# ============================================================
def test_tier_guard_rejects_third_partner():
    """assert_partner_change_allowed(create) rejects a target NOT on the fixed
    partner allow-list -- the exact guard sv2_employee_add runs before a partner
    placement, so a 3rd partner can never be created."""
    from app.services.tier_invariants import (TierInvariantError,
                                              assert_partner_change_allowed)
    actor = {"email": "samsahragard@gmail.com", "permission_level": "partner"}
    third = {"email": "stranger@example.com"}
    with pytest.raises(TierInvariantError):
        assert_partner_change_allowed(actor, third, "create")


def test_tier_guard_allows_pinned_partner(monkeypatch):
    """Sanity: a pinned identity (the escape-hatch allow-list) is allowed to be a
    partner -- the guard rejects ONLY non-pinned 3rd partners."""
    import app.services.tier_invariants as ti
    monkeypatch.setenv(ti.PARTNER_EMAILS_ENV,
                       "samsahragard@gmail.com,masood@cenaskitchen.com")
    ti.assert_partner_change_allowed(
        {"email": "samsahragard@gmail.com"},
        {"email": "masood@cenaskitchen.com"}, "create")  # no raise


# ============================================================
# Layer 2 -- ONE endpoint path: POST /dos/schedules-v2/employees/add
# ============================================================
@pytest.fixture
def app_with_partner(db_session, monkeypatch):
    """Flask app bound to the in-memory session, logged in as a partner. Mirrors
    tests/test_team_role_leak.py: seed a partner User, bind SessionLocal across
    every module the +Add path touches (incl. employee_setup, whose
    send_setup_invite opens its own session)."""
    from app import create_app
    from app import db as appdb
    from app.web import schedules_v2 as sv2_mod
    from app.web import schedules_v2_roster as roster_mod
    from app.web import employee_setup as setup_mod
    from app.web import permissions as perm_mod
    from app.web import store_routes as store_mod

    partner = User(id=1, full_name="test partner", email="partner@test.local",
                   passcode_hash=generate_password_hash("12345"),
                   permission_level="partner", store_scope=None, active=True,
                   first_login_done=True, session_version=1)
    db_session.add(partner)
    # Seed the canonical positions the +Add validates against.
    for nm in ["GM", "KM", "Server", "Cook"]:
        db_session.add(Position(name=nm, store_key=None))
    db_session.commit()

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    sess = lambda: db_session
    for mod in (appdb, sv2_mod, roster_mod, setup_mod, perm_mod, store_mod):
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", sess, raising=False)
    # send_setup_invite -> brief_email._smtp_send: make it a no-op so the test is
    # hermetic (the add succeeds regardless; the real code already logs on fail).
    import app.services.brief_email as be
    monkeypatch.setattr(be, "_smtp_send", lambda *a, **k: None, raising=False)
    return flask_app, db_session


def _partner_client(flask_app):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["partner_auth_ok"] = True
        s["auth_ok"] = True
        s["user_id"] = 1
        s["user_session_version"] = 1
    return c


def _pid(db, name):
    return db.query(Position).filter(Position.name == name).first().id


def test_endpoint_management_add_writes_position_store_and_user(app_with_partner):
    """Management +Add at a store -> EmployeePosition(store_key=that store) +
    EmployeeStoreAssignment for the new employee."""
    flask_app, db = app_with_partner
    client = _partner_client(flask_app)
    gm = _pid(db, "GM")

    r = client.post("/dos/schedules-v2/employees/add",
                    json={"full_name": "Gina GM", "email": "gina@test.local",
                          "store_keys": ["tomball"], "position_ids": [gm],
                          "section": "management"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    emp = db.query(Employee).filter(Employee.email == "gina@test.local").one()
    eps = db.query(EmployeePosition).filter_by(employee_id=emp.id).all()
    assert [(ep.position_id, ep.store_key) for ep in eps] == [(gm, "tomball")]
    sas = db.query(EmployeeStoreAssignment).filter_by(employee_id=emp.id).all()
    assert [sa.store_key for sa in sas] == ["tomball"]
    # New +Add employee has no linked User yet -> permission carried by the
    # EmployeePosition row for the boot backfill (no parallel mechanism).
    assert emp.user_id is None


def test_endpoint_add_reactivates_existing_employee_and_adds_management_role(app_with_partner):
    """A deactivated employee with the same email is the same person. +Add should
    reactivate and add the requested management placement instead of returning
    'employee with that email already exists'."""
    flask_app, db = app_with_partner
    client = _partner_client(flask_app)
    km = _pid(db, "KM")
    existing = Employee(full_name="Janeth Old", email="janeth@test.local",
                        phone="8325411871", active=False, session_version=3)
    db.add(existing)
    db.commit()

    r = client.post("/dos/schedules-v2/employees/add",
                    json={"full_name": "Janeth Arvizu Animas",
                          "email": "janeth@test.local",
                          "phone": "8325411871",
                          "assignments": [{"position_id": km, "store_key": "tomball"}],
                          "section": "management"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    assert body["employee_id"] == existing.id

    rows = db.query(Employee).filter(Employee.email == "janeth@test.local").all()
    assert len(rows) == 1
    emp = rows[0]
    assert emp.active is True
    assert emp.full_name == "Janeth Arvizu Animas"
    eps = db.query(EmployeePosition).filter_by(employee_id=emp.id).all()
    assert {(ep.position_id, ep.store_key) for ep in eps} == {(km, "tomball")}
    sas = db.query(EmployeeStoreAssignment).filter_by(employee_id=emp.id).all()
    assert {sa.store_key for sa in sas} == {"tomball"}


def test_endpoint_add_rejects_email_and_phone_matching_different_employees(app_with_partner):
    """Reactivation is only safe when the identity resolves to one Employee row."""
    flask_app, db = app_with_partner
    client = _partner_client(flask_app)
    km = _pid(db, "KM")
    db.add_all([
        Employee(full_name="Email Owner", email="janeth@test.local",
                 phone="1111111111", active=True, session_version=1),
        Employee(full_name="Phone Owner", email="other@test.local",
                 phone="8325411871", active=True, session_version=1),
    ])
    db.commit()

    r = client.post("/dos/schedules-v2/employees/add",
                    json={"full_name": "Janeth Arvizu Animas",
                          "email": "janeth@test.local",
                          "phone": "8325411871",
                          "assignments": [{"position_id": km, "store_key": "tomball"}],
                          "section": "management"})
    assert r.status_code == 409, r.get_data(as_text=True)
    assert "two different employees" in r.get_json()["error"]


def test_endpoint_hourly_add_writes_rows(app_with_partner):
    """Hourly +Add -> the same EmployeePosition(store_key) + EmployeeStoreAssignment
    writes; section accepted as hourly."""
    flask_app, db = app_with_partner
    client = _partner_client(flask_app)
    server = _pid(db, "Server")

    r = client.post("/dos/schedules-v2/employees/add",
                    json={"full_name": "Sam Server", "email": "ss@test.local",
                          "store_keys": ["tomball"], "position_ids": [server],
                          "section": "hourly"})
    assert r.status_code == 200, r.get_data(as_text=True)
    emp = db.query(Employee).filter(Employee.email == "ss@test.local").one()
    eps = db.query(EmployeePosition).filter_by(employee_id=emp.id).all()
    assert [(ep.position_id, ep.store_key) for ep in eps] == [(server, "tomball")]


def test_endpoint_rejects_mixed_section_add(app_with_partner):
    """A single +Add mixing a management + an hourly position is rejected 400
    (one +Add is one section) -- nothing is inserted."""
    flask_app, db = app_with_partner
    client = _partner_client(flask_app)
    gm, server = _pid(db, "GM"), _pid(db, "Server")

    r = client.post("/dos/schedules-v2/employees/add",
                    json={"full_name": "Mixed Person", "email": "mix@test.local",
                          "store_keys": ["tomball"], "position_ids": [gm, server]})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert db.query(Employee).filter(Employee.email == "mix@test.local").first() is None


def test_endpoint_rank_gate_rejects_at_or_above_actor(app_with_partner, monkeypatch):
    """The rank-gate rejects adding a role at/above the actor's own rank. A
    foh_manager actor (rank 50) cannot add a GM (rank 70). 403; no insert."""
    flask_app, db = app_with_partner
    # Demote the seeded actor to foh_manager so a GM add is over-rank.
    actor = db.query(User).filter(User.id == 1).first()
    actor.permission_level = "foh_manager"
    actor.store_scope = "tomball"
    db.commit()
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s["auth_ok"] = True
        s["user_id"] = 1
        s["user_session_version"] = 1
    gm = _pid(db, "GM")

    r = client.post("/dos/schedules-v2/employees/add",
                    json={"full_name": "Over Rank", "email": "over@test.local",
                          "store_keys": ["tomball"], "position_ids": [gm],
                          "section": "management"})
    assert r.status_code == 403, r.get_data(as_text=True)
    assert db.query(Employee).filter(Employee.email == "over@test.local").first() is None
