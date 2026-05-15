"""Regression tests for Issue 4 — the driver-role-on-users leak
(Sam #1509 / samai #1511, 2026-05-15).

Drivers belong in the `drivers` table, not the `users` table. The team
admin page's role dropdown used to offer "Driver" as a value, which
created User rows with permission_level="driver" — wrong table, wrong
auth flow, wrong post-login routing. This file is the regression net:

  1. LEVEL_OPTIONS does NOT contain "driver".
  2. POST /partner/team/add with permission_level="driver" rejects with
     "Pick a role." and does NOT insert a User row.

Both tests were named verbatim by samai in the Issue 4 spec; do not
rename without coordinating with samai.
"""
from __future__ import annotations

import pytest
from werkzeug.security import generate_password_hash

from app.web.team_routes import LEVEL_OPTIONS


# ============================================================
# 1. LEVEL_OPTIONS regression
# ============================================================

def test_level_options_excludes_driver():
    """Driver must NOT appear as a user-creatable role. Single-line
    assertion serves as the regression net: if anyone re-adds the
    tuple, this test catches it before merge."""
    assert "driver" not in [k for k, _ in LEVEL_OPTIONS]


# ============================================================
# 2. POST /partner/team/add — driver role rejected
# ============================================================

@pytest.fixture
def app_with_partner(db_session, monkeypatch):
    """Spin up the Flask app + bind the in-memory session as the
    global SessionLocal so the team_add view sees the test DB. Seeds a
    real partner User row so require_level('partner') accepts the call.
    Mirrors the test_briefs_routes pattern (briefs uses the same auth
    decorator stack)."""
    from app import create_app
    from app import db as appdb
    from app.web import team_routes as team_mod
    from app.web import keypad_auth as keypad_mod
    from app.models import User

    # Seed a partner user the require_level check will load via
    # load_current_user(session["user_id"]).
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
    db_session.add(partner)
    db_session.commit()

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    # Bind the in-memory db_session as the global SessionLocal used
    # by route handlers + the keypad-auth attach-current-user hook.
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(team_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(keypad_mod, "SessionLocal", lambda: db_session)

    return flask_app, db_session


def _client_logged_in_as_partner(flask_app):
    """Return a test_client with the session keys that the global
    auth gate + the keypad-auth attach hook recognize as 'logged in
    partner'. session_version=1 must match the seeded partner."""
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess["partner_auth_ok"] = True
        sess["auth_ok"] = True
        sess["user_id"] = 1
        sess["user_session_version"] = 1
    return c


def test_team_add_rejects_driver_role_and_does_not_insert_user(app_with_partner):
    """POST /partner/team/add with permission_level="driver" should be
    rejected at the role-validation step. No User row inserted; no
    user_audit_log row for the rejected create attempt."""
    from app.models import User, UserAuditLog

    flask_app, db_session = app_with_partner
    client = _client_logged_in_as_partner(flask_app)

    label = "test_driver_leak"
    response = client.post(
        "/partner/team/add",
        data={
            "permission_level": "driver",
            "full_name": label,
            "stores": "tomball",
        },
        follow_redirects=False,
    )

    # team_add returns a 302 redirect to /partner/team with ?error=...
    # in the query string. The exact text is "Pick a role." (urlencoded
    # to "Pick+a+role." or "Pick%20a%20role." depending on the encoder).
    assert response.status_code in (302, 303), (
        f"Expected redirect, got {response.status_code}: "
        f"{response.get_data(as_text=True)[:300]}"
    )
    location = response.headers.get("Location", "")
    assert "/partner/team" in location, (
        f"Expected redirect to /partner/team, got {location!r}"
    )
    assert "Pick" in location and "role" in location, (
        f"Expected 'Pick a role' error in redirect, got {location!r}"
    )

    # No User row inserted under that label.
    inserted = db_session.query(User).filter(
        User.full_name == label).all()
    assert inserted == [], (
        f"Driver-role POST should not insert a User; found {inserted!r}"
    )

    # No audit log row for a create with that target_label.
    audit_create_rows = db_session.query(UserAuditLog).filter(
        UserAuditLog.target_label == label,
        UserAuditLog.action == "create",
    ).all()
    assert audit_create_rows == [], (
        f"No audit row should exist for the rejected create; "
        f"found {audit_create_rows!r}"
    )
