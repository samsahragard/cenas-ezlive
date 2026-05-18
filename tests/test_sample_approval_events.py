"""Gate 1 tests for /partner/developer/samples/approval-events endpoint
per scope #2736 + cena #2738 sign-off.

Tests query/filter logic directly against the SampleApproval ORM via the
db_session fixture (in-memory SQLite). Endpoint-level (Flask client +
permission gate) tests deferred to Gate 3 URL probes per spec pattern.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import SampleApproval, SampleApprovalAttachment


UTC = timezone.utc


def _mk_approval(db, slug, status, notes=None, user_id=None,
                 updated_at: datetime | None = None) -> SampleApproval:
    ap = SampleApproval(sample_slug=slug, status=status, notes=notes,
                        marked_by_user_id=user_id)
    db.add(ap)
    db.flush()
    if updated_at is not None:
        ap.updated_at = updated_at
        db.flush()
    return ap


def _filter_since(db, since_dt: datetime | None):
    """Mirrors the endpoint's row-selection logic."""
    q = db.query(SampleApproval)
    if since_dt is not None:
        q = q.filter(SampleApproval.updated_at > since_dt)
    return q.order_by(SampleApproval.updated_at.desc()).all()


class TestSinceCursorFilter:
    """Cursor monotonicity: since=T must exclude rows where updated_at <= T."""

    def test_no_since_returns_all_rows(self, db_session):
        _mk_approval(db_session, "a", "approved")
        _mk_approval(db_session, "b", "rejected")
        db_session.commit()
        rows = _filter_since(db_session, None)
        slugs = [r.sample_slug for r in rows]
        assert "a" in slugs
        assert "b" in slugs

    def test_since_excludes_older_rows(self, db_session):
        old = datetime(2026, 1, 1, tzinfo=UTC)
        new = datetime(2026, 5, 18, tzinfo=UTC)
        _mk_approval(db_session, "old-slug", "approved", updated_at=old)
        _mk_approval(db_session, "new-slug", "approved", updated_at=new)
        db_session.commit()
        cutoff = datetime(2026, 3, 1, tzinfo=UTC)
        rows = _filter_since(db_session, cutoff)
        slugs = [r.sample_slug for r in rows]
        assert "new-slug" in slugs
        assert "old-slug" not in slugs

    def test_since_strict_greater_than(self, db_session):
        """since=T returns rows with updated_at > T (NOT >= T) — caller
        re-using `now` from the previous response must not see the same
        rows twice."""
        ts = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)
        _mk_approval(db_session, "exact-ts", "approved", updated_at=ts)
        db_session.commit()
        rows = _filter_since(db_session, ts)
        assert rows == [], "since=T should NOT include rows with updated_at == T"

    def test_results_ordered_by_updated_at_desc(self, db_session):
        t1 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 5, 18, 11, 0, 0, tzinfo=UTC)
        t3 = datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC)
        _mk_approval(db_session, "first", "approved", updated_at=t1)
        _mk_approval(db_session, "third", "approved", updated_at=t3)
        _mk_approval(db_session, "second", "approved", updated_at=t2)
        db_session.commit()
        rows = _filter_since(db_session, None)
        slugs = [r.sample_slug for r in rows]
        assert slugs == ["third", "second", "first"]


class TestPayloadShape:
    """Latest-state-only per scope #2736 — one row per slug, no history."""

    def test_status_flip_keeps_latest_only(self, db_session):
        ap = _mk_approval(db_session, "flip-slug", "approved")
        db_session.commit()
        ap.status = "rejected"
        db_session.commit()
        ap.status = "approved"
        db_session.commit()
        rows = db_session.query(SampleApproval).filter_by(
            sample_slug="flip-slug"
        ).all()
        assert len(rows) == 1
        assert rows[0].status == "approved"

    def test_attachments_join_eager_load(self, db_session):
        ap = _mk_approval(db_session, "with-atts", "rejected")
        db_session.commit()
        for fn in ("a.png", "b.png", "c.png"):
            db_session.add(SampleApprovalAttachment(
                sample_approval_id=ap.id,
                filename=fn,
                mime_type="image/png",
                byte_size=1024,
                storage_path=f"{ap.id}/{fn}",
            ))
        db_session.commit()
        atts = db_session.query(SampleApprovalAttachment).filter_by(
            sample_approval_id=ap.id
        ).all()
        assert len(atts) == 3
        assert {a.filename for a in atts} == {"a.png", "b.png", "c.png"}

    def test_notes_field_round_trip(self, db_session):
        notes_text = "Approve — ship it. Photo content matches spec."
        _mk_approval(db_session, "notes-slug", "approved", notes=notes_text)
        db_session.commit()
        row = db_session.query(SampleApproval).filter_by(
            sample_slug="notes-slug"
        ).one()
        assert row.notes == notes_text


class TestEmptyAndEdgeCases:
    def test_empty_result_when_no_approvals(self, db_session):
        rows = _filter_since(db_session, None)
        assert rows == []

    def test_empty_result_when_since_in_future(self, db_session):
        _mk_approval(db_session, "now-slug", "approved")
        db_session.commit()
        future = datetime(2030, 1, 1, tzinfo=UTC)
        rows = _filter_since(db_session, future)
        assert rows == []
