"""Phase 2 / Block 1 precondition — ScheduledEvent model tests.

Model-only commit, so coverage is small (precondition spec §4):
  - round-trip: all 8 named fields persist + read back; a point-in-time
    event (scheduled_end_at=None) round-trips fine
  - default: a row created without explicit status gets "scheduled";
    created_at / updated_at default-fire
  - index declared: ix_scheduled_events_ribbon present in __table_args__
  - valid-value constants: the 3 module-level constant sets exist + hold
    the documented values (the Block 2 admin form + 1C adapter validate
    against them)
"""
from __future__ import annotations

from datetime import datetime

from app.models import (
    ScheduledEvent,
    _VALID_EVENT_STORES,
    _VALID_EVENT_CATEGORIES,
    _VALID_EVENT_STATUSES,
)


# ---- round-trip ----

def test_scheduled_event_roundtrips_all_fields(db_session):
    ev = ScheduledEvent(
        store="tomball",
        category="catering",
        title="Henderson wedding — 120 plates",
        scheduled_at=datetime(2026, 5, 20, 17, 0),
        scheduled_end_at=datetime(2026, 5, 20, 21, 0),
        status="confirmed",
        notes="Drop at the country club, ask for Linda.",
    )
    db_session.add(ev)
    db_session.commit()

    row = db_session.query(ScheduledEvent).one()
    assert row.store == "tomball"
    assert row.category == "catering"
    assert row.title == "Henderson wedding — 120 plates"
    assert row.scheduled_at == datetime(2026, 5, 20, 17, 0)
    assert row.scheduled_end_at == datetime(2026, 5, 20, 21, 0)
    assert row.status == "confirmed"
    assert row.notes == "Drop at the country club, ask for Linda."
    assert row.created_at is not None
    assert row.updated_at is not None


def test_point_in_time_event_roundtrips_with_null_end(db_session):
    """A catering delivery is point-in-time — scheduled_end_at=None."""
    ev = ScheduledEvent(
        store="copperfield",
        category="catering",
        title="Office lunch drop",
        scheduled_at=datetime(2026, 5, 15, 11, 30),
        scheduled_end_at=None,
        status="scheduled",
    )
    db_session.add(ev)
    db_session.commit()

    row = db_session.query(ScheduledEvent).one()
    assert row.scheduled_end_at is None
    assert row.notes is None  # nullable, unset


# ---- defaults ----

def test_status_defaults_to_scheduled(db_session):
    ev = ScheduledEvent(
        store="both",
        category="event",
        title="Astros home game — expect late dinner rush",
        scheduled_at=datetime(2026, 5, 18, 19, 0),
    )
    db_session.add(ev)
    db_session.commit()

    row = db_session.query(ScheduledEvent).one()
    assert row.status == "scheduled"
    assert row.created_at is not None
    assert row.updated_at is not None


# ---- index declaration ----

def test_ribbon_index_declared():
    """ix_scheduled_events_ribbon (store, status, scheduled_at) is the
    1C ribbon query's covering index — guards the 'someone removes it
    thinking it's redundant' regression."""
    idx_names = {i.name for i in ScheduledEvent.__table__.indexes}
    assert "ix_scheduled_events_ribbon" in idx_names


# ---- valid-value constants ----

def test_valid_value_constants():
    assert _VALID_EVENT_STORES == {"tomball", "copperfield", "both"}
    assert _VALID_EVENT_CATEGORIES == {"catering", "event"}
    assert _VALID_EVENT_STATUSES == {
        "scheduled", "confirmed", "completed", "cancelled"}
