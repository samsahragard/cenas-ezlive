"""Phase 1 / Block 6 calibration C2 — briefs routes tests.

Covers:
  - GET /partner/briefs/<brief_id> reads the brief, returns 200 for owner
    + partner, 404 for unknown brief, 403 for other user's brief
  - GET /partner/briefs/<brief_id>/feedback renders form + INSERTs a
    BriefFeedback row with submitted_at=NULL on first visit (click
    engagement tracking)
  - GET is idempotent — refresh doesn't INSERT a second row
  - POST /feedback UPDATEs the existing row, setting submitted_at +
    answer fields
  - POST without prior GET creates fresh row with submitted_at set
  - POST rejects unknown submitted_via (samai C2 review note A)
  - POST rejects useful_score outside 1-5
  - POST rejects resubmit when submitted_at already non-NULL (samai
    C2 review note B — one-time NULL→non-NULL transition)
"""
from __future__ import annotations

import os
from datetime import date, datetime

import pytest


@pytest.fixture
def app_with_user(db_session, monkeypatch):
    """Spin up the Flask app with the in-memory db_session bound. Seeds
    a partner user (id=1) and a corporate user (id=2) in the same DB,
    and yields the app + helpers for issuing authenticated requests."""
    from app.models import User
    partner = User(
        id=1, full_name="Sam Sahragard", email="sam@x.test",
        passcode_hash="x", permission_level="partner",
        active=True, first_login_done=True,
    )
    corp = User(
        id=2, full_name="Masood C", email="masood@x.test",
        passcode_hash="x", permission_level="corporate",
        active=True, first_login_done=True,
    )
    db_session.add_all([partner, corp])
    db_session.commit()

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")

    # Bind db_session as the global session factory the routes use.
    import app.db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    import app.web.briefs as briefs_mod
    monkeypatch.setattr(briefs_mod, "SessionLocal", lambda: db_session)

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True

    def _client_for(user_id: int):
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["partner_auth_ok"] = True
            sess["auth_ok"] = True
            sess["user_id"] = user_id
            # load_current_user() rejects the session unless this matches
            # User.session_version (defaults to 1 for fresh users).
            sess["user_session_version"] = 1
        return c

    yield app, _client_for, db_session


def _seed_brief(db, *, brief_id: str, audience_user_id: int):
    from app.models import MorningBrief
    b = MorningBrief(
        brief_id=brief_id, audience_role="partner",
        audience_user_id=audience_user_id,
        brief_date=date(2026, 5, 13),
        body={"greeting": "Good morning, Sam.", "headline": "Quiet.",
              "sections": [], "closing": "Have a strong day."},
        composed_at=datetime(2026, 5, 13, 7, 30),
        composer_model="deterministic", fallback_used=True,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


# ---- GET /partner/briefs/<brief_id> ----

def test_show_brief_returns_200_for_owner(app_with_user):
    app, client_for, db = app_with_user
    brief = _seed_brief(db, brief_id="brief-1", audience_user_id=1)
    r = client_for(1).get(f"/partner/briefs/{brief.brief_id}")
    assert r.status_code == 200
    assert b"Good morning, Sam." in r.data


def test_show_brief_returns_200_for_partner_viewing_others(app_with_user):
    """Partner wildcard reaches all briefs."""
    app, client_for, db = app_with_user
    brief = _seed_brief(db, brief_id="brief-2", audience_user_id=2)
    r = client_for(1).get(f"/partner/briefs/{brief.brief_id}")
    assert r.status_code == 200


def test_show_brief_returns_403_for_other_corporate(app_with_user):
    """Corporate user can't view a brief addressed to a different user."""
    app, client_for, db = app_with_user
    # Seed a third corporate user
    from app.models import User
    other = User(
        id=3, full_name="Angelica", email="ang@x.test",
        passcode_hash="x", permission_level="corporate",
        active=True, first_login_done=True,
    )
    db.add(other); db.commit()
    brief = _seed_brief(db, brief_id="brief-3", audience_user_id=3)
    r = client_for(2).get(f"/partner/briefs/{brief.brief_id}")
    assert r.status_code == 403


def test_show_brief_returns_404_for_unknown_brief(app_with_user):
    app, client_for, db = app_with_user
    r = client_for(1).get("/partner/briefs/nonexistent-id")
    assert r.status_code == 404


# ---- GET /partner/briefs/<brief_id>/feedback ----

def test_feedback_get_inserts_engagement_row(app_with_user):
    """First visit to the form INSERTs a BriefFeedback row with
    submitted_at=NULL — tracks engagement-vs-completion split."""
    app, client_for, db = app_with_user
    brief = _seed_brief(db, brief_id="brief-4", audience_user_id=2)
    from app.models import BriefFeedback
    assert db.query(BriefFeedback).count() == 0
    r = client_for(2).get(f"/partner/briefs/{brief.brief_id}/feedback")
    assert r.status_code == 200
    rows = db.query(BriefFeedback).all()
    assert len(rows) == 1
    assert rows[0].submitted_via == "form"
    assert rows[0].submitted_at is None
    assert rows[0].user_id == 2


def test_feedback_get_is_idempotent(app_with_user):
    """Refreshing the form doesn't INSERT a second row."""
    app, client_for, db = app_with_user
    brief = _seed_brief(db, brief_id="brief-5", audience_user_id=2)
    c = client_for(2)
    c.get(f"/partner/briefs/{brief.brief_id}/feedback")
    c.get(f"/partner/briefs/{brief.brief_id}/feedback")
    c.get(f"/partner/briefs/{brief.brief_id}/feedback")
    from app.models import BriefFeedback
    assert db.query(BriefFeedback).count() == 1


# ---- POST /partner/briefs/<brief_id>/feedback ----

def test_feedback_post_updates_existing_engagement_row(app_with_user):
    app, client_for, db = app_with_user
    brief = _seed_brief(db, brief_id="brief-6", audience_user_id=2)
    c = client_for(2)
    # GET first to create the engagement row
    c.get(f"/partner/briefs/{brief.brief_id}/feedback")
    # POST submits
    r = c.post(
        f"/partner/briefs/{brief.brief_id}/feedback",
        data={
            "submitted_via": "form",
            "useful_score": "4",
            "missed_something": "The X order",
            "was_noise": "Vendor stuff",
            "single_change": "Show only items I can act on",
        },
    )
    assert r.status_code in (302, 303)  # redirect to feedback form
    from app.models import BriefFeedback
    rows = db.query(BriefFeedback).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.submitted_at is not None
    assert row.useful_score == 4
    assert row.missed_something == "The X order"
    assert row.was_noise == "Vendor stuff"
    assert row.single_change == "Show only items I can act on"


def test_feedback_post_without_get_creates_fresh_row(app_with_user):
    """If user POSTs without first visiting GET, INSERT a fresh row
    with submitted_at set directly."""
    app, client_for, db = app_with_user
    brief = _seed_brief(db, brief_id="brief-7", audience_user_id=2)
    r = client_for(2).post(
        f"/partner/briefs/{brief.brief_id}/feedback",
        data={"submitted_via": "form", "useful_score": "3"},
    )
    assert r.status_code in (302, 303)
    from app.models import BriefFeedback
    rows = db.query(BriefFeedback).all()
    assert len(rows) == 1
    assert rows[0].submitted_at is not None


def test_feedback_post_rejects_unknown_submitted_via(app_with_user):
    """samai C2 review note A: validate submitted_via at application
    layer since DB col is String(20) not ENUM."""
    app, client_for, db = app_with_user
    brief = _seed_brief(db, brief_id="brief-8", audience_user_id=2)
    r = client_for(2).post(
        f"/partner/briefs/{brief.brief_id}/feedback",
        data={"submitted_via": "telegram", "useful_score": "3"},
    )
    assert r.status_code == 400


def test_feedback_post_rejects_score_outside_range(app_with_user):
    app, client_for, db = app_with_user
    brief = _seed_brief(db, brief_id="brief-9", audience_user_id=2)
    r = client_for(2).post(
        f"/partner/briefs/{brief.brief_id}/feedback",
        data={"submitted_via": "form", "useful_score": "9"},
    )
    assert r.status_code == 400


def test_feedback_post_rejects_resubmit(app_with_user):
    """samai C2 review note B: one-time NULL→non-NULL transition on
    submitted_at. Second POST after first submission must reject."""
    app, client_for, db = app_with_user
    brief = _seed_brief(db, brief_id="brief-10", audience_user_id=2)
    c = client_for(2)
    c.post(
        f"/partner/briefs/{brief.brief_id}/feedback",
        data={"submitted_via": "form", "useful_score": "4"},
    )
    # Second POST should reject — submitted_at already non-NULL
    r = c.post(
        f"/partner/briefs/{brief.brief_id}/feedback",
        data={"submitted_via": "form", "useful_score": "5"},
    )
    assert r.status_code == 409
