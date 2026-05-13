"""Phase 1 / Block 1 — auto-clear tests for app.services.anomaly_engine.

Asserts _auto_clear stamps resolved_at when a reversible rule no longer
sees the subject in the new fire-set, and never touches non-reversible
rules.
"""
from __future__ import annotations

import pytest

from app.services.anomaly_engine import (
    SignalDraft,
    _auto_clear,
    _upsert_signal,
    REVERSIBLE_RULES,
)
from app.models import Signal


def _seed(db, rule_name: str, subject_id: str, store_id: str = "dos") -> Signal:
    d = SignalDraft(
        rule_name=rule_name, severity="warn",
        store_id=store_id, subject_id=subject_id,
        subject_label=f"Subj {subject_id}", payload={},
        action_text="-", surfaces=["x"], audience_roles=["partner"],
    )
    _was_new, row = _upsert_signal(db, d)
    return row


def test_reversible_rule_clears_when_subject_missing(db_session):
    """orders.late_delivery is reversible — when a previously-fired
    subject no longer shows up, the row's resolved_at gets stamped."""
    assert "orders.late_delivery" in REVERSIBLE_RULES
    _seed(db_session, "orders.late_delivery", "65X-814")
    _seed(db_session, "orders.late_delivery", "GONE-1")
    db_session.commit()
    # New fire only sees 65X-814; GONE-1 should auto-clear.
    seen = {("orders.late_delivery", "65X-814", "dos")}
    cleared = _auto_clear(db_session, "orders.late_delivery", seen)
    db_session.commit()
    assert cleared == 1
    gone = (db_session.query(Signal)
            .filter_by(subject_id="GONE-1").one())
    still = (db_session.query(Signal)
             .filter_by(subject_id="65X-814").one())
    assert gone.resolved_at is not None
    assert still.resolved_at is None


def test_non_reversible_rule_never_clears(db_session):
    """A rule NOT in REVERSIBLE_RULES — _auto_clear is a no-op even when
    subjects are missing from the new fire-set. The user must ack the
    signal explicitly."""
    rule = "vendor.invoice_over_quoted_price"
    assert rule not in REVERSIBLE_RULES
    _seed(db_session, rule, "INV-001")
    db_session.commit()
    cleared = _auto_clear(db_session, rule, seen_keys=set())
    db_session.commit()
    assert cleared == 0
    row = (db_session.query(Signal).filter_by(subject_id="INV-001").one())
    assert row.resolved_at is None


def test_acked_row_not_touched_by_autoclear(db_session):
    """Already-acked rows are 'closed' regardless of fire-set membership.
    They should not have resolved_at stamped because the timeline of
    'ack at X, auto-resolved at Y' would be misleading."""
    from datetime import datetime
    row = _seed(db_session, "orders.late_delivery", "ACKED-1")
    row.acknowledged_by = 1
    row.acknowledged_at = datetime.utcnow()
    db_session.commit()
    cleared = _auto_clear(db_session, "orders.late_delivery", seen_keys=set())
    db_session.commit()
    assert cleared == 0
    db_session.refresh(row)
    assert row.resolved_at is None
