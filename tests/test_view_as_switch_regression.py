"""Regression probe (audit lens): the A->B switch path in view_as_start.

FIX-4 made view_as_start write a 'view_as_stop' audit row for the PRIOR
target before stamping the new one, REUSING the same open `db` session and
calling db.commit() inside _write_audit. The prod SessionLocal uses the
SQLAlchemy default expire_on_commit=True (app/db.py), so that intermediate
commit EXPIRES the already-loaded `target` instance. This test reproduces
the switch under a production-like (expire_on_commit=True) session bound to
the SAME engine the rest of the app uses, to prove the subsequent
target.id / target.full_name / target.permission_level accesses in the
'view_as_start' write do not raise (lazy-reload off the still-open session)
and the audit trail gets BOTH rows.

The shipped tests/test_view_as.py routes SessionLocal -> a single
expire_on_commit=False session, so it cannot observe this; we use a
dedicated engine here with expire_on_commit=True.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash


def _mk_user(uid, name, level, *, store_scope=None, active=True, session_version=1):
    from app.models import User
    return User(
        id=uid, full_name=name,
        email=f"{name.lower().replace(' ', '.')}.{uid}@test.local",
        passcode_hash=generate_password_hash("12345"),
        permission_level=level, store_scope=store_scope, active=active,
        first_login_done=True, session_version=session_version,
    )


@pytest.fixture
def prodlike_app(monkeypatch):
    """App wired to a SHARED in-memory engine via a PROD-LIKE sessionmaker
    (expire_on_commit=True, like app/db.py). Each SessionLocal() call returns
    a NEW Session on that engine — matching production semantics (the switch
    path opens one session, the loader opens another)."""
    from app.models import Base
    from app import create_app
    from app import db as appdb
    from app.web import view_as_routes as va_mod
    from app.web import keypad_auth as keypad_mod
    from app.web import employee_auth as emp_mod

    # StaticPool so every connection sees the same in-memory DB.
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(engine)
    # PROD-LIKE: expire_on_commit defaults to True (do NOT disable it).
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    seed = SessionFactory()
    seed.add_all([
        _mk_user(1, "Owner Partner", "partner"),
        _mk_user(2, "Gina Manager", "gm", store_scope="tomball"),
        _mk_user(4, "Kara Km", "km", store_scope="tomball"),
    ])
    seed.commit()
    seed.close()

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    flask_app = create_app()
    flask_app.config["TESTING"] = True

    monkeypatch.setattr(appdb, "SessionLocal", SessionFactory)
    monkeypatch.setattr(va_mod, "SessionLocal", SessionFactory)
    monkeypatch.setattr(keypad_mod, "SessionLocal", SessionFactory)
    monkeypatch.setattr(emp_mod, "SessionLocal", SessionFactory)

    return flask_app, engine, SessionFactory


def _login_partner(client):
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["user_session_version"] = 1
        sess["auth_ok"] = True
        sess["partner_auth_ok"] = True


def test_switch_A_to_B_does_not_raise_and_audits_both(prodlike_app):
    flask_app, engine, SessionFactory = prodlike_app
    client = flask_app.test_client()
    _login_partner(client)

    # Start viewing as gm (id=2).
    r1 = client.post("/view-as/2")
    assert r1.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") == 2

    # Switch DIRECTLY to km (id=4) WITHOUT stopping. This is the FIX-4 path:
    # prev (2) is not None and != 4, so _write_audit('view_as_stop') runs and
    # commits on the SAME db BEFORE the 'view_as_start' write touches `target`.
    # If the expired-instance reload broke, this request 500s.
    r2 = client.post("/view-as/4")
    assert r2.status_code in (301, 302), (
        f"switch A->B returned {r2.status_code}; "
        f"body={r2.get_data(as_text=True)[:400]}"
    )
    with client.session_transaction() as sess:
        assert sess.get("view_as_user_id") == 4
        assert "impersonating_user_id" not in sess

    # Audit trail: a 'view_as_stop' (auto-stop on switch) AND a 'view_as_start'
    # for the new target must both be present, with the start naming km's role.
    from app.models import UserAuditLog
    check = SessionFactory()
    try:
        actions = [r.action for r in check.query(UserAuditLog).all()]
        details = " || ".join(
            (r.details or "") for r in check.query(UserAuditLog).all()
        )
    finally:
        check.close()
    assert actions.count("view_as_start") == 2     # gm start + km start
    assert actions.count("view_as_stop") >= 1      # the auto-stop on switch
    assert "auto-stop view_as=2 on switch to 4" in details
    # The km start row's details embed the resolved target role (proves the
    # post-commit attribute access on `target` succeeded, not a None/raise).
    assert "(km)" in details


def test_switch_banner_follows_new_target(prodlike_app):
    """After an A->B switch the loader must resolve g.current_user to the NEW
    target (km), so the always-on banner names km, not gm."""
    flask_app, engine, SessionFactory = prodlike_app
    client = flask_app.test_client()
    _login_partner(client)

    assert client.post("/view-as/2").status_code in (301, 302)
    assert client.post("/view-as/4").status_code in (301, 302)

    # Picker renders base_dashboard.html -> the view_as_banner.
    r = client.get("/view-as")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "VIEW-AS (read-only)" in body
    assert "Kara Km" in body       # new target
    assert "Gina Manager" not in body.split("VIEW-AS")[1][:300]  # not the old one in the banner
