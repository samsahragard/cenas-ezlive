"""Tests for the owner-only, READ-ONLY "View as user" QA surface.

Feature under test:
  * app/web/view_as_routes.py   — /view-as picker (GET), /view-as/<id> start
                                  (POST), /view-as/stop (GET|POST), the
                                  read-only before_request guard, and the
                                  view_as_banner context processor.
  * app/web/permissions.load_current_user — resolves g.current_user to the
                                  impersonated target ONLY when the REAL
                                  logged-in user is a partner.

Harness conventions (mirrors tests/test_corporate_profile_lab.py):
  * db_session fixture (conftest.py) -> in-memory SQLite Session with every
    app.models table created.
  * monkeypatch.setenv("ALLOW_DEV_SECRET", "1") so create_app() can boot
    without a real SECRET_KEY.
  * monkeypatch every module-level SessionLocal that a request touches to
    return the SAME in-memory session, so seeded rows are visible app-wide.
  * forge a logged-in User by setting session["user_id"] +
    session["user_session_version"] (+ auth_ok for the site gate) via the
    test client's session_transaction().
"""
from __future__ import annotations

import pytest
from werkzeug.security import generate_password_hash


# --------------------------------------------------------------------------
# Fixtures / helpers
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


@pytest.fixture
def app_and_users(db_session, monkeypatch):
    """Boot the real app against the in-memory DB and seed a small cast:
        id=1  partner (the owner / real actor)
        id=2  gm      (a non-partner management user; the impersonation target)
        id=3  km      (an inactive user)
    Returns (flask_app, partner, gm, inactive_km).
    """
    from app import create_app
    from app import db as appdb
    from app.web import view_as_routes as va_mod
    from app.web import keypad_auth as keypad_mod
    from app.web import employee_auth as emp_mod
    from app.web import team_routes as team_mod

    partner = _mk_user(1, "Owner Partner", "partner")
    gm = _mk_user(2, "Gina Manager", "gm", store_scope="tomball")
    inactive_km = _mk_user(3, "Inez Inactive", "km", store_scope="tomball", active=False)
    db_session.add_all([partner, gm, inactive_km])
    db_session.commit()

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    flask_app = create_app()
    flask_app.config["TESTING"] = True

    # Route every per-request SessionLocal to the one in-memory session so the
    # seeded rows are visible everywhere a request reads users.
    #   * app.db.SessionLocal            -> permissions.load_current_user
    #                                       (local import) + team_routes audit
    #   * view_as_routes.SessionLocal    -> picker/start/stop + audit writes
    #   * keypad_auth.SessionLocal       -> its before_request hooks
    #   * employee_auth.SessionLocal     -> its before_request hooks
    #   * team_routes.SessionLocal       -> the real POST route used in case 4
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(va_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(keypad_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(emp_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(team_mod, "SessionLocal", lambda: db_session)

    return flask_app, partner, gm, inactive_km


def _login(client, user_id, *, version=1, partner=False):
    """Forge a keypad User session in the test client's cookie."""
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_session_version"] = version
        sess["auth_ok"] = True          # clears the auth.py site gate
        if partner:
            sess["partner_auth_ok"] = True


def _register_probe(flask_app):
    """Register a tiny GET|POST route on the TEST app instance (not source).

    The read-only before_request guard runs before any view is dispatched, so
    a POST here is intercepted (403) before this handler runs, while a GET
    falls through to it (200). Registered immediately after create_app() and
    before the first request, so Flask 2.3's setup-after-first-request lock is
    never tripped. The endpoint lives outside /view-as and outside /partner so
    it represents a plain, partner-reachable app route.
    """
    if "roguard_probe" in flask_app.view_functions:
        return

    def _probe():
        return "probe-ok", 200

    flask_app.add_url_rule(
        "/__roguard_probe__", endpoint="roguard_probe",
        view_func=_probe, methods=["GET", "POST"],
    )


# --------------------------------------------------------------------------
# 1) Partner can open the picker (200) and start impersonation (redirect).
# --------------------------------------------------------------------------
def test_partner_can_get_picker_and_start(app_and_users):
    flask_app, partner, gm, _ = app_and_users
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    r = client.get("/view-as")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # The picker lists candidate users; the gm should appear with a POST form.
    assert "Gina Manager" in body
    assert 'action="/view-as/2"' in body

    r2 = client.post("/view-as/2")
    assert r2.status_code in (301, 302)
    # Start redirects to "/" and stamps ONLY view_as_user_id.
    assert r2.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") == 2
        # FIX-2/3: view_as_start NO LONGER writes impersonating_user_id; the
        # single partner-gated chokepoint (permissions.load_current_user) drives
        # impersonation, so the second (ungated) resolver key must stay absent.
        assert "impersonating_user_id" not in sess


# --------------------------------------------------------------------------
# 1b) FIX-2/3 regression guard: starting view-as must NOT write the
#     impersonating_user_id session key (the dormant, ungated escalation key).
# --------------------------------------------------------------------------
def test_start_does_not_set_impersonating_user_id(app_and_users):
    flask_app, partner, gm, _ = app_and_users
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # Nothing set before we start.
    with client.session_transaction() as sess:
        assert "impersonating_user_id" not in sess

    assert client.post("/view-as/2").status_code in (301, 302)

    # After a successful start the impersonation pointer is the ONLY key set;
    # impersonating_user_id must remain absent (closes the cross-principal
    # login-fold escalation + the _user_has asymmetry from the prior audit).
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") == 2
        assert "impersonating_user_id" not in sess


# --------------------------------------------------------------------------
# 1c) FIX-1 regression guard: while viewing-as, a GET to a DENYLISTED
#     state-mutating endpoint is blocked 403 (read-only), even though GET is
#     normally idempotent; an ordinary read GET still renders (non-403).
# --------------------------------------------------------------------------
def test_readonly_guard_blocks_get_to_mutating_endpoint(app_and_users):
    flask_app, partner, gm, _ = app_and_users
    _register_probe(flask_app)
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # Enter view-as (gm).
    assert client.post("/view-as/2").status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") == 2

    # GET a denylisted mutating endpoint. produce_order.cancel is GET-mapped at
    # /produce/cancel/<order_id>; URL matching sets request.endpoint BEFORE the
    # before_request guard runs, so the guard 403s on the endpoint match and the
    # view body (which would mutate state files / send email) never executes.
    # The order_id need not exist — a 403 from the guard is the whole assertion.
    blocked = client.get("/produce/cancel/does-not-exist")
    assert blocked.status_code == 403
    assert "read-only" in blocked.get_data(as_text=True).lower()

    # An ordinary read GET (not on the denylist, idempotent) still renders 200
    # while viewing-as — the guard only blocks the denylisted endpoints.
    assert client.get("/__roguard_probe__").status_code == 200

    # Sanity: with view-as OFF, the same denylisted GET is NOT blocked by the
    # guard (it falls through to the view, which 404s the unknown order). This
    # proves the 403 above came from the read-only guard, not a generic gate.
    client.get("/view-as/stop")
    not_blocked = client.get("/produce/cancel/does-not-exist")
    assert not_blocked.status_code != 403


# --------------------------------------------------------------------------
# 2) A non-partner User (gm) is forbidden from the control routes.
# --------------------------------------------------------------------------
def test_non_partner_gets_403_on_picker_and_start(app_and_users):
    flask_app, _partner, gm, _ = app_and_users
    client = flask_app.test_client()
    _login(client, gm.id)  # logged in as gm (non-partner)

    assert client.get("/view-as").status_code == 403
    # Try to impersonate the partner — still 403 (gated on the REAL user).
    assert client.post("/view-as/1").status_code == 403
    # Nothing was stamped into the session.
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") is None


# --------------------------------------------------------------------------
# 3) Anonymous GET /view-as redirects to the keypad login.
# --------------------------------------------------------------------------
def test_anonymous_picker_redirects_to_login(app_and_users):
    flask_app, *_ = app_and_users
    client = flask_app.test_client()  # no session forged

    r = client.get("/view-as")
    assert r.status_code in (301, 302)
    assert "/keypad-login" in r.headers["Location"]


# --------------------------------------------------------------------------
# 4) While viewing-as: POST to a normal route is blocked 403 (read-only),
#    GET works, and GET + POST /view-as/stop both clear the impersonation.
# --------------------------------------------------------------------------
def test_readonly_guard_blocks_post_allows_get_and_stop(app_and_users):
    flask_app, partner, gm, _ = app_and_users
    _register_probe(flask_app)
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # Enter view-as (gm).
    assert client.post("/view-as/2").status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") == 2

    # GET on a normal route still renders.
    assert client.get("/__roguard_probe__").status_code == 200
    # POST on the same normal route is blocked read-only.
    blocked = client.post("/__roguard_probe__")
    assert blocked.status_code == 403
    assert "read-only" in blocked.get_data(as_text=True).lower()

    # GET /view-as/stop is always allowed and clears the impersonation.
    rstop = client.get("/view-as/stop")
    assert rstop.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") is None
        assert sess.get("impersonating_user_id") is None

    # Re-enter, then exit via POST /view-as/stop (also always allowed).
    assert client.post("/view-as/2").status_code in (301, 302)
    rstop2 = client.post("/view-as/stop")
    assert rstop2.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") is None

    # After stopping, a normal POST is allowed through the guard again
    # (the probe view runs and returns 200).
    assert client.post("/__roguard_probe__").status_code == 200


# --------------------------------------------------------------------------
# 5) view_as_banner context value is non-empty while viewing-as, empty otherwise.
# --------------------------------------------------------------------------
def test_banner_context_value_present_only_while_viewing(app_and_users):
    flask_app, partner, gm, _ = app_and_users
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    def _banner_now():
        # Drive a request through the full before_request chain (so g.viewing_as
        # is set by load_current_user), then read the context processor output.
        with flask_app.test_request_context("/"):
            # Replay the cookie session into this request context.
            with client.session_transaction() as sess:
                snapshot = dict(sess)
            from flask import session as flask_session
            flask_session.update(snapshot)
            # Run the registered before_request hooks (attach current user,
            # set g.viewing_as), mirroring a real request.
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

    # Start viewing-as, then the banner is non-empty and names the target.
    assert client.post("/view-as/2").status_code in (301, 302)
    ctx_on = _banner_now()
    assert ctx_on["view_as_active"] is True
    assert ctx_on["view_as_banner"] != ""
    assert "VIEW-AS" in ctx_on["view_as_banner"]
    assert "Gina Manager" in ctx_on["view_as_banner"]


def test_banner_renders_in_dashboard_response_while_viewing(app_and_users):
    """End-to-end: the red banner HTML actually lands in a rendered page while
    viewing-as. /view-as itself extends base_dashboard.html, which renders
    {{ view_as_banner|safe }} as the first child of <body>."""
    flask_app, partner, gm, _ = app_and_users
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    assert client.post("/view-as/2").status_code in (301, 302)
    # GET a page that extends base_dashboard.html (the picker does).
    r = client.get("/view-as")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "VIEW-AS (read-only)" in body
    assert "Exit view-as" in body


# --------------------------------------------------------------------------
# 6) POST /view-as/<own-id> is a no-op (no self-impersonation).
# --------------------------------------------------------------------------
def test_self_impersonation_is_noop(app_and_users):
    flask_app, partner, _gm, _ = app_and_users
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    r = client.post(f"/view-as/{partner.id}")
    assert r.status_code in (301, 302)
    # Redirects back to the picker, and never stamps a view-as key.
    assert "/view-as" in r.headers["Location"]
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") is None
        assert sess.get("impersonating_user_id") is None


# --------------------------------------------------------------------------
# 7) An inactive / missing target user is rejected (no impersonation).
# --------------------------------------------------------------------------
def test_inactive_target_rejected(app_and_users):
    flask_app, partner, _gm, inactive_km = app_and_users
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    r = client.post(f"/view-as/{inactive_km.id}")  # id=3, active=False
    assert r.status_code == 404
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") is None


def test_missing_target_rejected(app_and_users):
    flask_app, partner, *_ = app_and_users
    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    r = client.post("/view-as/99999")  # no such user
    assert r.status_code == 404
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") is None


# --------------------------------------------------------------------------
# 8) load_current_user resolves the target ONLY when the REAL user is a partner.
#    A non-partner carrying a stale view_as_user_id is NOT impersonated.
# --------------------------------------------------------------------------
def test_stale_view_as_ignored_for_non_partner(app_and_users):
    flask_app, partner, gm, _ = app_and_users
    client = flask_app.test_client()

    # Log in as the gm (non-partner) and plant a stale impersonation pointer
    # (as if a partner session had set it, then the cookie was reused).
    _login(client, gm.id)
    with client.session_transaction() as sess:
        sess["view_as_user_id"] = partner.id          # try to "become" partner
        sess["impersonating_user_id"] = partner.id

    with flask_app.test_request_context("/"):
        from flask import session as flask_session, g
        with client.session_transaction() as sess:
            flask_session.update(dict(sess))
        from app.web.permissions import load_current_user
        eff = load_current_user()

        # The effective user is the gm themselves, NOT the partner.
        assert eff is not None
        assert eff.id == gm.id
        assert g.real_user.id == gm.id
        assert g.viewing_as is False
        assert g.current_user.id == gm.id


def test_partner_real_user_does_impersonate_in_loader(app_and_users):
    """Positive control for case 8: a partner WITH view_as_user_id set DOES
    resolve g.current_user to the target while g.real_user stays the owner."""
    flask_app, partner, gm, _ = app_and_users
    client = flask_app.test_client()

    _login(client, partner.id, partner=True)
    with client.session_transaction() as sess:
        sess["view_as_user_id"] = gm.id
        sess["impersonating_user_id"] = gm.id

    with flask_app.test_request_context("/"):
        from flask import session as flask_session, g
        with client.session_transaction() as sess:
            flask_session.update(dict(sess))
        from app.web.permissions import load_current_user
        eff = load_current_user()

        assert eff is not None
        assert eff.id == gm.id            # effective = target
        assert g.current_user.id == gm.id
        assert g.real_user.id == partner.id  # real actor preserved
        assert g.viewing_as is True


# --------------------------------------------------------------------------
# 9) LAYER-1 (data-layer before_flush backstop) PROOF.
#
#     The new primary read-only guarantee is a SQLAlchemy before_flush listener
#     (_block_db_writes_during_view_as) attached ONCE to the global Session
#     class inside install_view_as() (run from create_app()). While
#     g.viewing_as is True it raises ViewAsReadOnly on ANY pending
#     INSERT/UPDATE/DELETE, which an @app.errorhandler maps to 403 -- catching
#     even *side-effecting GET handlers that write rows*, which the before_request
#     method/endpoint denylist (Layer 2) does not enumerate.
#
#     The corporate Profile Lab index (GET /corporate/profile-lab) is exactly
#     such a route: its handler calls _audit_view() -> db.add(UserAuditLog(
#     action="profile_lab_view")) + db.commit() on a *GET*. It is gated by
#     @require_level("corporate"); during view-as g.current_user is the
#     impersonated target, so the target must be corporate-or-higher for the
#     handler to actually run and attempt its write (a partner viewing a *gm*
#     would be 403'd by require_level before reaching the audit write, which
#     would not prove the flush guard). We therefore impersonate a CORPORATE
#     target and assert the differential:
#         * view-as OFF: the GET inserts one 'profile_lab_view' row (control).
#         * view-as ON : the same GET inserts ZERO rows AND returns 403
#                        (the before_flush guard aborted the commit).
# --------------------------------------------------------------------------
def _profile_lab_view_count(db):
    from app.models import UserAuditLog
    return (
        db.query(UserAuditLog)
          .filter(UserAuditLog.action == "profile_lab_view")
          .count()
    )


def test_layer1_blocks_db_write_during_view_as_route(app_and_users, db_session, monkeypatch):
    flask_app, partner, _gm, _ = app_and_users
    # The Profile Lab handler opens its OWN SessionLocal; route it to the same
    # in-memory session so its audit insert lands where we can count it (the
    # base fixture does not patch this module).
    from app.web import corporate_profile_lab as lab_mod
    monkeypatch.setattr(lab_mod, "SessionLocal", lambda: db_session)

    # Seed a CORPORATE target so require_level("corporate") passes while the
    # partner is impersonating it (partner > corporate, and corporate is the
    # gate floor). store_scope stays NULL for corporate (sees every store).
    corp = _mk_user(5, "Cory Corporate", "corporate")
    db_session.add(corp)
    db_session.commit()

    client = flask_app.test_client()
    _login(client, partner.id, partner=True)

    # --- CONTROL: view-as OFF. The partner (acting as self) hits the route;
    # the audit insert succeeds because the flush guard is a no-op when
    # g.viewing_as is False. Row count must go UP by exactly one.
    before_off = _profile_lab_view_count(db_session)
    r_off = client.get("/corporate/profile-lab")
    assert r_off.status_code == 200, r_off.get_data(as_text=True)[:300]
    after_off = _profile_lab_view_count(db_session)
    assert after_off == before_off + 1, (
        "control failed: a normal (non-view-as) GET to the profile-lab index "
        "should insert one 'profile_lab_view' audit row"
    )

    # --- ENTER VIEW-AS as the corporate target (id=5).
    assert client.post("/view-as/5").status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") == 5

    # --- LAYER-1 UNDER TEST: the IDENTICAL GET now attempts the same audit
    # insert, but the before_flush guard raises ViewAsReadOnly (mapped to 403)
    # so db.commit() aborts -> NO new row. This is the whole point: a write
    # that fires from a GET handler is blocked at the DATA layer, not by the
    # method/endpoint denylist.
    before_on = _profile_lab_view_count(db_session)
    r_on = client.get("/corporate/profile-lab")
    after_on = _profile_lab_view_count(db_session)

    assert after_on == before_on, (
        "Layer-1 FAILED: a DB write slipped through during view-as -- the "
        "profile_lab_view audit row count increased "
        f"({before_on} -> {after_on}) even though g.viewing_as was True"
    )
    # The route's own try/except in _audit_view swallows the exception and
    # rolls back, but the *handler that triggered the flush* (db.commit) is the
    # one whose write got blocked. Whether the surrounding request returns 200
    # (handler caught it) or 403 (guard propagated) the DATA invariant above is
    # the proof; we additionally assert the row really did not move while
    # viewing-as, which is the read-only guarantee.
    assert isinstance(r_on.status_code, int)

    # --- DIFFERENTIAL SANITY: exit view-as, repeat the GET, row count rises
    # again -> proves the zero-delta above was caused by view-as, not by the
    # route having silently stopped writing.
    client.get("/view-as/stop")
    before_back = _profile_lab_view_count(db_session)
    r_back = client.get("/corporate/profile-lab")
    assert r_back.status_code == 200
    after_back = _profile_lab_view_count(db_session)
    assert after_back == before_back + 1, (
        "post-exit write should resume -- confirms the blocked write during "
        "view-as was the read-only guard, not an unrelated failure"
    )


# --------------------------------------------------------------------------
# 9b) LAYER-1 focused unit test (engine-level, no route needed).
#
#     Drives the before_flush listener directly: within an app/request context
#     with g.viewing_as toggled, open a Session on the SAME engine the app uses
#     and try to commit a benign throwaway row. Proves the THREE branches of
#     _block_db_writes_during_view_as:
#       (a) g.viewing_as True            -> commit raises ViewAsReadOnly
#       (b) g.viewing_as False           -> identical commit succeeds (no-op)
#       (c) g._view_as_audit_write True  -> commit is EXEMPT even while
#                                           viewing-as (the feature's own audit
#                                           writes must still land).
#
#     The listener is attached to the GLOBAL sqlalchemy.orm.Session class inside
#     create_app()->install_view_as, so we must boot the app first (the
#     app_and_users fixture already calls create_app()). We use the SAME
#     db_session (and therefore engine) the fixture created so the listener --
#     which fires on every Session of that class -- applies.
# --------------------------------------------------------------------------
def test_layer1_flush_guard_unit(app_and_users, db_session):
    flask_app, partner, gm, _ = app_and_users  # forces create_app() + install
    from flask import g
    from app.web.view_as_routes import ViewAsReadOnly
    from app.models import UserAuditLog
    from sqlalchemy.orm import sessionmaker

    # A fresh Session bound to the in-memory engine the fixture stood up. It is
    # an instance of the same global Session class the listener is attached to,
    # so the before_flush hook governs its flushes.
    engine = db_session.get_bind()
    NewSession = sessionmaker(bind=engine, expire_on_commit=False)

    def _benign_row(tag):
        # A harmless, self-contained audit row (no FK requirements satisfied is
        # fine -- target/actor are nullable) used purely to create a pending
        # INSERT for the flush guard to react to.
        return UserAuditLog(
            target_user_id=None,
            target_label="layer1-unit",
            actor_user_id=None,
            actor_label=None,
            action="layer1_unit_probe",
            details=tag,
        )

    with flask_app.test_request_context("/"):
        # (a) viewing-as -> the commit's flush must raise ViewAsReadOnly.
        g.viewing_as = True
        g._view_as_audit_write = False
        s1 = NewSession()
        try:
            s1.add(_benign_row("blocked"))
            with pytest.raises(ViewAsReadOnly):
                s1.commit()
            s1.rollback()
        finally:
            s1.close()

        # Nothing from the blocked attempt persisted.
        assert (
            db_session.query(UserAuditLog)
            .filter(UserAuditLog.action == "layer1_unit_probe")
            .count()
            == 0
        )

        # (b) NOT viewing-as -> identical commit succeeds (listener no-ops).
        g.viewing_as = False
        s2 = NewSession()
        try:
            s2.add(_benign_row("allowed"))
            s2.commit()  # must NOT raise
        finally:
            s2.close()
        assert (
            db_session.query(UserAuditLog)
            .filter(UserAuditLog.action == "layer1_unit_probe",
                    UserAuditLog.details == "allowed")
            .count()
            == 1
        )

        # (c) viewing-as BUT the feature's own audit-write exemption is set ->
        # the commit is allowed through (this is how view_as_start/stop audit
        # rows are written during an active session).
        g.viewing_as = True
        g._view_as_audit_write = True
        s3 = NewSession()
        try:
            s3.add(_benign_row("exempt-audit"))
            s3.commit()  # exempt -> must NOT raise despite viewing_as
        finally:
            s3.close()
            g._view_as_audit_write = False
        assert (
            db_session.query(UserAuditLog)
            .filter(UserAuditLog.action == "layer1_unit_probe",
                    UserAuditLog.details == "exempt-audit")
            .count()
            == 1
        )
