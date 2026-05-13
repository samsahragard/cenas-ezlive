"""Phase 1 / Block 1 — dedup tests for app.services.anomaly_engine.

Asserts _upsert_signal upserts on (rule_name, subject_id, store_id) and
leaves resolved / acked rows alone (so a re-fire creates a fresh row
after acknowledge).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.anomaly_engine import SignalDraft, _upsert_signal
from app.models import Signal


def _draft(**over) -> SignalDraft:
    base = dict(
        rule_name="orders.late_delivery",
        severity="warn",
        store_id="dos",
        subject_id="65X-814",
        subject_label="Order 65X-814",
        payload={"minutes_late": 5},
        action_text="Call driver.",
        surfaces=["orders.by_store"],
        audience_roles=["partner", "gm"],
    )
    base.update(over)
    return SignalDraft(**base)


def test_first_fire_creates_row(db_session):
    was_new, row = _upsert_signal(db_session, _draft())
    db_session.commit()
    assert was_new is True
    assert row.id is not None
    assert db_session.query(Signal).count() == 1


def test_second_fire_same_key_updates_existing(db_session):
    _upsert_signal(db_session, _draft(payload={"minutes_late": 5}))
    was_new, row = _upsert_signal(
        db_session, _draft(payload={"minutes_late": 15}))
    db_session.commit()
    assert was_new is False
    assert db_session.query(Signal).count() == 1
    assert row.payload["minutes_late"] == 15  # updated, not appended


def test_different_subject_creates_separate_row(db_session):
    _upsert_signal(db_session, _draft(subject_id="65X-814"))
    _upsert_signal(db_session, _draft(subject_id="DIFFERENT-1"))
    db_session.commit()
    assert db_session.query(Signal).count() == 2


def test_different_store_creates_separate_row(db_session):
    _upsert_signal(db_session, _draft(store_id="dos"))
    _upsert_signal(db_session, _draft(store_id="uno"))
    db_session.commit()
    assert db_session.query(Signal).count() == 2


def test_acked_row_does_not_collide_with_new_fire(db_session):
    """After a signal is acknowledged, a subsequent fire creates a
    fresh row so the next ack tracks the new event."""
    _was_new, first = _upsert_signal(db_session, _draft())
    first.acknowledged_by = 1
    first.acknowledged_at = datetime.utcnow()
    db_session.commit()
    was_new, second = _upsert_signal(db_session, _draft())
    db_session.commit()
    assert was_new is True  # fresh row even though same (rule, subject, store)
    assert second.id != first.id
    assert db_session.query(Signal).count() == 2


def test_resolved_row_does_not_collide_with_new_fire(db_session):
    """Same as ack: a previously-auto-cleared row stays closed; a new
    fire opens a fresh one."""
    _was_new, first = _upsert_signal(db_session, _draft())
    first.resolved_at = datetime.utcnow()
    db_session.commit()
    was_new, second = _upsert_signal(db_session, _draft())
    db_session.commit()
    assert was_new is True
    assert second.id != first.id
