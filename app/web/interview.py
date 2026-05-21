"""Interview Tracker — Partner-only candidate hiring pipeline.

NEW feature (Sam #5:48). A 4-stage pipeline — Applied / 1st Interview /
2nd Interview / Hired — over the `Candidate` model. dck delivered the
production template (interview_tracker.html); this blueprint wires the
route contract that template documents at its top.

Like developer_chat, these routes live at /partner/interview-tracker
directly (not under store_bp's <store> prefix), so they re-check the
partner session flag via the shared `_enforce_partner()` helper rather
than relying on store_bp.before_request.

Routes:
    GET /partner/interview-tracker      — the pipeline board + detail
    GET /partner/interview-tracker/new  — add-candidate placeholder (v1)
"""
from __future__ import annotations

import logging

from flask import Blueprint, render_template, request, redirect, url_for, g

from app.db import SessionLocal
from app.models import Candidate
# Partner gate — shared with developer_chat; store_bp.before_request
# doesn't cover this URL prefix so we re-check the session flag here.
from app.web.developer_chat import _enforce_partner

log = logging.getLogger(__name__)

interview = Blueprint("interview", __name__)

# The 4 pipeline stages, in order. key matches Candidate.stage values;
# label is the display string the template renders.
STAGE_ORDER = [
    ("applied", "Applied"),
    ("first", "1st Interview"),
    ("second", "2nd Interview"),
    ("hired", "Hired"),
]
STAGE_LABELS = {key: label for key, label in STAGE_ORDER}


def _initials(name: str | None) -> str:
    """First letters of the first + last word of a name, uppercased.
    'Maria Gonzalez' -> 'MG'; single word -> first 2 letters; '' -> '?'."""
    parts = (name or "").split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _card_dict(c: Candidate) -> dict:
    """Build the candidate-card dict the pipeline columns render.
    Display fields are derived here — the model stores source only."""
    if c.stage == "hired":
        tag_kind = "hired"
        tag_label = "Hired"
        meta_label = ""
    else:
        # tag_kind 'none' -> the template shows meta_label as plain text.
        tag_kind = "none"
        tag_label = ""
        meta_label = "Applied" + (f" · {c.source}" if c.source else "")
    return {
        "id": c.id,
        "name": c.name or "",
        "initials": _initials(c.name),
        "role": c.role or "",
        "store": c.store or "",
        "meta_label": meta_label,
        "tag_label": tag_label,
        "tag_kind": tag_kind,
        "urgent": bool(c.urgent),
    }


def _timeline(c: Candidate) -> list[dict]:
    """Derive a 4-entry timeline from the candidate's stage. Entries up
    to and including the current stage are reached; later stages render
    dimmed (pending=True). author/body/time_label are empty for v1 —
    interview-log entries are a separate future task."""
    stage_keys = [key for key, _ in STAGE_ORDER]
    try:
        current_idx = stage_keys.index(c.stage)
    except ValueError:
        current_idx = 0
    entries = []
    for idx, (key, label) in enumerate(STAGE_ORDER):
        entries.append({
            "stage_key": key,
            "stage_label": label,
            "author": "",
            "time_label": "",
            "body": "",
            "pending": idx > current_idx,
        })
    return entries


def _detail_dict(c: Candidate) -> dict:
    """Build the full selected-candidate detail dict."""
    return {
        "id": c.id,
        "name": c.name or "",
        "initials": _initials(c.name),
        "role": c.role or "",
        "store": c.store or "",
        "phone": c.phone or "",
        "email": c.email or "",
        "stage_label": STAGE_LABELS.get(c.stage, "Applied"),
        "source": c.source or "",
        "applied_label": (
            c.applied_at.strftime("%b %d, %Y") if c.applied_at else ""
        ),
        "position": c.position or "",
        "desired_wage": c.desired_wage or "",
        "availability": c.availability or "",
        "experience": c.experience or "",
        "referred_by": c.referred_by or "",
        "documents": [],  # v1 — document upload is a future task
        "timeline": _timeline(c),
    }


@interview.route("/partner/interview-tracker", methods=["GET"])
def interview_tracker():
    """The Interview Tracker board. Builds the 4 ordered stage objects
    and, if ?candidate=<id> matches a row, the detail dict."""
    gate = _enforce_partner()
    if gate is not None:
        return gate

    db = SessionLocal()
    try:
        rows = db.query(Candidate).order_by(Candidate.created_at.asc()).all()

        # Bucket candidates by stage, then build the 4 ordered columns.
        by_stage: dict[str, list] = {key: [] for key, _ in STAGE_ORDER}
        for c in rows:
            by_stage.setdefault(c.stage, []).append(c)
        stages = [
            {
                "key": key,
                "label": label,
                "candidates": [_card_dict(c) for c in by_stage.get(key, [])],
            }
            for key, label in STAGE_ORDER
        ]

        # Optional selected candidate from ?candidate=<id>.
        selected_candidate = None
        sel_raw = request.args.get("candidate")
        if sel_raw:
            try:
                sel_id = int(sel_raw)
            except (TypeError, ValueError):
                sel_id = None
            if sel_id is not None:
                match = next((c for c in rows if c.id == sel_id), None)
                if match is not None:
                    selected_candidate = _detail_dict(match)
    finally:
        db.close()

    # Synthesize the per-store sidebar context. This page lives under
    # /partner/ but the URL doesn't pass through the store_slug prefix,
    # so we set g manually for base_dashboard + the sidebar template.
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    return render_template(
        "interview_tracker.html",
        active="interview_tracker",
        store_label=g.store_label,
        stages=stages,
        selected_candidate=selected_candidate,
    )


@interview.route("/partner/interview-tracker/new", methods=["GET"])
def candidate_new():
    """Add-candidate placeholder for v1. The real new-candidate form is
    a separate task; this endpoint only needs to exist so the template's
    url_for('interview.candidate_new') resolves. Redirect back to the
    tracker for now."""
    gate = _enforce_partner()
    if gate is not None:
        return gate
    return redirect(url_for("interview.interview_tracker"))
