"""Briefs blueprint — in-app surface for morning briefs + the calibration
feedback form.

Phase 1 / Block 6 calibration C2 (aick 2026-05-13). Surfaces:

  GET  /partner/briefs/<brief_id>             — render the brief body
  GET  /partner/briefs/<brief_id>/feedback    — feedback form
  POST /partner/briefs/<brief_id>/feedback    — submit feedback

Authorization model:
  - Decorator @requires_permission("briefs.view_own") gates the role.
    Partner gets it via wildcard; corporate explicitly. Other roles
    will be added when Phase 2 extends the panel to GM (Anna +
    Brittany — see permissions.py colocated comment).
  - Handler-side belt: brief.audience_user_id == g.current_user.id,
    OR partner wildcard. Without this, a corporate user could view
    another corporate user's brief by guessing brief_id.

Form-feedback wire (Round 2 of the calibration plan — Round 1 uses
email reply, samai/aick INSERTs the row manually):

  Click link in [Calibration] email  ──►  GET /feedback
                                          (if no row exists for
                                           (brief, user, submitted_via='form'),
                                           INSERT one with submitted_at=NULL —
                                           tracks engagement)

  Fill out + submit form             ──►  POST /feedback
                                          UPDATE the existing row, setting
                                          submitted_at + the answer fields.
                                          One-time transition: submitted_at
                                          must be NULL → non-NULL only.
                                          Resubmits are rejected (samai
                                          C2 review note B).
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, abort, g, redirect, render_template, request, url_for
from sqlalchemy import and_

from app.db import SessionLocal
from app.models import BriefFeedback, MorningBrief
from app.services.permissions import requires_permission


briefs_bp = Blueprint("briefs", __name__)


# Submission channels accepted by the POST handler (samai C2 review
# note A — DB col is String(20), not ENUM; we reject unknown values at
# application layer).
_SUBMIT_VIA_VALID = {"form"}   # email_reply rows are INSERTed by
                               # samai/aick out-of-band, not via this
                               # endpoint.


def _is_partner() -> bool:
    u = getattr(g, "current_user", None)
    return u is not None and getattr(u, "permission_level", None) == "partner"


def _load_brief_and_authz(brief_id: str) -> MorningBrief:
    """Resolve brief_id → MorningBrief row, then enforce audience match.
    Returns the row or aborts with 404 / 403."""
    db = SessionLocal()
    try:
        brief = (
            db.query(MorningBrief)
            .filter(MorningBrief.brief_id == brief_id)
            .first()
        )
        if brief is None:
            abort(404)
        u = getattr(g, "current_user", None)
        if u is None:
            abort(403)
        # Partner wildcard reaches everyone's briefs (oversight + Sam
        # is in the panel). Everyone else: only their own.
        if not _is_partner() and brief.audience_user_id != u.id:
            abort(403)
        return brief
    finally:
        db.close()


@briefs_bp.route("/partner/briefs/<brief_id>", methods=["GET"])
@requires_permission("briefs.view_own")
def show_brief(brief_id: str):
    """Render the brief body in-page. Uses the same Jinja partial the
    email path renders (templates/email/morning_brief.html per spec §11),
    wrapped in the in-app dashboard layout."""
    brief = _load_brief_and_authz(brief_id)
    # Synthesize sidebar context — these routes don't live under the
    # store_slug prefix, mirror the developer_chat pattern.
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    return render_template(
        "briefs/show.html",
        brief=brief,
        body=brief.body or {},
        active="briefs",
        page_title=f"Brief — {brief.brief_date.isoformat()}",
    )


@briefs_bp.route("/partner/briefs/<brief_id>/feedback", methods=["GET"])
@requires_permission("briefs.view_own")
def feedback_form(brief_id: str):
    """Render the feedback form. On first visit (no existing
    submitted_via='form' row for (brief, user)), INSERT a row with
    submitted_at=NULL — captures engagement-vs-completion split per
    Sam 20:47 docstring directive. Refreshing the page is idempotent
    (looks up existing row first)."""
    brief = _load_brief_and_authz(brief_id)
    u = g.current_user
    db = SessionLocal()
    try:
        existing = (
            db.query(BriefFeedback)
            .filter(and_(
                BriefFeedback.morning_brief_id == brief.id,
                BriefFeedback.user_id == u.id,
                BriefFeedback.submitted_via == "form",
            ))
            .first()
        )
        if existing is None:
            existing = BriefFeedback(
                morning_brief_id=brief.id,
                user_id=u.id,
                submitted_via="form",
                submitted_at=None,
            )
            db.add(existing)
            db.commit()
            db.refresh(existing)

        g.current_store = "partner"
        g.store_label = "Partner"
        g.current_location = "both"
        return render_template(
            "briefs/feedback.html",
            brief=brief,
            body=brief.body or {},
            feedback=existing,
            already_submitted=existing.submitted_at is not None,
            active="briefs",
            page_title=f"Feedback — {brief.brief_date.isoformat()}",
        )
    finally:
        db.close()


@briefs_bp.route("/partner/briefs/<brief_id>/feedback", methods=["POST"])
@requires_permission("briefs.view_own")
def feedback_submit(brief_id: str):
    """Save the form submission. Validates submitted_via (samai note A),
    enforces NULL → non-NULL one-time transition on submitted_at (samai
    note B), then UPDATEs the row's answer fields + submitted_at."""
    brief = _load_brief_and_authz(brief_id)
    u = g.current_user

    submitted_via = (request.form.get("submitted_via") or "form").strip()
    if submitted_via not in _SUBMIT_VIA_VALID:
        abort(400, description=f"unsupported submitted_via={submitted_via!r}")

    raw_score = (request.form.get("useful_score") or "").strip()
    useful_score: int | None
    if raw_score:
        try:
            n = int(raw_score)
            if not 1 <= n <= 5:
                abort(400, description="useful_score must be 1-5")
            useful_score = n
        except ValueError:
            abort(400, description="useful_score must be an integer 1-5")
    else:
        useful_score = None

    missed_something = (request.form.get("missed_something") or "").strip() or None
    was_noise = (request.form.get("was_noise") or "").strip() or None
    single_change = (request.form.get("single_change") or "").strip() or None

    db = SessionLocal()
    try:
        row = (
            db.query(BriefFeedback)
            .filter(and_(
                BriefFeedback.morning_brief_id == brief.id,
                BriefFeedback.user_id == u.id,
                BriefFeedback.submitted_via == "form",
            ))
            .first()
        )
        if row is None:
            # User POSTed without first visiting GET — INSERT fresh row.
            row = BriefFeedback(
                morning_brief_id=brief.id,
                user_id=u.id,
                submitted_via="form",
                submitted_at=datetime.utcnow(),
                useful_score=useful_score,
                missed_something=missed_something,
                was_noise=was_noise,
                single_change=single_change,
            )
            db.add(row)
            db.commit()
        else:
            # samai C2 review note B: one-time NULL → non-NULL transition
            # on submitted_at. Belt against accidental resubmit clobbering
            # prior answers.
            if row.submitted_at is not None:
                abort(409, description="feedback already submitted")
            row.submitted_at = datetime.utcnow()
            row.useful_score = useful_score
            row.missed_something = missed_something
            row.was_noise = was_noise
            row.single_change = single_change
            db.commit()
        return redirect(url_for(
            "briefs.feedback_form", brief_id=brief_id))
    finally:
        db.close()
