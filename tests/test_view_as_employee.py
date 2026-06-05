"""Tests for the owner-only, READ-ONLY EMPLOYEE "View as" swap (phase-2).

Feature under test (app/web/view_as_routes.py):
  * POST /view-as/employee/<id>  -- owner-gated (REAL partner). Anchors the real
    owner (view_as_owner_uid + _sv + view_as_kind), POPS the owner's user_id /
    user_session_version, and SWAPS the session to BE the target employee
    (session['employee_id'] = target, employee_session_version, auth_ok), then
    302 -> /employee/my-profile. Missing/inactive target -> 404, no swap.
  * permissions.load_current_user -- with user_id popped it recovers the real
    owner from view_as_owner_uid (fail-closed: owner must still exist, be active,
    match the stashed session_version, AND still be a partner) and sets
    g.viewing_as = True / g.real_user = owner. g.current_user is NOT swapped
    (the employee routes read session['employee_id']).
  * The SQLAlchemy before_flush guard (keyed on g.viewing_as) stays armed across
    the swap, so any write attempt while employee-view-as is 403'd (read-only).
  * GET|POST /view-as/stop -- restores session['user_id'] = owner and clears the
    swapped employee identity (employee_id et al).
  * view_as_banner context value names the swapped EMPLOYEE while active.

Harness mirrors tests/test_view_as.py + tests/conftest.py:
  * db_session fixture (conftest.py) -> in-memory SQLite Session.
  * monkeypatch.setenv("ALLOW_DEV_SECRET", "1") so create_app() boots.
  * monkeypatch every module-level SessionLocal a request touches to the SAME
    in-memory session so seeded rows are visible app-wide.
  * forge a partner session via client.session_transaction() (user_id +
    user_session_version + auth_ok + partner_auth_ok).
"""
from __future__ import annotations

import pytest
from werkzeug.security import generate_password_hash


# --------------------------------------------------------------------------
# Seed helpers
# --------------------------------------------------------------------------
def _mk_user(uid, name, level, *, store_scope=None, active=True, session_version=1):
    from app.models import User
    return User(
        id=uid,
        full_name=name,
        email=f"{name.lower().replace(' ', '.')}.{uid}@test.local",
        passcode_hash=generate_password_hash("12345"),
        permission_level=level,
        store_scope=store_scope,
        active=active,
        first_login_done=True,
        session_version=session_version,
    )


def _mk_employee(eid, name, *, active=True, session_version=0):
    from app.models import Employee
    return Employee(
        id=eid,
        full_name=name,
        phone=f"71350000{eid:02d}",
        email=f"{name.lower().replace(' ', '.')}.{eid}@staff.local",
        active=active,
        passcode_hash=generate_password_hash("54321"),
        session_version=session_version,
    )


def _mk_store_assignment(asg_id, emp_id, store_key):
    from app.models import EmployeeStoreAssignment
    return EmployeeStoreAssignment(id=asg_id, employee_id=emp_id, store_key=store_key)


@pytest.fixture
def app_and_emps(db_session, monkeypatch):
    """Boot the real app against the in-memory DB and seed:
        User  id=1  partner (the owner / real actor)
        User  id=2  gm      (a non-partner management user; case 3)
        Emp   id=10 "Edward Employee"  (tomball)        -> primary view-as target
        Emp   id=11 "Frida Frontline"  (copperfield)
        Emp   id=12 "Iggy Inactive"    (tomball, inactive) -> 404 target
    Returns (flask_app, partner, gm, emp_target, emp_other, emp_inactive).
    """
    from app import create_app
    from app import db as appdb
    from app.web import view_as_routes as va_mod
    from app.web import keypad_auth as keypad_mod
    from app.web import employee_auth as emp_mod
    from app.web import employee_my_profile_page as myprof_mod

    partner = _mk_user(1, "Owner Partner", "partner")
    gm = _mk_user(2, "Gina Manager", "gm", store_scope="tomball")
    emp_target = _mk_employee(10, "Edward Employee")
    emp_other = _mk_employee(11, "Frida Frontline")
    emp_inactive = _mk_employee(12, "Iggy Inactive", active=False)
    db_session.add_all([partner, gm, emp_target, emp_other, emp_inactive])
    db_session.add_all([
        _mk_store_assignment(100, 10, "tomball"),
        _mk_store_assignment(101, 11, "copperfield"),
        _mk_store_assignment(102, 12, "tomball"),
    ])
    db_session.commit()

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    flask_app = create_app()
    flask_app.config["TESTING"] = True

    # Route every per-request SessionLocal that this feature touches to the one
    # in-memory session so seeded rows are visible everywhere.
    #   * app.db.SessionLocal             -> permissions.load_current_user (import)
    #   * view_as_routes.SessionLocal     -> start/stop/banner + audit writes
    #   * keypad_auth.SessionLocal        -> its before_request hooks
    #   * employee_auth.SessionLocal      -> firewall + session-version gate hooks
    #   * employee_my_profile_page.SessionLocal -> the /employee/my-profile render
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(va_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(keypad_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(emp_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(myprof_mod, "SessionLocal", lambda: db_session)

    return flask_app, partner, gm, emp_target, emp_other, emp_inactive


def _login(client, user_id, *, version=1, partner=False):
    """Forge a keypad User session in the test client's cookie."""
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_session_version"] = version
        sess["auth_ok"] = True          # clears the auth.py site gate
        if partner:
            sess["partner_auth_ok"] = True


def _register_probe(flask_app):
    """Register a tiny GET|POST route on the TEST app (outside /view-as and
    /partner) so it represents a plain, non-privileged write route. The
    read-only before_request guard runs before dispatch, so a POST here is
    intercepted (403) while a GET falls through (200). Being outside /partner
    means the employee->/partner firewall does not interfere during an
    employee-view-as swap -- isolating the read-only guard as the cause."""
    if "emp_roguard_probe" in flask_app.view_functions:
        return

    def _probe():
        return "probe-ok", 200

    flask_app.add_url_rule(
        "/__emp_roguard_probe__", endpoint="emp_roguard_probe",
        view_func=_probe, methods=["GET", "POST"],
    )


# --------------------------------------------------------------------------
# 1) Partner POST /view-as/employee/<id> -> 302, and a follow GET
#    /employee/my-profile returns 200 rendering the TARGET employee's name
#    (the actual self-view, NOT the corporate Profile-Lab summary).
# --------------------------------------------------------------------------
def test_partner_start_employee_view_as_and_follow_profile(app_and_emps):
    flask_app, partner, _gm, emp_target, _other, _inactive = app_and_emps
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    r = client.post(f"/view-as/employee/{emp_target.id}")
    assert r.status_code in (301, 302)
    # Swap redirects to the employee self-view hub.
    assert r.headers["Location"].endswith("/employee/my-profile")

    # The follow GET renders the TARGET employee's profile (their full_name),
    # not the lab summary. employee_my_profile.html prints the name in the
    # profile header <div class="name">{{ employee.full_name }}</div>.
    g = client.get("/employee/my-profile")
    assert g.status_code == 200, g.get_data(as_text=True)[:300]
    body = g.get_data(as_text=True)
    assert "Edward Employee" in body
    # It is the employee self-view shell, not the corporate Profile-Lab page.
    assert "profile-lab" not in body.lower()


# --------------------------------------------------------------------------
# 2) Session after start: employee_id=target, user_id popped,
#    view_as_owner_uid=partner (+ owner_sv + view_as_kind=employee), and the
#    USER view-as / escalation keys stay absent.
# --------------------------------------------------------------------------
def test_session_state_after_employee_view_as_start(app_and_emps):
    flask_app, partner, _gm, emp_target, _other, _inactive = app_and_emps
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    assert client.post(f"/view-as/employee/{emp_target.id}").status_code in (301, 302)

    with client.session_transaction() as sess:
        # Session now reads as the target employee...
        assert sess.get("employee_id") == emp_target.id
        assert sess.get("auth_ok") is True
        # ...the owner's User keys were popped...
        assert "user_id" not in sess
        assert "user_session_version" not in sess
        # ...and the real owner is anchored for gating / exit / banner / audit.
        assert sess.get("view_as_owner_uid") == partner.id
        assert sess.get("view_as_owner_sv") == partner.session_version
        assert sess.get("view_as_kind") == "employee"
        # This is NOT a USER view-as, and the ungated escalation key is absent.
        assert sess.get("view_as_user_id") is None
        assert "impersonating_user_id" not in sess


# --------------------------------------------------------------------------
# 3) A non-partner User gets 403 on POST /view-as/employee/<id> (owner-gated on
#    the REAL user), and no swap occurs.
# --------------------------------------------------------------------------
def test_non_partner_forbidden_from_employee_view_as(app_and_emps):
    flask_app, _partner, gm, emp_target, _other, _inactive = app_and_emps
    client = flask_app.test_client()
    _login(client, gm.id)  # logged in as gm (non-partner)

    r = client.post(f"/view-as/employee/{emp_target.id}")
    assert r.status_code == 403

    # Nothing was swapped: the gm's own User session is intact, no employee swap.
    with client.session_transaction() as sess:
        assert sess.get("user_id") == gm.id
        assert sess.get("employee_id") is None
        assert sess.get("view_as_owner_uid") is None


# --------------------------------------------------------------------------
# 4) While employee-view-as, a POST to a normal write route is blocked
#    read-only (403); a GET to the same route still renders (200).
# --------------------------------------------------------------------------
def test_readonly_guard_blocks_post_while_employee_view_as(app_and_emps):
    flask_app, partner, _gm, emp_target, _other, _inactive = app_and_emps
    _register_probe(flask_app)
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # Enter employee-view-as (the swap also arms the read-only flush guard via
    # g.viewing_as, recovered from view_as_owner_uid even though user_id popped).
    assert client.post(f"/view-as/employee/{emp_target.id}").status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("employee_id") == emp_target.id

    # GET on a normal route still renders while viewing-as.
    assert client.get("/__emp_roguard_probe__").status_code == 200
    # POST on the same normal route is blocked read-only (non-idempotent method).
    blocked = client.post("/__emp_roguard_probe__")
    assert blocked.status_code == 403
    assert "read-only" in blocked.get_data(as_text=True).lower()


# --------------------------------------------------------------------------
# 5) GET/POST /view-as/stop restores session['user_id']=partner and clears the
#    swapped employee identity.
# --------------------------------------------------------------------------
def test_stop_restores_owner_and_clears_employee(app_and_emps):
    flask_app, partner, _gm, emp_target, _other, _inactive = app_and_emps
    _register_probe(flask_app)
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # --- GET /view-as/stop path.
    assert client.post(f"/view-as/employee/{emp_target.id}").status_code in (301, 302)
    rstop = client.get("/view-as/stop")
    assert rstop.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("user_id") == partner.id
        assert sess.get("user_session_version") == partner.session_version
        assert sess.get("employee_id") is None
        assert sess.get("employee_session_version") is None
        assert sess.get("view_as_owner_uid") is None
        assert sess.get("view_as_kind") is None

    # With the owner restored, a normal POST is allowed through the guard again.
    assert client.post("/__emp_roguard_probe__").status_code == 200

    # --- POST /view-as/stop path (also always permitted, even mid-swap).
    assert client.post(f"/view-as/employee/{emp_target.id}").status_code in (301, 302)
    rstop2 = client.post("/view-as/stop")
    assert rstop2.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("user_id") == partner.id
        assert sess.get("employee_id") is None
        assert sess.get("view_as_owner_uid") is None


# --------------------------------------------------------------------------
# 6) view_as_banner is non-empty AND names the swapped employee while active;
#    empty before any swap.
# --------------------------------------------------------------------------
def test_banner_names_employee_while_viewing(app_and_emps):
    flask_app, partner, _gm, emp_target, _other, _inactive = app_and_emps
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    def _banner_now():
        # Drive the full before_request chain (so load_current_user sets
        # g.viewing_as / g.real_user from view_as_owner_uid), then read the
        # context processor output -- mirroring a real request render.
        with flask_app.test_request_context("/"):
            with client.session_transaction() as sess:
                snapshot = dict(sess)
            from flask import session as flask_session
            flask_session.update(snapshot)
            for func in flask_app.before_request_funcs.get(None, []):
                func()
            ctx = {}
            for proc in flask_app.template_context_processors.get(None, []):
                ctx.update(proc())
            return ctx

    # Not viewing yet -> banner empty, flag false.
    ctx_off = _banner_now()
    assert ctx_off["view_as_banner"] == ""
    assert ctx_off["view_as_active"] is False

    # Start employee-view-as, then the banner is non-empty and names the
    # swapped EMPLOYEE (not g.current_user, which is None in this mode).
    assert client.post(f"/view-as/employee/{emp_target.id}").status_code in (301, 302)
    ctx_on = _banner_now()
    assert ctx_on["view_as_active"] is True
    assert ctx_on["view_as_banner"] != ""
    assert "VIEW-AS" in ctx_on["view_as_banner"]
    assert "Edward Employee" in ctx_on["view_as_banner"]
    assert "employee" in ctx_on["view_as_banner"]


# --------------------------------------------------------------------------
# 7) Extra coverage: a missing/inactive employee target -> 404 with NO swap.
# --------------------------------------------------------------------------
def test_inactive_employee_target_rejected(app_and_emps):
    flask_app, partner, _gm, _target, _other, emp_inactive = app_and_emps
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    r = client.post(f"/view-as/employee/{emp_inactive.id}")  # id=12, active=False
    assert r.status_code == 404
    with client.session_transaction() as sess:
        assert sess.get("employee_id") is None
        assert sess.get("view_as_owner_uid") is None
        assert sess.get("user_id") == partner.id  # owner session untouched


def test_missing_employee_target_rejected(app_and_emps):
    flask_app, partner, *_ = app_and_emps
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    r = client.post("/view-as/employee/99999")  # no such employee
    assert r.status_code == 404
    with client.session_transaction() as sess:
        assert sess.get("employee_id") is None
        assert sess.get("view_as_owner_uid") is None
        assert sess.get("user_id") == partner.id


# ==========================================================================
# FIX-PROVING TESTS (F1/F2/F3) -- each proves the named fix CLOSED its issue
# and (via a negative control) that the closure is attributable to the fix,
# not an unrelated gate. Feature code is NOT modified by these tests.
# ==========================================================================


# --------------------------------------------------------------------------
# (F1) HIGH escalation: _start_principal_view_as now ALSO pops
#      session['partner_auth_ok'] in the swap pop-block. A partner who starts
#      employee view-as must therefore NO LONGER reach /partner/* -- the
#      employee->/partner firewall (employee_auth.install ~1014: employee_id
#      set AND partner_auth_ok absent -> 403) now fires.
#
#      The partner forges partner_auth_ok=True (the /partner second factor),
#      starts an EMPLOYEE view-as, then hits a real /partner/* route
#      (/partner/profile-lab). Pre-fix, partner_auth_ok survived the swap, the
#      firewall's `not partner_auth_ok` was False, and the swapped session
#      could reach partner-only data (privilege escalation). Post-fix the key
#      is popped, the firewall fires, -> 403.
#
#      NEGATIVE CONTROL (proves the 403 is the firewall reacting to the popped
#      key, not a generic block): the SAME partner WITHOUT the swap reaches
#      /partner/profile-lab fine (non-403). So the only thing that changed is
#      that the swap dropped partner_auth_ok.
# --------------------------------------------------------------------------
def test_employee_view_as_cannot_reach_partner(app_and_emps, db_session, monkeypatch):
    flask_app, partner, _gm, emp_target, _other, _inactive = app_and_emps
    # /partner/profile-lab opens its OWN SessionLocal (the base fixture does
    # not patch this module); route it to the in-memory session so the control
    # path renders against the seeded rows instead of 500-ing.
    from app.web import corporate_profile_lab as lab_mod
    monkeypatch.setattr(lab_mod, "SessionLocal", lambda: db_session)

    client = flask_app.test_client()
    _login(client, partner.id, partner=True)  # forges partner_auth_ok=True

    # NEGATIVE CONTROL: with NO swap, the partner (second-factored) reaches the
    # /partner/* route. This must NOT be a 403 -- it is the baseline the firewall
    # would otherwise let through.
    with client.session_transaction() as sess:
        assert sess.get("partner_auth_ok") is True
    r_ctrl = client.get("/partner/profile-lab")
    assert r_ctrl.status_code != 403, (
        "control failed: a second-factored partner (no swap) should reach "
        f"/partner/profile-lab; got {r_ctrl.status_code}: "
        f"{r_ctrl.get_data(as_text=True)[:200]}"
    )

    # Start EMPLOYEE view-as. F1: the swap pops partner_auth_ok.
    assert client.post(f"/view-as/employee/{emp_target.id}").status_code in (301, 302)
    with client.session_transaction() as sess:
        # The session now reads as the target employee, and -- the F1 fix --
        # partner_auth_ok is GONE.
        assert sess.get("employee_id") == emp_target.id
        assert sess.get("partner_auth_ok") is None
        assert "partner_auth_ok" not in sess

    # The firewall fires (employee_id set AND partner_auth_ok absent) -> 403 on
    # ANY /partner/* route. Pre-fix this would have been a non-403 (the control
    # value above), i.e. partner data reached from a swapped employee session.
    r = client.get("/partner/profile-lab")
    assert r.status_code == 403, (
        "F1 REGRESSION: a swapped employee view-as reached /partner/profile-lab "
        f"(status {r.status_code}); partner_auth_ok was NOT popped by the swap"
    )


# --------------------------------------------------------------------------
# (F2) HIGH fail-open: load_current_user's stale-anchor teardown (the no-uid
#      branch) now ALSO pops employee_id / employee_session_version /
#      driver_id/_name/_location/_session_version / auth_ok (fail CLOSED). A
#      revoked / session_version-bumped / no-longer-partner owner who is mid
#      employee/driver view-as is dropped to ANONYMOUS on the next request --
#      NOT left in a live read-WRITE employee self-session.
#
#      Setup: partner starts employee view-as (user_id popped, owner anchored
#      via view_as_owner_uid + _sv). Then bump the OWNER User.session_version
#      in the DB so the anchored _sv no longer matches. The next request's
#      load_current_user takes the fail-closed path.
#
#      We assert on the teardown request itself (a probe route captures
#      g.viewing_as) AND on the resulting session: g.viewing_as is False (not a
#      live view-as), employee_id + auth_ok are gone, and a FOLLOW-UP write
#      POST as the (now-revoked) employee is bounced to the keypad by the site
#      gate -- proving it is NOT a live employee write session.
# --------------------------------------------------------------------------
def test_revoked_owner_midswap_fails_closed(app_and_emps, db_session):
    flask_app, partner, _gm, emp_target, _other, _inactive = app_and_emps

    # A probe route (registered before first request) that records g.viewing_as
    # as seen on the request where the teardown runs, then returns 200 (so we
    # can tell apart "ran but viewing_as cleared" from "bounced by a gate").
    captured = {}

    def _cap_probe():
        from flask import g
        captured["viewing_as"] = getattr(g, "viewing_as", "MISSING")
        return "probe-ok", 200

    if "emp_f2_capture_probe" not in flask_app.view_functions:
        flask_app.add_url_rule(
            "/__emp_f2_capture__", endpoint="emp_f2_capture_probe",
            view_func=_cap_probe, methods=["GET", "POST"],
        )

    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # Enter employee view-as: user_id popped, owner anchored at its CURRENT sv.
    assert client.post(f"/view-as/employee/{emp_target.id}").status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("employee_id") == emp_target.id
        assert sess.get("view_as_owner_uid") == partner.id
        assert sess.get("view_as_owner_sv") == partner.session_version
        assert "user_id" not in sess

    # REVOKE the owner mid-swap: bump their session_version so the anchored _sv
    # is now stale (equivalent to a passcode reset / forced re-auth). (Setting
    # owner.active=False would trip the SAME fail-closed branch.) db_session is
    # the same in-memory Session every per-request SessionLocal is patched to.
    from app.models import User
    owner_row = db_session.query(User).filter(User.id == partner.id).first()
    owner_row.session_version = (owner_row.session_version or 0) + 1
    db_session.commit()

    # The NEXT request runs load_current_user's no-uid branch. The anchored _sv
    # no longer matches the (bumped) owner -> FAIL CLOSED: it pops the swapped
    # employee_id/_sv, auth_ok, and the owner anchor. The probe view still runs
    # (the site gate saw the OLD auth_ok at gate-time, BEFORE the loader popped
    # it), so we can read the g.viewing_as it observed.
    r1 = client.get("/__emp_f2_capture__")
    assert r1.status_code == 200
    # On the teardown request the session is NOT a live view-as.
    assert captured.get("viewing_as") is False, (
        "F2 REGRESSION: g.viewing_as was not False on the post-revocation "
        f"request (got {captured.get('viewing_as')!r}); the revoked owner was "
        "left in a live view-as session instead of failing closed"
    )

    # After that request the session is dropped to ANONYMOUS -- the swapped
    # employee identity died WITH the invalid owner anchor (fail-closed), it was
    # NOT left as a live read-write employee self-session.
    with client.session_transaction() as sess:
        assert sess.get("employee_id") is None, "F2 REGRESSION: employee_id survived a revoked owner"
        assert sess.get("employee_session_version") is None
        assert sess.get("auth_ok") is None, "F2 REGRESSION: auth_ok survived a revoked owner"
        assert sess.get("view_as_owner_uid") is None
        assert sess.get("view_as_owner_sv") is None
        assert sess.get("view_as_kind") is None
        assert sess.get("user_id") is None  # owner's user_id was never restored

    # A WRITE as the (now-revoked) employee is NOT allowed through: the cookie is
    # anonymous, so the site gate bounces the next request to the keypad login
    # (302). i.e. this is decisively NOT a live employee write session.
    r2 = client.post("/__emp_f2_capture__")
    assert r2.status_code in (301, 302)
    assert "/keypad-login" in r2.headers.get("Location", "")


# --------------------------------------------------------------------------
# (F3) exit robustness: '/view-as' is in auth.py EXEMPT_PREFIXES, so
#      /view-as/stop (+ the control routes) stay reachable even when
#      user_id/auth_ok are absent -- they self-gate via _require_owner / the
#      owner anchor. This matters when the version-gate / teardown dropped
#      auth_ok mid-swap: the owner must STILL be able to exit.
#
#      Setup: partner starts employee view-as, then we DELETE auth_ok from the
#      session (simulating the version-gate/teardown popping it). GET
#      /view-as/stop must NOT be redirected to the keypad-login by the SITE
#      gate (the /view-as prefix is exempt), and it must clear the swap
#      (restore the owner's user_id, drop the employee identity).
#
#      NEGATIVE CONTROL (proves the exemption is what kept the exit reachable):
#      with auth_ok removed AND no /view-as exemption in play, a NON-exempt
#      route (a probe outside /view-as) IS bounced to keypad-login -- so the
#      same auth_ok-less session is unauthenticated to the site gate, and only
#      the /view-as exemption lets /view-as/stop through.
# --------------------------------------------------------------------------
def test_exit_reachable_without_auth(app_and_emps):
    flask_app, partner, _gm, emp_target, _other, _inactive = app_and_emps
    _register_probe(flask_app)  # /__emp_roguard_probe__ -- a NON-exempt route
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # Enter employee view-as.
    assert client.post(f"/view-as/employee/{emp_target.id}").status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("employee_id") == emp_target.id
        assert sess.get("view_as_owner_uid") == partner.id

    # Simulate the version-gate / teardown having popped auth_ok mid-swap. (The
    # owner anchor + employee_id remain -- this is the "stranded owner" state F3
    # guards against.)
    with client.session_transaction() as sess:
        sess.pop("auth_ok", None)
        assert "auth_ok" not in sess

    # NEGATIVE CONTROL: a NON-exempt route with auth_ok gone IS bounced to the
    # keypad by the site gate (no user_id, no auth_ok) -- confirming the session
    # is unauthenticated to the gate, so only the /view-as exemption can let the
    # exit through.
    r_ctrl = client.get("/__emp_roguard_probe__")
    assert r_ctrl.status_code in (301, 302)
    assert "/keypad-login" in r_ctrl.headers.get("Location", ""), (
        "control failed: an auth_ok-less, user_id-less session should be "
        f"bounced to keypad on a non-exempt route; got {r_ctrl.status_code}"
    )

    # F3 UNDER TEST: GET /view-as/stop is EXEMPT, so the site gate does NOT
    # bounce it to keypad-login. It self-gates (owner anchor) and restores the
    # owner / clears the swap.
    rstop = client.get("/view-as/stop")
    assert rstop.status_code in (301, 302), rstop.get_data(as_text=True)[:200]
    assert "/keypad-login" not in rstop.headers.get("Location", ""), (
        "F3 REGRESSION: /view-as/stop was redirected to keypad-login -- the "
        "/view-as exemption did not keep the exit reachable when auth_ok was gone"
    )

    # The swap is cleared: the owner's management session is restored and the
    # swapped employee identity is gone.
    with client.session_transaction() as sess:
        assert sess.get("user_id") == partner.id
        assert sess.get("employee_id") is None
        assert sess.get("view_as_owner_uid") is None
        assert sess.get("view_as_kind") is None


# ==========================================================================
# FIX-PROVING TESTS (F5b / F4b) -- the PRINCIPAL-ID BINDING that closes the
# foreign-relogin hijack, plus the driver-gate parity (F4b).
#
# THREAT MODEL (the PRIOR HIGH being closed):
#   1. A partner (owner) starts an EMPLOYEE/DRIVER view-as on a shared device.
#      The swap pops the owner's user_id, plants the target principal's
#      employee_id/driver_id, and anchors the owner (view_as_owner_uid + _sv +
#      _kind) AND now BINDS the planted principal (view_as_principal_id=target).
#   2. The owner walks away. A FOREIGN employee/driver then logs in on the SAME
#      device. A real login pops the owner's user_id (already absent) but does
#      NOT necessarily clear the owner ANCHOR -> pre-F5b the stale anchor
#      survived alongside the foreign employee_id.
#   3. The foreign user clicks /view-as/stop. Pre-F5b view_as_stop's restore
#      branch (owner_uid set AND user_id absent) fired and wrote
#      session['user_id'] = owner -> the FOREIGN user was silently escalated to
#      the partner (a privilege-escalation account-takeover).
#
# F5b closes this by BINDING the anchor to the planted principal:
#   load_current_user's no-uid branch is now THREE-way --
#     (i)   owner anchor INVALID            -> FAIL CLOSED (drop to anonymous).
#     (ii)  owner VALID AND present principal == view_as_principal_id
#                                            -> revive g.viewing_as (genuine).
#     (iii) owner VALID BUT present principal != view_as_principal_id (a FOREIGN
#           re-login, or no principal)       -> clear ONLY the anchor keys; the
#           foreign login KEEPS its own employee_id/auth_ok (no surprise logout)
#           but the anchor is gone, so /view-as/stop can no longer write
#           user_id=owner (no hijack).
# ==========================================================================


def _mk_driver(did, name, location, *, active=True, session_version=1):
    from app.models import Driver
    return Driver(
        id=did,
        name=name,
        location=location,
        email=f"{name.lower().replace(' ', '.')}.{did}@drv.local",
        phone=f"71360000{did:02d}",
        active=active,
        passcode_hash=generate_password_hash("99999"),
        first_login_done=True,
        session_version=session_version,
    )


@pytest.fixture
def app_and_emps_ext(db_session, monkeypatch):
    """Extends app_and_emps' seed with a 2nd ACTIVE employee (B, the foreign
    re-login actor) and an ACTIVE Driver (for the F4b driver-gate parity test):

        User   id=1  partner (owner / real actor)
        Emp    id=10 "Edward Employee"   (tomball)   -> view-as target A
        Emp    id=20 "Boris Bystander"   (copperfield) -> FOREIGN re-login B
        Driver id=30 "Dorian Driver"     (uno)       -> driver view-as target

    Returns (flask_app, partner, emp_a, emp_b, driver).

    Patches every per-request SessionLocal this feature touches -- including
    driver_system.SessionLocal (the /my-profile driver self-view + _current_driver)
    so the F4b driver path reads the in-memory rows.
    """
    from app import create_app
    from app import db as appdb
    from app.web import view_as_routes as va_mod
    from app.web import keypad_auth as keypad_mod
    from app.web import employee_auth as emp_mod
    from app.web import employee_my_profile_page as myprof_mod
    from app.web import driver_system as drv_mod

    partner = _mk_user(1, "Owner Partner", "partner")
    emp_a = _mk_employee(10, "Edward Employee", session_version=3)
    emp_b = _mk_employee(20, "Boris Bystander", session_version=7)
    driver = _mk_driver(30, "Dorian Driver", "uno", session_version=4)
    db_session.add_all([partner, emp_a, emp_b, driver])
    db_session.add_all([
        _mk_store_assignment(100, 10, "tomball"),
        _mk_store_assignment(120, 20, "copperfield"),
    ])
    db_session.commit()

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    flask_app = create_app()
    flask_app.config["TESTING"] = True

    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(va_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(keypad_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(emp_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(myprof_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(drv_mod, "SessionLocal", lambda: db_session)

    return flask_app, partner, emp_a, emp_b, driver


# --------------------------------------------------------------------------
# (H1) FOREIGN-RELOGIN HIJACK CLOSED.
#
#   Partner starts an employee view-as on target A. Then a FOREIGN employee B
#   logs in on the SAME client (we plant B's employee_id + employee_session_version
#   + auth_ok exactly the way _establish_employee_session does, but DELIBERATELY
#   leave the owner anchor in place -- the worst case the F5b binding must survive:
#   a login fold that does NOT clear the anchor). The first request then runs
#   load_current_user's no-uid branch:
#     * owner is still VALID, but the present principal (employee_id=B=20) is NOT
#       the bound view_as_principal_id (A=10) -> branch (iii): the anchor keys are
#       torn down, while B's own session (employee_id + auth_ok) is left intact.
#   Assertions:
#     * g.viewing_as is False on that request (NO phantom view-as for B).
#     * the session still has employee_id == B (the foreign login is NOT logged out).
#     * the owner anchor (view_as_owner_uid) has been cleared.
#     * GET /view-as/stop does NOT write user_id = owner (NO hijack), and the
#       next request's current_user is NOT the owner/partner.
# --------------------------------------------------------------------------
def test_foreign_employee_relogin_cannot_hijack_owner(app_and_emps_ext):
    flask_app, partner, emp_a, emp_b, _driver = app_and_emps_ext

    # A probe route that records g.viewing_as + the effective current_user id as
    # observed on the request where the no-uid teardown runs, then 200s (so we can
    # distinguish "ran but no view-as" from "bounced by a gate").
    captured = {}

    def _cap_probe():
        from flask import g
        captured["viewing_as"] = getattr(g, "viewing_as", "MISSING")
        cu = getattr(g, "current_user", None)
        captured["current_user_id"] = getattr(cu, "id", None)
        ru = getattr(g, "real_user", None)
        captured["real_user_id"] = getattr(ru, "id", None)
        return "probe-ok", 200

    if "emp_h1_capture_probe" not in flask_app.view_functions:
        flask_app.add_url_rule(
            "/__emp_h1_capture__", endpoint="emp_h1_capture_probe",
            view_func=_cap_probe, methods=["GET", "POST"],
        )

    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # 1) Partner starts employee view-as on target A. The swap binds the anchor
    #    to A via view_as_principal_id.
    assert client.post(f"/view-as/employee/{emp_a.id}").status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("employee_id") == emp_a.id
        assert sess.get("view_as_owner_uid") == partner.id
        assert sess.get("view_as_principal_id") == emp_a.id   # F5b binding
        assert "user_id" not in sess

    # 2) Simulate a FOREIGN employee B login on the SAME client: set B's
    #    employee_id + a MATCHING employee_session_version + auth_ok the way
    #    _establish_employee_session does -- but leave the owner anchor present
    #    (the hijack precondition: the stale anchor survived the foreign login).
    with client.session_transaction() as sess:
        sess["employee_id"] = emp_b.id
        sess["employee_session_version"] = emp_b.session_version  # matches DB -> not stale
        sess["auth_ok"] = True
        # The owner anchor is STILL present (this is the attack surface F5b closes).
        assert sess.get("view_as_owner_uid") == partner.id
        assert sess.get("view_as_principal_id") == emp_a.id
        assert "user_id" not in sess

    # 3) Issue a request. load_current_user's no-uid branch sees owner VALID but
    #    present principal (B) != bound principal (A) -> branch (iii): clears ONLY
    #    the anchor, keeps B's session. (B's matching session_version means the
    #    employee version-gate -- which runs AFTER the loader cleared the anchor --
    #    leaves B logged in.)
    r1 = client.get("/__emp_h1_capture__")
    assert r1.status_code == 200, r1.get_data(as_text=True)[:300]

    # NO phantom view-as for the foreign user.
    assert captured.get("viewing_as") is False, (
        "F5b REGRESSION: g.viewing_as was not False for a FOREIGN employee re-login "
        f"(got {captured.get('viewing_as')!r}); B inherited the owner's view-as anchor"
    )
    # g.current_user is NOT the owner/partner (the loader did not resolve a User).
    assert captured.get("current_user_id") != partner.id
    assert captured.get("real_user_id") != partner.id

    with client.session_transaction() as sess:
        # The foreign login is NOT logged out: B keeps its own employee session.
        assert sess.get("employee_id") == emp_b.id, (
            "F5b REGRESSION: the foreign employee B was logged out -- branch (iii) "
            "must keep B's own employee_id (no surprise logout)"
        )
        assert sess.get("auth_ok") is True
        # The owner anchor (+ binding) has been torn down.
        assert sess.get("view_as_owner_uid") is None, (
            "F5b REGRESSION: the stale owner anchor survived a foreign re-login"
        )
        assert sess.get("view_as_owner_sv") is None
        assert sess.get("view_as_kind") is None
        assert sess.get("view_as_principal_id") is None
        # The owner's user_id was never restored onto B's session.
        assert sess.get("user_id") is None

    # 4) THE HIJACK ATTEMPT: B clicks /view-as/stop. With the anchor gone, the
    #    restore branch (owner_uid set AND user_id absent) can NOT fire -> it must
    #    NOT write user_id = owner.
    rstop = client.get("/view-as/stop")
    assert rstop.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("user_id") != partner.id, (
            "PRIOR-HIGH NOT CLOSED: /view-as/stop escalated the foreign employee "
            "B into the partner (user_id == owner) -- the F5b principal binding "
            "did not prevent the hijack"
        )
        assert sess.get("user_id") is None
        # B is still just an employee (no management identity gained).
        assert sess.get("employee_id") == emp_b.id

    # 5) The NEXT request's effective current_user is NOT the owner/partner.
    captured.clear()
    r2 = client.get("/__emp_h1_capture__")
    assert r2.status_code == 200
    assert captured.get("current_user_id") != partner.id, (
        "PRIOR-HIGH NOT CLOSED: after /view-as/stop the effective current_user "
        "became the partner -- foreign B was escalated to the owner"
    )
    assert captured.get("real_user_id") != partner.id
    # B's post-stop session is a PURE employee session (no user_id, no anchor),
    # so _attach_current_user does not even run load_current_user -> g.viewing_as
    # is never set ("MISSING"). Either way it is NOT an active view-as.
    assert captured.get("viewing_as") in (False, "MISSING")


# --------------------------------------------------------------------------
# (H1-control) GENUINE active view-as still revives. Positive control proving
#   the H1 teardown is the PRINCIPAL-MISMATCH reacting (branch iii), not a
#   generic "always tear down" -- when the present principal MATCHES the bound
#   view_as_principal_id (the real, unbroken swap), the loader revives the
#   view-as (branch ii): g.viewing_as True + g.real_user == owner.
# --------------------------------------------------------------------------
def test_genuine_employee_view_as_still_revives_after_binding(app_and_emps_ext):
    flask_app, partner, emp_a, _emp_b, _driver = app_and_emps_ext

    captured = {}

    def _cap_probe():
        from flask import g
        captured["viewing_as"] = getattr(g, "viewing_as", "MISSING")
        ru = getattr(g, "real_user", None)
        captured["real_user_id"] = getattr(ru, "id", None)
        return "probe-ok", 200

    if "emp_h1c_capture_probe" not in flask_app.view_functions:
        flask_app.add_url_rule(
            "/__emp_h1c_capture__", endpoint="emp_h1c_capture_probe",
            view_func=_cap_probe, methods=["GET", "POST"],
        )

    client = flask_app.test_client()
    _login(client, partner.id, partner=True)
    assert client.post(f"/view-as/employee/{emp_a.id}").status_code in (301, 302)

    # The present principal IS the bound one (A) -> branch (ii): genuine view-as.
    r = client.get("/__emp_h1c_capture__")
    assert r.status_code == 200
    assert captured.get("viewing_as") is True, (
        "branch (ii) failed: a genuine, unbroken employee view-as (present "
        "principal == bound principal) must still revive g.viewing_as"
    )
    assert captured.get("real_user_id") == partner.id
    # The anchor + binding survive an UNBROKEN swap (only a mismatch tears down).
    with client.session_transaction() as sess:
        assert sess.get("view_as_owner_uid") == partner.id
        assert sess.get("view_as_principal_id") == emp_a.id
        assert sess.get("employee_id") == emp_a.id


# --------------------------------------------------------------------------
# (H4) DRIVER-GATE PARITY (F4b): keypad_auth._validate_driver_session now early-
#   returns when view_as_owner_uid is set (mirrors the employee gate's F4), so a
#   target-DRIVER session_version bump mid-swap no longer pops the driver keys --
#   the view-as stays ACTIVE (load_current_user revives it because the owner is
#   valid and the present driver principal still matches view_as_principal_id).
#
#   This is the driver analogue of the employee F4 gate: without F4b the driver
#   version-gate would strip session['driver_id'] mid-swap and break the view.
# --------------------------------------------------------------------------
def test_driver_target_sv_bump_midswap_keeps_view(app_and_emps_ext, db_session):
    flask_app, partner, _emp_a, _emp_b, driver = app_and_emps_ext

    captured = {}

    def _cap_probe():
        from flask import g
        captured["viewing_as"] = getattr(g, "viewing_as", "MISSING")
        ru = getattr(g, "real_user", None)
        captured["real_user_id"] = getattr(ru, "id", None)
        return "probe-ok", 200

    if "drv_h4_capture_probe" not in flask_app.view_functions:
        flask_app.add_url_rule(
            "/__drv_h4_capture__", endpoint="drv_h4_capture_probe",
            view_func=_cap_probe, methods=["GET", "POST"],
        )

    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # Partner starts a DRIVER view-as. The swap plants driver_id + binds the
    # anchor to the driver via view_as_principal_id.
    assert client.post(f"/view-as/driver/{driver.id}").status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("driver_id") == driver.id
        assert sess.get("view_as_owner_uid") == partner.id
        assert sess.get("view_as_kind") == "driver"
        assert sess.get("view_as_principal_id") == driver.id
        assert "user_id" not in sess

    # Sanity: view-as is active right now.
    r0 = client.get("/__drv_h4_capture__")
    assert r0.status_code == 200
    assert captured.get("viewing_as") is True

    # BUMP the TARGET DRIVER's session_version in the DB (mid-swap). Pre-F4b,
    # _validate_driver_session would see the stale driver_session_version and POP
    # the driver keys, breaking the swap. F4b: it early-returns because
    # view_as_owner_uid is set, so view-as ownership of the lifecycle is preserved.
    from app.models import Driver
    drv_row = db_session.query(Driver).filter(Driver.id == driver.id).first()
    drv_row.session_version = (drv_row.session_version or 0) + 1
    db_session.commit()

    captured.clear()
    r1 = client.get("/__drv_h4_capture__")
    assert r1.status_code == 200

    # F4b: the driver keys SURVIVE the target sv-bump, and -- because the owner is
    # still valid AND the present driver principal still equals the bound
    # view_as_principal_id -- load_current_user REVIVES the view-as (branch ii).
    assert captured.get("viewing_as") is True, (
        "F4b REGRESSION: a target-driver session_version bump mid-swap stripped "
        "the driver view-as (g.viewing_as went False); _validate_driver_session "
        "did not early-return on the owner anchor"
    )
    assert captured.get("real_user_id") == partner.id
    with client.session_transaction() as sess:
        assert sess.get("driver_id") == driver.id, (
            "F4b REGRESSION: the planted driver_id was popped by the driver "
            "version-gate mid-swap"
        )
        assert sess.get("view_as_owner_uid") == partner.id
        assert sess.get("view_as_principal_id") == driver.id

    # And the owner can still exit cleanly -> management session restored.
    rstop = client.get("/view-as/stop")
    assert rstop.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("user_id") == partner.id
        assert sess.get("driver_id") is None
        assert sess.get("view_as_owner_uid") is None
        assert sess.get("view_as_principal_id") is None


# ==========================================================================
# CROSS-KIND FOREIGN-RELOGIN HIJACK TESTS (C1 / C2 / C3).
#
# THREAT MODEL (the PRIOR re-verify HIGH being closed -- the CROSS-KIND variant):
#   The earlier F5b binding bound the owner anchor to the planted principal of
#   ONE kind (view_as_principal_id). But the no-uid revive check originally only
#   compared the BOUND kind's present id to view_as_principal_id. A foreign login
#   of the OTHER kind (a DRIVER over an active EMPLOYEE view-as, or vice-versa)
#   left the EMPLOYEE's stale planted employee_id (== the bound A) untouched while
#   ALSO planting a foreign driver_id. The bound-kind id therefore STILL equalled
#   view_as_principal_id -> the loader REVIVED a phantom view-as -> /view-as/stop's
#   restore branch fired -> the foreign DRIVER was escalated to the owner partner
#   (cross-principal, cross-kind account takeover).
#
# TWO COMPLEMENTARY FIXES (read the ACTUAL code):
#   FIX A (permissions.load_current_user no-uid branch -- the single chokepoint):
#     computes, per view_as_kind, the present principal of THAT kind, whether a
#     CROSS-kind principal is ALSO present (_cross_present), and the planted-key
#     list. Revive (branch ii) now requires owner valid AND
#     present-principal-of-bound-kind == view_as_principal_id AND NO cross-kind
#     principal present. A cross-kind foreign login trips _cross_present -> branch
#     (iii): clear the anchor, AND clear the stale PLANTED principal key (the
#     bound A leftover the foreign login never overwrote) so it cannot leak A's
#     data, while KEEPING the foreign login's own keys.
#   FIX B (symmetric login cleanup at the SOURCE -- all three principal-login
#     paths): keypad_auth driver Path-1, driver_routes.driver_login_submit, AND
#     employee_auth._establish_employee_session ALL now pop the cross-principal id
#     (employee_id/_sv on a driver login; driver_* on an employee login) AND the
#     view-as anchor keys, so a principal login on a shared device starts clean.
#
# C1 + C2 below DELIBERATELY exercise the worst case for FIX A -- the foreign
# login LEAVES the anchor + the stale cross-kind planted key in place (the very
# login-fold path FIX B would otherwise pre-clean) -- so the assertion proves the
# LOADER CHOKEPOINT (FIX A) closes the hijack on its own, independent of FIX B.
# C2 additionally drives the REAL employee-login path (_establish_employee_session)
# to confirm FIX B pre-cleans the cross-principal + anchor at the source too.
# ==========================================================================


def _run_employee_login(flask_app, client, emp):
    """Drive the REAL employee-login cleanup+set (employee_auth.
    _establish_employee_session -- the same function /employee/login uses) against
    the test client's cookie. Runs inside a request context with the client's
    current session loaded, then writes the mutated session back to the cookie.
    This is a faithful 'foreign employee logs in on the same device' simulation:
    it exercises FIX B's symmetric cleanup (pop cross-principal driver_* keys +
    the view-as anchor) AND the employee key-set, exactly as production login does."""
    from app.web.employee_auth import _establish_employee_session
    from flask import session as flask_session

    with client.session_transaction() as sess:
        snapshot = dict(sess)
    with flask_app.test_request_context("/"):
        flask_session.update(snapshot)
        _establish_employee_session(emp)
        mutated = dict(flask_session)
    with client.session_transaction() as sess:
        sess.clear()
        sess.update(mutated)


# --------------------------------------------------------------------------
# (C1) DRIVER-OVER-EMPLOYEE foreign re-login CLOSED.
#
#   Partner starts an EMPLOYEE view-as on target A (emp_a=10). Then a FOREIGN
#   DRIVER logs in on the SAME client. We replicate driver_routes.
#   driver_login_submit's key-SET (driver_id=D + driver_session_version + auth_ok)
#   but DELIBERATELY leave BOTH the owner anchor AND the stale planted employee_id
#   (== bound A) in place -- the worst-case login-fold the FIX A chokepoint must
#   survive on its own. The first request runs load_current_user's no-uid branch:
#     * owner still VALID, bound kind is 'employee', present employee_id == bound A
#       (10), BUT a CROSS-kind principal (driver_id=D) is ALSO present
#       -> _cross_present True -> branch (iii): tear down the anchor, and -- because
#       the bound A id is still the planted leftover -- ALSO clear employee_id/_sv
#       (the cross-kind leftover) so A's data can't leak; KEEP the foreign driver.
#   Assertions:
#     * g.viewing_as is False on that request (NO phantom view-as).
#     * driver_id is still == D (the foreign driver is NOT logged out).
#     * employee_id is GONE (the stale planted key the loader cleared).
#     * the owner anchor is GONE.
#     * GET /view-as/stop does NOT write user_id = owner (NO hijack); next
#       request's effective current_user is NOT the owner/partner.
# --------------------------------------------------------------------------
def test_foreign_driver_relogin_over_employee_view_as_no_hijack(app_and_emps_ext, db_session):
    flask_app, partner, emp_a, _emp_b, driver = app_and_emps_ext

    captured = {}

    def _cap_probe():
        from flask import g
        captured["viewing_as"] = getattr(g, "viewing_as", "MISSING")
        cu = getattr(g, "current_user", None)
        captured["current_user_id"] = getattr(cu, "id", None)
        ru = getattr(g, "real_user", None)
        captured["real_user_id"] = getattr(ru, "id", None)
        return "probe-ok", 200

    if "c1_capture_probe" not in flask_app.view_functions:
        flask_app.add_url_rule(
            "/__c1_capture__", endpoint="c1_capture_probe",
            view_func=_cap_probe, methods=["GET", "POST"],
        )

    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # 1) Partner starts EMPLOYEE view-as on A. Swap binds anchor to A (employee).
    assert client.post(f"/view-as/employee/{emp_a.id}").status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("employee_id") == emp_a.id
        assert sess.get("view_as_kind") == "employee"
        assert sess.get("view_as_principal_id") == emp_a.id
        assert "user_id" not in sess

    # 2) FOREIGN DRIVER login on the SAME client -- replicate driver_login_submit's
    #    key-SET, but WORST CASE: leave the owner anchor AND the stale planted
    #    employee_id (== bound A) in place (the cross-kind leftover FIX A must
    #    handle). driver_session_version MATCHES the DB row so the driver
    #    version-gate (which runs AFTER the loader) won't independently pop it.
    with client.session_transaction() as sess:
        sess["driver_id"] = driver.id
        sess["driver_name"] = driver.name
        sess["driver_location"] = driver.location
        sess["driver_session_version"] = driver.session_version  # matches DB -> not stale
        sess["auth_ok"] = True
        # The cross-kind leftovers the chokepoint must clean up:
        assert sess.get("employee_id") == emp_a.id              # stale planted (== bound A)
        assert sess.get("view_as_owner_uid") == partner.id      # anchor survived the foreign login
        assert sess.get("view_as_principal_id") == emp_a.id
        assert "user_id" not in sess

    # 3) Request: no-uid branch sees owner VALID, bound employee_id == A, BUT a
    #    cross-kind driver_id is ALSO present -> branch (iii): clear anchor + the
    #    stale planted employee_id; keep the foreign driver. NO revive.
    r1 = client.get("/__c1_capture__")
    assert r1.status_code == 200, r1.get_data(as_text=True)[:300]

    assert captured.get("viewing_as") is False, (
        "CROSS-KIND REGRESSION (C1): g.viewing_as was not False for a FOREIGN "
        f"driver re-login over an employee view-as (got {captured.get('viewing_as')!r}); "
        "the cross-kind driver inherited the owner's employee view-as anchor"
    )
    # The loader did not resolve a User (owner not revived as effective/ real user).
    assert captured.get("current_user_id") != partner.id
    assert captured.get("real_user_id") != partner.id

    with client.session_transaction() as sess:
        # Foreign DRIVER kept (no surprise logout).
        assert sess.get("driver_id") == driver.id, (
            "CROSS-KIND REGRESSION (C1): the foreign driver D was logged out -- "
            "branch (iii) must keep the foreign login's own driver_id"
        )
        assert sess.get("auth_ok") is True
        # Stale planted EMPLOYEE key (== bound A) cleared so A's data can't leak.
        assert sess.get("employee_id") is None, (
            "CROSS-KIND REGRESSION (C1): the stale planted employee_id (the swap "
            "target A) survived the cross-kind foreign driver login -> A's data leak"
        )
        assert sess.get("employee_session_version") is None
        # Owner anchor (+ binding) torn down.
        assert sess.get("view_as_owner_uid") is None, (
            "CROSS-KIND REGRESSION (C1): the stale owner anchor survived a "
            "cross-kind foreign driver re-login"
        )
        assert sess.get("view_as_owner_sv") is None
        assert sess.get("view_as_kind") is None
        assert sess.get("view_as_principal_id") is None
        assert sess.get("user_id") is None

    # 4) THE HIJACK ATTEMPT: the foreign driver clicks /view-as/stop. Anchor gone
    #    -> the restore branch (owner_uid set AND user_id absent) can NOT fire ->
    #    it must NOT write user_id = owner.
    rstop = client.get("/view-as/stop")
    assert rstop.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("user_id") != partner.id, (
            "PRIOR-HIGH (CROSS-KIND) NOT CLOSED: /view-as/stop escalated the "
            "foreign DRIVER into the partner (user_id == owner)"
        )
        assert sess.get("user_id") is None
        # The foreign principal is still just a driver (no management identity).
        assert sess.get("driver_id") == driver.id

    # 5) The NEXT request's effective current_user is NOT the owner/partner.
    captured.clear()
    r2 = client.get("/__c1_capture__")
    assert r2.status_code == 200
    assert captured.get("current_user_id") != partner.id, (
        "PRIOR-HIGH (CROSS-KIND) NOT CLOSED: after /view-as/stop the effective "
        "current_user became the partner -- the foreign driver was escalated"
    )
    assert captured.get("real_user_id") != partner.id
    # Post-stop B/D session is a PURE driver session (no user_id, no anchor) so
    # _attach_current_user never runs the loader -> viewing_as is "MISSING".
    assert captured.get("viewing_as") in (False, "MISSING")


# --------------------------------------------------------------------------
# (C2) EMPLOYEE-OVER-DRIVER foreign re-login CLOSED.
#
#   Partner starts a DRIVER view-as on target D (driver=30). Then a FOREIGN
#   EMPLOYEE B (emp_b=20) logs in on the SAME client via the REAL login path
#   (_establish_employee_session) -- which (FIX B) pops the cross-principal
#   driver_* keys AND the view-as anchor at the source. We then ALSO assert the
#   end-state is hijack-proof. To independently prove the FIX A chokepoint (in
#   case a future login-fold path forgets to clean up), a second leg re-plants
#   the worst case (anchor + stale driver_id left behind) and shows the loader
#   tears it down.
#   Assertions (real-login leg):
#     * after _establish_employee_session: employee_id == B, driver_* GONE,
#       anchor GONE (FIX B at the source).
#     * request -> g.viewing_as False, employee_id == B, driver_id gone, anchor gone.
#     * GET /view-as/stop does NOT write user_id = owner.
# --------------------------------------------------------------------------
def test_foreign_employee_relogin_over_driver_view_as_no_hijack(app_and_emps_ext, db_session):
    flask_app, partner, _emp_a, emp_b, driver = app_and_emps_ext

    captured = {}

    def _cap_probe():
        from flask import g
        captured["viewing_as"] = getattr(g, "viewing_as", "MISSING")
        cu = getattr(g, "current_user", None)
        captured["current_user_id"] = getattr(cu, "id", None)
        ru = getattr(g, "real_user", None)
        captured["real_user_id"] = getattr(ru, "id", None)
        return "probe-ok", 200

    if "c2_capture_probe" not in flask_app.view_functions:
        flask_app.add_url_rule(
            "/__c2_capture__", endpoint="c2_capture_probe",
            view_func=_cap_probe, methods=["GET", "POST"],
        )

    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # 1) Partner starts DRIVER view-as on D. Swap binds anchor to D (driver).
    assert client.post(f"/view-as/driver/{driver.id}").status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("driver_id") == driver.id
        assert sess.get("view_as_kind") == "driver"
        assert sess.get("view_as_principal_id") == driver.id
        assert "user_id" not in sess

    # 2) FOREIGN EMPLOYEE B logs in via the REAL path. FIX B: _establish_employee_
    #    session pops the cross-principal driver_* keys AND the view-as anchor at
    #    the source, and sets B's employee keys + auth_ok.
    _run_employee_login(flask_app, client, emp_b)
    with client.session_transaction() as sess:
        assert sess.get("employee_id") == emp_b.id
        assert sess.get("auth_ok") is True
        # FIX B cleaned the cross-principal driver identity at the source...
        assert sess.get("driver_id") is None, (
            "FIX B REGRESSION (C2): _establish_employee_session did not pop the "
            "cross-principal driver_id on an employee login"
        )
        assert sess.get("driver_session_version") is None
        # ...AND the view-as anchor at the source.
        assert sess.get("view_as_owner_uid") is None, (
            "FIX B REGRESSION (C2): _establish_employee_session did not pop the "
            "view-as owner anchor on an employee login"
        )
        assert sess.get("view_as_kind") is None
        assert sess.get("view_as_principal_id") is None
        # B is a PURE employee (no user_id login-fold -- emp_b has no linked User).
        assert sess.get("user_id") is None

    # 3) Request -> no phantom view-as (the anchor was cleared at login).
    r1 = client.get("/__c2_capture__")
    assert r1.status_code == 200, r1.get_data(as_text=True)[:300]
    assert captured.get("viewing_as") in (False, "MISSING"), (
        "CROSS-KIND REGRESSION (C2): a FOREIGN employee re-login over a driver "
        f"view-as showed a phantom view-as (got {captured.get('viewing_as')!r})"
    )
    assert captured.get("current_user_id") != partner.id
    assert captured.get("real_user_id") != partner.id
    with client.session_transaction() as sess:
        assert sess.get("employee_id") == emp_b.id
        assert sess.get("driver_id") is None
        assert sess.get("view_as_owner_uid") is None

    # 4) THE HIJACK ATTEMPT: B clicks /view-as/stop. Anchor gone -> no restore.
    rstop = client.get("/view-as/stop")
    assert rstop.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("user_id") != partner.id, (
            "PRIOR-HIGH (CROSS-KIND) NOT CLOSED: /view-as/stop escalated the "
            "foreign EMPLOYEE into the partner (user_id == owner)"
        )
        assert sess.get("user_id") is None
        assert sess.get("employee_id") == emp_b.id

    # 5) Effective current_user is NOT the owner on the next request.
    captured.clear()
    r2 = client.get("/__c2_capture__")
    assert r2.status_code == 200
    assert captured.get("current_user_id") != partner.id
    assert captured.get("real_user_id") != partner.id

    # ---- SECOND LEG: prove the FIX A chokepoint independently of FIX B ----
    # Re-enter the driver view-as, then re-plant the WORST case a future login-
    # fold might leave: a foreign employee B WITH the owner anchor AND the stale
    # planted driver_id (== bound D) still present. The loader's no-uid branch
    # must tear it down (cross-kind present -> no revive; clear anchor + stale
    # planted driver_*; keep B).
    _login(client, partner.id, partner=True)
    assert client.post(f"/view-as/driver/{driver.id}").status_code in (301, 302)
    with client.session_transaction() as sess:
        # Foreign employee B planted, but the driver anchor + stale driver_id
        # (== bound D) are DELIBERATELY left behind (the cross-kind leftover).
        sess["employee_id"] = emp_b.id
        sess["employee_session_version"] = emp_b.session_version  # matches DB -> not stale
        sess["auth_ok"] = True
        assert sess.get("driver_id") == driver.id                # stale planted (== bound D)
        assert sess.get("view_as_kind") == "driver"
        assert sess.get("view_as_principal_id") == driver.id
        assert "user_id" not in sess

    captured.clear()
    r3 = client.get("/__c2_capture__")
    assert r3.status_code == 200
    assert captured.get("viewing_as") is False, (
        "CROSS-KIND REGRESSION (C2, FIX A leg): g.viewing_as was not False for a "
        f"foreign employee over a driver view-as (got {captured.get('viewing_as')!r}); "
        "the bound-kind (driver) id still equalled view_as_principal_id so the "
        "loader revived a phantom view-as despite the cross-kind employee_id"
    )
    with client.session_transaction() as sess:
        # B kept; stale planted DRIVER keys (== bound D) cleared; anchor gone.
        assert sess.get("employee_id") == emp_b.id, (
            "CROSS-KIND REGRESSION (C2, FIX A leg): foreign employee B was logged out"
        )
        assert sess.get("driver_id") is None, (
            "CROSS-KIND REGRESSION (C2, FIX A leg): the stale planted driver_id "
            "(swap target D) survived -> D's data leak"
        )
        assert sess.get("driver_session_version") is None
        assert sess.get("driver_name") is None
        assert sess.get("driver_location") is None
        assert sess.get("view_as_owner_uid") is None
        assert sess.get("view_as_principal_id") is None
        assert sess.get("user_id") is None

    # The hijack attempt still fails on the FIX A leg.
    rstop2 = client.get("/view-as/stop")
    assert rstop2.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("user_id") != partner.id
        assert sess.get("user_id") is None
        assert sess.get("employee_id") == emp_b.id


# --------------------------------------------------------------------------
# (C3) POSITIVE CONTROL -- a GENUINE clean DRIVER view-as (no foreign cross-kind
#   principal present) STILL revives + renders. Proves the new _cross_present
#   guard in branch (ii) is reacting to the CROSS-KIND intruder specifically and
#   has NOT broken the ordinary single-kind swap. (The genuine EMPLOYEE positive
#   control lives in test_genuine_employee_view_as_still_revives_after_binding
#   and test_partner_start_employee_view_as_and_follow_profile above; this adds
#   the driver analogue + an end-to-end /my-profile render.)
# --------------------------------------------------------------------------
def test_genuine_driver_view_as_still_revives_and_renders(app_and_emps_ext):
    flask_app, partner, _emp_a, _emp_b, driver = app_and_emps_ext

    captured = {}

    def _cap_probe():
        from flask import g
        captured["viewing_as"] = getattr(g, "viewing_as", "MISSING")
        ru = getattr(g, "real_user", None)
        captured["real_user_id"] = getattr(ru, "id", None)
        return "probe-ok", 200

    if "c3_capture_probe" not in flask_app.view_functions:
        flask_app.add_url_rule(
            "/__c3_capture__", endpoint="c3_capture_probe",
            view_func=_cap_probe, methods=["GET", "POST"],
        )

    client = flask_app.test_client()
    _login(client, partner.id, partner=True)
    r = client.post(f"/view-as/driver/{driver.id}")
    assert r.status_code in (301, 302)
    assert r.headers["Location"].endswith("/my-profile")

    # Clean swap: present principal IS the bound driver, NO cross-kind principal
    # present -> branch (ii): genuine view-as revives.
    rc = client.get("/__c3_capture__")
    assert rc.status_code == 200
    assert captured.get("viewing_as") is True, (
        "C3 POSITIVE CONTROL FAILED: a genuine, single-kind driver view-as (no "
        "cross-kind principal present) must still revive g.viewing_as -- the "
        "_cross_present guard must not block the ordinary swap"
    )
    assert captured.get("real_user_id") == partner.id
    with client.session_transaction() as sess:
        assert sess.get("driver_id") == driver.id
        assert sess.get("view_as_owner_uid") == partner.id
        assert sess.get("view_as_principal_id") == driver.id
        # No cross-kind employee principal leaked into the genuine swap.
        assert sess.get("employee_id") is None

    # End-to-end: the driver self-view actually renders the TARGET driver.
    g = client.get("/my-profile")
    assert g.status_code == 200, g.get_data(as_text=True)[:300]
    assert "Dorian Driver" in g.get_data(as_text=True)

    # And the owner can exit cleanly.
    rstop = client.get("/view-as/stop")
    assert rstop.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("user_id") == partner.id
        assert sess.get("driver_id") is None
        assert sess.get("view_as_owner_uid") is None


# ==========================================================================
# (LP1) LINKED-EMPLOYEE-PARTNER regression -- the POP-BOTH fix.
#
# THREAT/REGRESSION (the prior audit FAIL this proves CLOSED):
#   A partner can be UNIFY-linked to their OWN Employee row (Employee.user_id ==
#   that partner's User). After their ONE passcode login the login-fold
#   (employee_auth._establish_employee_session) leaves the session carrying BOTH
#   session['user_id'] (manager keys) AND session['employee_id'] (their own
#   employee self-service) + auth_ok + partner_auth_ok.
#
#   When this LINKED partner starts a GENUINE *DRIVER* view-as, the swap pops
#   user_id and plants driver_id. PRE-FIX, the swap did NOT pop the partner's own
#   residual employee_id, so the swapped session was an IMPURE driver: it carried
#   driver_id (the bound target) AND a leftover employee_id (the linked partner's
#   own). On the follow-up request load_current_user's no-uid branch (kind ==
#   'driver') computes _cross_present = session.get('employee_id') is not None ->
#   True, so the genuine swap fell to branch (iii) and was DESTROYED (g.viewing_as
#   False, the planted driver_id/anchor torn down) instead of REVIVED. The owner
#   saw a broken view-as for a perfectly valid driver target.
#
#   THE FIX (view_as_routes._start_principal_view_as): the swap now pops BOTH
#   principals' keys (employee_id/_sv AND driver_id/_name/_location/_sv) before
#   planting the bound kind's target keys, so the swap session is a PURE driver
#   with NO residual cross-kind employee_id. The loader then sees _cross_present
#   False and REVIVES the genuine view-as (branch ii).
#
#   This test forges the EXACT linked-partner session the UNIFY fold produces
#   (user_id=partner AND employee_id=partner's-own-employee + auth_ok +
#   partner_auth_ok), starts a DRIVER view-as on a seeded ACTIVE driver, and on
#   the FOLLOW-UP request asserts the genuine view-as REVIVES: g.viewing_as True,
#   session driver_id == target with NO employee_id (pure swap), view_as_owner_uid
#   == partner. Then GET /view-as/stop restores user_id == partner.
# --------------------------------------------------------------------------
@pytest.fixture
def app_and_linked_partner(db_session, monkeypatch):
    """Boot the real app against the in-memory DB and seed a LINKED-EMPLOYEE
    PARTNER plus an ACTIVE driver target:

        User   id=1  partner "Owner Partner"      (owner / real actor)
        Emp    id=10 "Owner Partner (staff)"      user_id=1  -> the UNIFY link
                                                  (the partner's OWN employee row)
        Driver id=30 "Dorian Driver" (uno)        -> the genuine DRIVER view-as target

    Returns (flask_app, partner, linked_emp, driver). Patches every per-request
    SessionLocal this path touches (incl. driver_system for the /my-profile
    self-view + employee_auth for the firewall / version-gate hooks)."""
    from app import create_app
    from app import db as appdb
    from app.web import view_as_routes as va_mod
    from app.web import keypad_auth as keypad_mod
    from app.web import employee_auth as emp_mod
    from app.web import employee_my_profile_page as myprof_mod
    from app.web import driver_system as drv_mod

    partner = _mk_user(1, "Owner Partner", "partner")
    # The partner's OWN employee row, UNIFY-linked back to them (Employee.user_id
    # == the partner's User id). _mk_employee doesn't set user_id, so set it here.
    linked_emp = _mk_employee(10, "Owner Partner Staff", session_version=2)
    linked_emp.user_id = partner.id
    driver = _mk_driver(30, "Dorian Driver", "uno", session_version=4)
    db_session.add_all([partner, linked_emp, driver])
    db_session.add_all([_mk_store_assignment(100, 10, "tomball")])
    db_session.commit()

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    flask_app = create_app()
    flask_app.config["TESTING"] = True

    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(va_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(keypad_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(emp_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(myprof_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(drv_mod, "SessionLocal", lambda: db_session)

    return flask_app, partner, linked_emp, driver


def test_linked_partner_driver_view_as_revives(app_and_linked_partner):
    flask_app, partner, linked_emp, driver = app_and_linked_partner

    # Probe route to read g.viewing_as / g.real_user on the follow-up request
    # (mirrors the C3 pattern; the driver self-view route is awkward to assert
    # g.viewing_as through directly).
    captured = {}

    def _cap_probe():
        from flask import g
        captured["viewing_as"] = getattr(g, "viewing_as", "MISSING")
        ru = getattr(g, "real_user", None)
        captured["real_user_id"] = getattr(ru, "id", None)
        return "probe-ok", 200

    if "lp1_capture_probe" not in flask_app.view_functions:
        flask_app.add_url_rule(
            "/__lp1_capture__", endpoint="lp1_capture_probe",
            view_func=_cap_probe, methods=["GET", "POST"],
        )

    client = flask_app.test_client()

    # Forge the EXACT session the UNIFY login-fold leaves for a LINKED partner:
    # BOTH the manager keys (user_id + user_session_version + partner_auth_ok) AND
    # the employee self-service key (employee_id + employee_session_version), all
    # behind auth_ok. This is the "both session['user_id'] AND session['employee_id']"
    # precondition the pop-both fix must handle.
    with client.session_transaction() as sess:
        sess["user_id"] = partner.id
        sess["user_session_version"] = partner.session_version
        sess["auth_ok"] = True
        sess["partner_auth_ok"] = True
        sess["employee_id"] = linked_emp.id                       # the partner's OWN employee row
        sess["employee_session_version"] = linked_emp.session_version
    # Sanity: the forged session really does carry BOTH principals.
    with client.session_transaction() as sess:
        assert sess.get("user_id") == partner.id
        assert sess.get("employee_id") == linked_emp.id

    # Start a GENUINE DRIVER view-as. _require_owner passes (the live user_id is a
    # partner). The swap pops user_id AND -- the pop-both fix -- the residual
    # employee_id, then plants driver_id. Result: a PURE driver swap.
    r = client.post(f"/view-as/driver/{driver.id}")
    assert r.status_code in (301, 302)
    assert r.headers["Location"].endswith("/my-profile")

    with client.session_transaction() as sess:
        # PURE driver swap: bound target planted, owner anchored, NO cross-kind
        # employee_id leftover (the pop-both fix), no user_id, no impersonate key.
        assert sess.get("driver_id") == driver.id
        assert sess.get("view_as_kind") == "driver"
        assert sess.get("view_as_principal_id") == driver.id
        assert sess.get("view_as_owner_uid") == partner.id
        assert sess.get("view_as_owner_sv") == partner.session_version
        assert sess.get("employee_id") is None, (
            "POP-BOTH REGRESSION: the linked partner's OWN employee_id survived the "
            "DRIVER swap -> the loader's cross-kind guard reads it as a foreign "
            "principal (_cross_present) and DESTROYS the genuine driver view-as"
        )
        assert sess.get("employee_session_version") is None
        assert "user_id" not in sess
        assert "impersonating_user_id" not in sess
        # partner_auth_ok is ALWAYS popped by the swap (no /partner escalation).
        assert sess.get("partner_auth_ok") is None

    # FOLLOW-UP request: load_current_user's no-uid branch sees the owner VALID,
    # the present driver principal == view_as_principal_id, AND -- thanks to the
    # pop-both fix -- NO cross-kind employee_id present (_cross_present False) ->
    # branch (ii): the GENUINE driver view-as REVIVES (not destroyed).
    rc = client.get("/__lp1_capture__")
    assert rc.status_code == 200, rc.get_data(as_text=True)[:300]
    assert captured.get("viewing_as") is True, (
        "POP-BOTH REGRESSION: a genuine DRIVER view-as for a LINKED-EMPLOYEE "
        f"partner did NOT revive (g.viewing_as={captured.get('viewing_as')!r}); the "
        "leftover employee_id from the UNIFY fold tripped the loader's cross-kind "
        "guard and destroyed the view-as"
    )
    assert captured.get("real_user_id") == partner.id

    # End-to-end: the driver self-view actually renders the TARGET driver (proves
    # the swap is a usable driver session, not a broken one).
    gp = client.get("/my-profile")
    assert gp.status_code == 200, gp.get_data(as_text=True)[:300]
    assert "Dorian Driver" in gp.get_data(as_text=True)

    # And the owner can exit cleanly -> management session restored (user_id ==
    # partner), the swapped driver identity dropped.
    rstop = client.get("/view-as/stop")
    assert rstop.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("user_id") == partner.id
        assert sess.get("user_session_version") == partner.session_version
        assert sess.get("driver_id") is None
        assert sess.get("view_as_owner_uid") is None
        assert sess.get("view_as_kind") is None
        assert sess.get("view_as_principal_id") is None

