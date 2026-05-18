"""Gate 1 tests for samples approval workflow per spec_samples_approval_workflow §11.

Tests the DB model contracts + helper logic directly via db_session fixture
(in-memory SQLite). Flask-route + auth-gate tests deferred to Gate 3 URL probes
+ Playwright batch per Sam #2547.
"""
from __future__ import annotations

import pytest

from app.models import SampleApproval, SampleApprovalAttachment


def _mk_approval(db, slug: str, status: str = "pending", notes: str | None = None,
                 user_id: int | None = None) -> SampleApproval:
    ap = SampleApproval(sample_slug=slug, status=status, notes=notes,
                        marked_by_user_id=user_id)
    db.add(ap)
    db.flush()
    return ap


def _mk_attachment(db, approval_id: int, filename: str = "shot.png",
                   byte_size: int = 1024) -> SampleApprovalAttachment:
    att = SampleApprovalAttachment(
        sample_approval_id=approval_id,
        filename=filename,
        mime_type="image/png",
        byte_size=byte_size,
        storage_path=f"{approval_id}/{filename}",
        created_by_user_id=None,
    )
    db.add(att)
    db.flush()
    return att


class TestSampleApprovalRoundtrip:
    """Spec §11: persistence across read/write."""

    def test_approve_sets_status_approved(self, db_session):
        ap = _mk_approval(db_session, "drivers-redesign-v2", status="pending")
        db_session.commit()
        ap.status = "approved"
        ap.notes = "Looks good"
        db_session.commit()
        fetched = db_session.query(SampleApproval).filter_by(
            sample_slug="drivers-redesign-v2"
        ).one()
        assert fetched.status == "approved"
        assert fetched.notes == "Looks good"

    def test_reject_sets_status_rejected(self, db_session):
        ap = _mk_approval(db_session, "right-sidebar-plan-v1", status="pending")
        db_session.commit()
        ap.status = "rejected"
        ap.notes = "Spatial fit unclear"
        db_session.commit()
        fetched = db_session.query(SampleApproval).filter_by(
            sample_slug="right-sidebar-plan-v1"
        ).one()
        assert fetched.status == "rejected"
        assert fetched.notes == "Spatial fit unclear"

    def test_approval_persists_across_request(self, db_session):
        """Closing + reopening session should still find the row."""
        _mk_approval(db_session, "build-plan", status="approved", notes="ship")
        db_session.commit()
        # Simulate request boundary
        db_session.expire_all()
        fetched = db_session.query(SampleApproval).filter_by(
            sample_slug="build-plan"
        ).one_or_none()
        assert fetched is not None
        assert fetched.status == "approved"
        assert fetched.notes == "ship"

    def test_status_flip_keeps_single_row_per_slug(self, db_session):
        """v1 keeps only latest state — one row per sample_slug, updated in place."""
        ap = _mk_approval(db_session, "legal-overview", status="approved")
        db_session.commit()
        ap.status = "rejected"
        db_session.commit()
        ap.status = "pending"
        db_session.commit()
        rows = db_session.query(SampleApproval).filter_by(
            sample_slug="legal-overview"
        ).all()
        assert len(rows) == 1, f"expected exactly 1 row per slug, got {len(rows)}"
        assert rows[0].status == "pending"


class TestSampleApprovalAttachments:
    """Spec §2.2 + §11: attachment lifecycle + size cap behavior."""

    def test_attachment_belongs_to_approval(self, db_session):
        ap = _mk_approval(db_session, "drivers-redesign-v2")
        db_session.commit()
        att = _mk_attachment(db_session, ap.id, filename="correction.png")
        db_session.commit()
        fetched = db_session.query(SampleApprovalAttachment).filter_by(
            sample_approval_id=ap.id
        ).all()
        assert len(fetched) == 1
        assert fetched[0].filename == "correction.png"
        assert fetched[0].storage_path == f"{ap.id}/correction.png"

    def test_size_limit_5mb_per_file(self):
        """Application-level cap per spec §2.2: 5 MB per file."""
        from app.web.developer_chat import SAMPLE_APPROVAL_MAX_BYTES
        assert SAMPLE_APPROVAL_MAX_BYTES == 5 * 1024 * 1024

    def test_allowed_mimes_image_only(self):
        from app.web.developer_chat import SAMPLE_APPROVAL_ALLOWED_MIMES
        assert "image/png" in SAMPLE_APPROVAL_ALLOWED_MIMES
        assert "image/jpeg" in SAMPLE_APPROVAL_ALLOWED_MIMES
        assert "image/webp" in SAMPLE_APPROVAL_ALLOWED_MIMES
        # No executables, no PDFs
        assert "application/pdf" not in SAMPLE_APPROVAL_ALLOWED_MIMES
        assert "application/x-msdownload" not in SAMPLE_APPROVAL_ALLOWED_MIMES

    def test_multiple_attachments_per_approval(self, db_session):
        ap = _mk_approval(db_session, "drivers-redesign-v2")
        db_session.commit()
        _mk_attachment(db_session, ap.id, filename="shot1.png")
        _mk_attachment(db_session, ap.id, filename="shot2.png")
        _mk_attachment(db_session, ap.id, filename="shot3.webp")
        db_session.commit()
        atts = db_session.query(SampleApprovalAttachment).filter_by(
            sample_approval_id=ap.id
        ).all()
        assert len(atts) == 3


class TestSampleApprovalSlugLookup:
    """The view enriches each SAMPLES dict by joining on slug — verify slug match."""

    def test_resolve_slug_finds_match(self):
        from app.web.developer_chat import _resolve_sample_slug
        s = _resolve_sample_slug("drivers-redesign-v2")
        assert s is not None
        assert s["title"] == "Drivers Page Redesign"

    def test_resolve_unknown_slug_returns_none(self):
        from app.web.developer_chat import _resolve_sample_slug
        assert _resolve_sample_slug("bogus-slug-xyz") is None

    def test_all_samples_have_slug(self):
        """Every entry in SAMPLES must have a slug for approval-state join."""
        from app.web.developer_chat import SAMPLES
        for s in SAMPLES:
            assert "slug" in s, f"sample {s.get('title')} missing slug"
            assert s["slug"], f"sample {s.get('title')} has empty slug"
