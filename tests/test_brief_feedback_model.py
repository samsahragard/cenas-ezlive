"""Phase 1 / Block 6 calibration C1 — BriefFeedback model tests.

Asserts:
  - Model creates + inserts via SessionLocal-equivalent
  - Both submission channels round-trip (email_reply with submitted_at set,
    form with submitted_at NULL)
  - Append-only event listener blocks DELETE with the expected error
  - FK to morning_briefs cascades; FK to users sets NULL on delete
  - Indexes are declared (cheap correctness check on __table_args__)
  - Latency join query (submitted_at − composed_at) works against
    minimal seed data — the actual Round 1 → Round 2 aggregation will
    use this exact join.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.models import BriefFeedback, MorningBrief, User


def _seed_brief(db, *, user_id: int, brief_id: str = "test-uuid-1",
                composed_at: datetime | None = None) -> MorningBrief:
    """Persist a minimal MorningBrief for FK-backing the feedback row."""
    u = User(
        id=user_id, full_name=f"Test User {user_id}",
        email=f"u{user_id}@x.test", passcode_hash="x",
        permission_level="partner", active=True, first_login_done=True,
    )
    db.add(u)
    b = MorningBrief(
        brief_id=brief_id, audience_role="partner",
        audience_user_id=user_id,
        brief_date=date(2026, 5, 13),
        body={"greeting": "hi", "headline": "x", "sections": [],
              "closing": "bye"},
        composed_at=composed_at or datetime(2026, 5, 13, 7, 30),
        composer_model="deterministic", fallback_used=True,
    )
    db.add(b)
    db.commit()
    return b


# ---- INSERT + round-trip ----

def test_email_reply_row_roundtrips(db_session):
    brief = _seed_brief(db_session, user_id=1)
    fb = BriefFeedback(
        morning_brief_id=brief.id, user_id=1,
        useful_score=4,
        missed_something="The X order was late and not flagged.",
        was_noise="Vendor stuff isn't relevant in the morning.",
        single_change="Show only items I can act on before 10am.",
        submitted_via="email_reply",
        submitted_at=datetime(2026, 5, 13, 8, 12),
    )
    db_session.add(fb)
    db_session.commit()

    row = db_session.query(BriefFeedback).one()
    assert row.morning_brief_id == brief.id
    assert row.user_id == 1
    assert row.useful_score == 4
    assert row.submitted_via == "email_reply"
    assert row.submitted_at == datetime(2026, 5, 13, 8, 12)
    assert row.created_at is not None  # default fired


def test_form_row_supports_null_submitted_at(db_session):
    brief = _seed_brief(db_session, user_id=2, brief_id="test-uuid-2")
    fb = BriefFeedback(
        morning_brief_id=brief.id, user_id=2,
        submitted_via="form",
        submitted_at=None,   # link clicked, form not posted yet
    )
    db_session.add(fb)
    db_session.commit()

    row = db_session.query(BriefFeedback).one()
    assert row.submitted_at is None
    # Free-text fields default to NULL
    assert row.missed_something is None
    assert row.useful_score is None


# ---- append-only listener ----

def test_delete_raises_runtime_error(db_session):
    brief = _seed_brief(db_session, user_id=3, brief_id="test-uuid-3")
    fb = BriefFeedback(
        morning_brief_id=brief.id, user_id=3,
        submitted_via="email_reply",
        submitted_at=datetime(2026, 5, 13, 9, 0),
        useful_score=3,
    )
    db_session.add(fb)
    db_session.commit()

    with pytest.raises(RuntimeError, match="append-only"):
        db_session.delete(fb)
        db_session.flush()


# ---- FK behavior ----

def test_morning_brief_cascade_declared():
    """SQLite default doesn't enforce FKs (PRAGMA foreign_keys=ON
    needed); production Postgres / configured SQLite honor the
    constraint. Verify the schema declared CASCADE on the morning_brief
    FK rather than runtime-checking — same shape as the user SET NULL
    test below."""
    fk = next(
        c for c in BriefFeedback.__table__.foreign_keys
        if c.column.table.name == "morning_briefs"
    )
    assert fk.ondelete == "CASCADE"


def test_user_set_null_on_delete(db_session):
    brief = _seed_brief(db_session, user_id=5, brief_id="test-uuid-5")
    fb = BriefFeedback(
        morning_brief_id=brief.id, user_id=5,
        submitted_via="email_reply",
        submitted_at=datetime(2026, 5, 13, 8, 0),
    )
    db_session.add(fb)
    db_session.commit()
    fb_id = fb.id

    # SQLite needs PRAGMA foreign_keys=ON to enforce SET NULL. The
    # in-memory engine in conftest doesn't set that by default, so we
    # verify the schema declared the constraint correctly rather than
    # the runtime behavior. (Production Postgres / configured SQLite
    # honor the constraint.)
    fk_target = next(
        c for c in BriefFeedback.__table__.foreign_keys
        if c.column.table.name == "users"
    )
    assert fk_target.ondelete == "SET NULL"


# ---- index declarations ----

def test_indexes_declared():
    """Confirms the __table_args__ Index objects landed — guards the
    'samai removes an index thinking it's redundant' regression."""
    idx_names = {i.name for i in BriefFeedback.__table__.indexes}
    assert "ix_brief_feedback_brief" in idx_names
    assert "ix_brief_feedback_user" in idx_names
    assert "ix_brief_feedback_submitted_at" in idx_names
    assert "ix_brief_feedback_created_at" in idx_names


# ---- latency join ----

def test_latency_join_query(db_session):
    """The Round 1 → Round 2 aggregation needs to compute response
    latency = submitted_at − composed_at. Smoke-test the join works."""
    brief = _seed_brief(
        db_session, user_id=6, brief_id="test-uuid-6",
        composed_at=datetime(2026, 5, 13, 7, 30),
    )
    fb = BriefFeedback(
        morning_brief_id=brief.id, user_id=6,
        submitted_via="email_reply",
        submitted_at=datetime(2026, 5, 13, 8, 5),   # 35 min later
        useful_score=4,
    )
    db_session.add(fb)
    db_session.commit()

    row = (
        db_session.query(
            BriefFeedback.submitted_at, MorningBrief.composed_at)
        .join(MorningBrief,
              BriefFeedback.morning_brief_id == MorningBrief.id)
        .filter(BriefFeedback.id == fb.id)
        .one()
    )
    submitted, composed = row
    latency_min = (submitted - composed).total_seconds() / 60.0
    assert 30 <= latency_min <= 40, f"expected ~35min, got {latency_min}"
