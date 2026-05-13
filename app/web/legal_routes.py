"""Legal section — partner-only.

Public legal pages (privacy/terms) live at /privacy so Google Play and
other auditors can crawl them — those stay open. Everything else on the
partner side (/partner/legal/*) is gated by session.partner_auth_ok and
every read/write writes an append-only row to LegalAccessLog.

Five pages (Sam, 2026-05-13 Phase 0 ck Block 3):

  GET  /partner/legal                       — Overview dashboard
  GET  /partner/legal/matters               — Matter list (filter + sort)
  GET  /partner/legal/matters/new           — New-matter form
  POST /partner/legal/matters               — Create submit
  GET  /partner/legal/matters/<id>          — Single matter detail + edit form
  POST /partner/legal/matters/<id>          — Edit submit
  POST /partner/legal/matters/<id>/status   — Status change (open/in-review/resolved/archived)
  GET  /partner/legal/audit                 — LegalAccessLog browse

Design language follows the Ez Market / Ez Manage / My Profile system —
glass cards (rgba(40,22,14,0.55) bg, 0.5px gold border, 12px radius,
14-16px padding), gold uppercase section labels (letter-spacing 0.12em),
Tabler icons, brand-token color (--ck-gold / --ck-text-soft / etc.),
cubic-bezier(0.22, 0.8, 0.24, 1) motion curve.
"""
from __future__ import annotations

from datetime import datetime, date

from flask import (
    Blueprint, g, render_template, request, redirect, url_for, abort, session,
)
from sqlalchemy import desc

from app.db import SessionLocal
from app.models import LegalMatter, LegalAccessLog, User

legal = Blueprint("legal", __name__)


# ============================================================
# Public — privacy policy (left here so Play Console etc. can crawl it)
# ============================================================
@legal.route("/privacy")
@legal.route("/privacy/")
def privacy():
    return render_template("privacy.html")


# ============================================================
# Partner-only — Matters + Audit
# ============================================================
_CATEGORIES = [
    ("contract",    "Contract"),
    ("employment",  "Employment"),
    ("compliance",  "Compliance"),
    ("litigation",  "Litigation"),
    ("ip",          "IP / Trademark"),
    ("corporate",   "Corporate"),
    ("real-estate", "Real Estate"),
    ("other",       "Other"),
]
_STATUSES = [
    ("open",      "Open"),
    ("in-review", "In Review"),
    ("resolved",  "Resolved"),
    ("archived",  "Archived"),
]
_STATUS_KEYS = {s for s, _ in _STATUSES}
_CATEGORY_KEYS = {c for c, _ in _CATEGORIES}


def _enforce_partner():
    """Same gate as developer_chat — partner_auth_ok in session is the
    keypad-side Tier-2 flag set when a partner-permission User signs in."""
    if not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login", next=request.path))
    return None


def _client_ip() -> str:
    return (request.headers.get("X-Forwarded-For") or
            request.remote_addr or "?").split(",")[0].strip()[:64]


def _log_access(db, action: str, target_type: str | None = None,
                target_id: int | None = None, details: str | None = None) -> None:
    """Append one row to LegalAccessLog. Caller commits."""
    actor = getattr(g, "current_user", None)
    db.add(LegalAccessLog(
        user_id=actor.id if actor else None,
        actor_label=(actor.full_name if actor and actor.full_name
                     else "partner-session"),
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=(details[:980] if details else None),
        ip=_client_ip(),
    ))


def _set_partner_g():
    """Fill in g.current_store / g.store_label so the dashboard sidebar
    has a consistent 'partner' scope across Legal pages (matches what
    developer_chat does for its routes)."""
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"


def _parse_date(raw: str | None):
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# -----------------------------------------------------------
# 1. Overview
# -----------------------------------------------------------
@legal.route("/partner/legal", methods=["GET"])
def legal_overview():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    _set_partner_g()
    db = SessionLocal()
    try:
        all_matters = db.query(LegalMatter).order_by(desc(LegalMatter.updated_at)).all()
        # Counts by status
        counts = {k: 0 for k in _STATUS_KEYS}
        for m in all_matters:
            counts[m.status] = counts.get(m.status, 0) + 1
        # Upcoming next actions (date set + not closed)
        today = date.today()
        upcoming = sorted(
            [m for m in all_matters
             if m.next_action_on and m.status in ("open", "in-review")],
            key=lambda m: m.next_action_on,
        )[:6]
        overdue = [m for m in upcoming if m.next_action_on and m.next_action_on < today]
        recent = sorted(all_matters, key=lambda m: m.updated_at, reverse=True)[:5]
        recent_audit = (db.query(LegalAccessLog)
                          .order_by(desc(LegalAccessLog.created_at))
                          .limit(8).all())
        _log_access(db, "view_overview")
        db.commit()
        return render_template(
            "legal_overview.html",
            counts=counts,
            upcoming=upcoming,
            overdue=overdue,
            today_date=today,
            recent=recent,
            recent_audit=recent_audit,
            statuses=_STATUSES,
            categories=_CATEGORIES,
            active="legal_overview",
        )
    finally:
        db.close()


# -----------------------------------------------------------
# 2. Matters list
# -----------------------------------------------------------
@legal.route("/partner/legal/matters", methods=["GET"])
def legal_matters():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    _set_partner_g()
    status_filter = request.args.get("status") or "all"
    category_filter = request.args.get("category") or "all"
    db = SessionLocal()
    try:
        q = db.query(LegalMatter).order_by(desc(LegalMatter.updated_at))
        if status_filter in _STATUS_KEYS:
            q = q.filter(LegalMatter.status == status_filter)
        if category_filter in _CATEGORY_KEYS:
            q = q.filter(LegalMatter.category == category_filter)
        matters = q.all()
        _log_access(db, "view_matters",
                    details=f"status={status_filter}, category={category_filter}")
        db.commit()
        return render_template(
            "legal_matters.html",
            matters=matters,
            statuses=_STATUSES,
            categories=_CATEGORIES,
            status_filter=status_filter,
            category_filter=category_filter,
            active="legal_matters",
        )
    finally:
        db.close()


# -----------------------------------------------------------
# 3. New matter
# -----------------------------------------------------------
@legal.route("/partner/legal/matters/new", methods=["GET"])
def legal_matter_new():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    _set_partner_g()
    # Even GETs on /partner/legal/* go in the audit. The new-matter
    # form is a 'view_new_matter_form' event so we have visibility into
    # who opened the form even if they cancelled before submitting.
    db = SessionLocal()
    try:
        _log_access(db, "view_new_matter_form")
        db.commit()
    finally:
        db.close()
    return render_template(
        "legal_matter_form.html",
        matter=None,
        statuses=_STATUSES,
        categories=_CATEGORIES,
        error=request.args.get("error"),
        active="legal_matter_new",
    )


@legal.route("/partner/legal/matters", methods=["POST"])
def legal_matter_create():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    title = (request.form.get("title") or "").strip()[:200]
    if not title:
        return redirect(url_for("legal.legal_matter_new", error="Title is required."))
    category = (request.form.get("category") or "other").strip()
    if category not in _CATEGORY_KEYS:
        category = "other"
    db = SessionLocal()
    try:
        actor = getattr(g, "current_user", None)
        m = LegalMatter(
            title=title,
            category=category,
            status=(request.form.get("status") or "open"),
            summary=(request.form.get("summary") or "").strip() or None,
            counterparty=(request.form.get("counterparty") or "").strip() or None,
            counsel_name=(request.form.get("counsel_name") or "").strip() or None,
            counsel_firm=(request.form.get("counsel_firm") or "").strip() or None,
            counsel_email=(request.form.get("counsel_email") or "").strip() or None,
            counsel_phone=(request.form.get("counsel_phone") or "").strip() or None,
            matter_ref=(request.form.get("matter_ref") or "").strip() or None,
            opened_on=_parse_date(request.form.get("opened_on")) or date.today(),
            next_action_on=_parse_date(request.form.get("next_action_on")),
            next_action_text=(request.form.get("next_action_text") or "").strip() or None,
            notes=(request.form.get("notes") or "").strip() or None,
            created_by_user_id=actor.id if actor else None,
        )
        if m.status not in _STATUS_KEYS:
            m.status = "open"
        db.add(m)
        db.flush()
        _log_access(db, "create_matter", target_type="legal_matter",
                    target_id=m.id, details=f"title={title!r}")
        db.commit()
        return redirect(url_for("legal.legal_matter_detail", matter_id=m.id))
    finally:
        db.close()


# -----------------------------------------------------------
# 4. Single matter detail + edit
# -----------------------------------------------------------
@legal.route("/partner/legal/matters/<int:matter_id>", methods=["GET"])
def legal_matter_detail(matter_id: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    _set_partner_g()
    db = SessionLocal()
    try:
        m = db.get(LegalMatter, matter_id)
        if m is None:
            abort(404)
        creator = (db.get(User, m.created_by_user_id)
                   if m.created_by_user_id else None)
        history = (db.query(LegalAccessLog)
                     .filter(LegalAccessLog.target_type == "legal_matter")
                     .filter(LegalAccessLog.target_id == m.id)
                     .order_by(desc(LegalAccessLog.created_at))
                     .limit(40).all())
        _log_access(db, "view_matter", target_type="legal_matter",
                    target_id=m.id)
        db.commit()
        return render_template(
            "legal_matter_detail.html",
            matter=m,
            creator=creator,
            history=history,
            statuses=_STATUSES,
            categories=_CATEGORY_KEYS and _CATEGORIES,
            success=request.args.get("success"),
            active="legal_matters",
        )
    finally:
        db.close()


@legal.route("/partner/legal/matters/<int:matter_id>", methods=["POST"])
def legal_matter_update(matter_id: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        m = db.get(LegalMatter, matter_id)
        if m is None:
            abort(404)
        # Capture before-state for the audit details
        before = (f"status={m.status} | category={m.category} | "
                  f"title={m.title!r}")

        m.title = (request.form.get("title") or m.title).strip()[:200] or m.title
        new_cat = (request.form.get("category") or m.category).strip()
        m.category = new_cat if new_cat in _CATEGORY_KEYS else m.category
        new_status = (request.form.get("status") or m.status).strip()
        m.status = new_status if new_status in _STATUS_KEYS else m.status
        m.summary = (request.form.get("summary") or "").strip() or None
        m.counterparty = (request.form.get("counterparty") or "").strip() or None
        m.counsel_name = (request.form.get("counsel_name") or "").strip() or None
        m.counsel_firm = (request.form.get("counsel_firm") or "").strip() or None
        m.counsel_email = (request.form.get("counsel_email") or "").strip() or None
        m.counsel_phone = (request.form.get("counsel_phone") or "").strip() or None
        m.matter_ref = (request.form.get("matter_ref") or "").strip() or None
        m.opened_on = _parse_date(request.form.get("opened_on")) or m.opened_on
        m.next_action_on = _parse_date(request.form.get("next_action_on"))
        m.next_action_text = (request.form.get("next_action_text") or "").strip() or None
        m.notes = (request.form.get("notes") or "").strip() or None
        if m.status in ("resolved", "archived") and m.closed_on is None:
            m.closed_on = date.today()
        elif m.status in ("open", "in-review"):
            m.closed_on = None

        _log_access(db, "edit_matter", target_type="legal_matter",
                    target_id=m.id, details=before)
        db.commit()
        return redirect(url_for(
            "legal.legal_matter_detail", matter_id=m.id,
            success="Matter updated."))
    finally:
        db.close()


@legal.route("/partner/legal/matters/<int:matter_id>/status", methods=["POST"])
def legal_matter_status(matter_id: int):
    """Quick status change from the matter detail page or list. Single
    column rather than re-rendering the full form."""
    gate = _enforce_partner()
    if gate is not None:
        return gate
    new_status = (request.form.get("status") or "").strip()
    if new_status not in _STATUS_KEYS:
        return redirect(url_for("legal.legal_matter_detail",
                                matter_id=matter_id,
                                success="(invalid status)"))
    db = SessionLocal()
    try:
        m = db.get(LegalMatter, matter_id)
        if m is None:
            abort(404)
        before_status = m.status
        m.status = new_status
        if new_status in ("resolved", "archived") and m.closed_on is None:
            m.closed_on = date.today()
        elif new_status in ("open", "in-review"):
            m.closed_on = None
        _log_access(db, "status_change", target_type="legal_matter",
                    target_id=m.id,
                    details=f"{before_status} → {new_status}")
        db.commit()
        return redirect(url_for(
            "legal.legal_matter_detail", matter_id=m.id,
            success=f"Status changed to {new_status}."))
    finally:
        db.close()


# -----------------------------------------------------------
# 5. Audit log
# -----------------------------------------------------------
@legal.route("/partner/legal/audit", methods=["GET"])
def legal_audit():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    _set_partner_g()
    try:
        limit = max(20, min(int(request.args.get("limit", "100")), 500))
    except (ValueError, TypeError):
        limit = 100
    db = SessionLocal()
    try:
        rows = (db.query(LegalAccessLog)
                  .order_by(desc(LegalAccessLog.created_at))
                  .limit(limit).all())
        # NB: viewing the audit log is itself an audit event.
        _log_access(db, "view_audit", details=f"limit={limit}")
        db.commit()
        return render_template(
            "legal_audit.html",
            rows=rows,
            limit=limit,
            active="legal_audit",
        )
    finally:
        db.close()
