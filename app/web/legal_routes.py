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

import os
import re
from datetime import datetime, date, timedelta
from pathlib import Path

from flask import (
    Blueprint, g, render_template, request, redirect, url_for, abort, session,
    send_file, current_app, jsonify,
)
from sqlalchemy import desc
from werkzeug.utils import secure_filename

from app.db import SessionLocal
from app.models import (
    LegalMatter, LegalMatterNote, LegalDocument,
    LegalCompanyStructure, LegalInsurancePolicy,
    LegalAccessLog, User,
)

legal = Blueprint("legal", __name__)

# Upload constraints — same shape as chat attachments + a few more
# extensions legal frequently traffics in. Storage path comes from
# LEGAL_ATTACHMENTS_DIR env var on Render (/var/data/legal-attachments)
# or a local fallback for dev.
LEGAL_UPLOAD_EXTENSIONS = {
    "pdf", "doc", "docx", "rtf", "odt",
    "xls", "xlsx", "csv",
    "png", "jpg", "jpeg", "gif", "webp", "heic",
    "txt", "md", "log",
    "zip",
}
LEGAL_UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # 25 MB per file


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


def _legal_attachments_dir() -> Path:
    """Where uploaded LegalDocument files live. /var/data is Render's
    persistent disk (set in render.yaml). Local dev falls back to a
    relative path so tests/dev still work without /var existing."""
    base = os.environ.get("LEGAL_ATTACHMENTS_DIR", "/var/data/legal-attachments")
    p = Path(base)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        # Fall back to a dev path under the repo's instance/ folder.
        p = Path(current_app.root_path).parent / "instance" / "legal-attachments"
        p.mkdir(parents=True, exist_ok=True)
    return p


def _allowed_legal_upload(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[-1].lower()
    return ext in LEGAL_UPLOAD_EXTENSIONS


def _parse_key_dates(raw: str | None) -> dict | None:
    """Parse the key_dates form text into a clean dict. Input shape:
    one entry per line, "label: YYYY-MM-DD" or "label=YYYY-MM-DD".
    Empty lines + lines without a recognizable date are skipped. We
    keep this loose because the partner is the only one editing
    here — better to store a partial dict than reject a typo."""
    if not raw:
        return None
    out: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = ":" if ":" in line else ("=" if "=" in line else None)
        if not sep:
            continue
        k, v = line.split(sep, 1)
        k = re.sub(r"[^a-z0-9_]+", "_", k.strip().lower()).strip("_")
        v = v.strip()
        d = _parse_date(v)
        if k and d:
            out[k] = d.isoformat()
    return out or None


def _format_key_dates(d: dict | None) -> str:
    """Inverse of _parse_key_dates — used to seed the form textarea."""
    if not d:
        return ""
    return "\n".join(f"{k.replace('_', ' ')}: {v}" for k, v in d.items())


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
        # Insurance-renewal banner: active policies with renewal_on
        # inside the next 30 days. Sorted nearest-first.
        soon_cutoff = today + timedelta(days=30)
        renewals = (db.query(LegalInsurancePolicy)
                      .filter(LegalInsurancePolicy.status == "active")
                      .filter(LegalInsurancePolicy.renewal_on.isnot(None))
                      .filter(LegalInsurancePolicy.renewal_on <= soon_cutoff)
                      .order_by(LegalInsurancePolicy.renewal_on)
                      .all())
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
            renewals=renewals,
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
        first_note = (request.form.get("notes") or "").strip()
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
            notes=first_note or None,
            key_dates=_parse_key_dates(request.form.get("key_dates")),
            created_by_user_id=actor.id if actor else None,
        )
        if m.status not in _STATUS_KEYS:
            m.status = "open"
        db.add(m)
        db.flush()
        # If the create form had a notes blob, also seed the first
        # LegalMatterNote so the timeline reads correctly from day 1.
        if first_note:
            db.add(LegalMatterNote(
                matter_id=m.id,
                body=first_note,
                created_by_user_id=actor.id if actor else None,
                actor_label=(actor.full_name if actor and actor.full_name
                             else "partner-session"),
            ))
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
        notes = (db.query(LegalMatterNote)
                   .filter(LegalMatterNote.matter_id == m.id)
                   .order_by(desc(LegalMatterNote.created_at))
                   .all())
        documents = (db.query(LegalDocument)
                       .filter(LegalDocument.matter_id == m.id)
                       .order_by(desc(LegalDocument.created_at))
                       .all())
        _log_access(db, "view_matter", target_type="legal_matter",
                    target_id=m.id)
        db.commit()
        return render_template(
            "legal_matter_detail.html",
            matter=m,
            creator=creator,
            history=history,
            notes=notes,
            documents=documents,
            key_dates_text=_format_key_dates(m.key_dates),
            statuses=_STATUSES,
            categories=_CATEGORIES,
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
        # Legacy `notes` field stays for backwards compat; new note
        # content is appended via the timeline endpoint, not this form.
        m.notes = (request.form.get("notes") or "").strip() or m.notes
        m.key_dates = _parse_key_dates(request.form.get("key_dates"))
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


# ============================================================
# Notes timeline (append-only, per-Matter)
# ============================================================
@legal.route("/partner/legal/matters/<int:matter_id>/notes", methods=["POST"])
def legal_matter_add_note(matter_id: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    body = (request.form.get("body") or "").strip()
    if not body:
        return redirect(url_for("legal.legal_matter_detail",
                                matter_id=matter_id,
                                success="(empty note ignored)"))
    db = SessionLocal()
    try:
        m = db.get(LegalMatter, matter_id)
        if m is None:
            abort(404)
        actor = getattr(g, "current_user", None)
        db.add(LegalMatterNote(
            matter_id=m.id,
            body=body,
            created_by_user_id=actor.id if actor else None,
            actor_label=(actor.full_name if actor and actor.full_name
                         else "partner-session"),
        ))
        _log_access(db, "add_note", target_type="legal_matter",
                    target_id=m.id, details=f"({len(body)} chars)")
        db.commit()
        return redirect(url_for("legal.legal_matter_detail",
                                matter_id=m.id, success="Note added."))
    finally:
        db.close()


# ============================================================
# Matter-scoped document upload (file attached to a specific Matter)
# ============================================================
@legal.route("/partner/legal/matters/<int:matter_id>/upload", methods=["POST"])
def legal_matter_upload(matter_id: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    f = request.files.get("file")
    if not f or not f.filename:
        return redirect(url_for("legal.legal_matter_detail",
                                matter_id=matter_id,
                                success="(no file selected)"))
    if not _allowed_legal_upload(f.filename):
        return redirect(url_for("legal.legal_matter_detail",
                                matter_id=matter_id,
                                success="(file type not allowed)"))
    db = SessionLocal()
    try:
        m = db.get(LegalMatter, matter_id)
        if m is None:
            abort(404)
        return _store_and_log_document(db, f, matter_id=m.id,
                                       redirect_target=url_for(
                                           "legal.legal_matter_detail",
                                           matter_id=m.id,
                                           success="Document uploaded."))
    finally:
        db.close()


def _store_and_log_document(db, f, matter_id, redirect_target,
                             extra_notes=None):
    """Common write path: secure_filename -> reserve a row -> write bytes
    to /<doc_id>/<safe_filename> on disk -> log access. Returns a redirect.
    Size cap enforced before any disk I/O."""
    actor = getattr(g, "current_user", None)
    safe = secure_filename(f.filename) or "upload"
    if not safe:
        abort(400)
    # Sniff size without trusting headers
    f.stream.seek(0, 2)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > LEGAL_UPLOAD_MAX_BYTES:
        return redirect(redirect_target)
    doc = LegalDocument(
        matter_id=matter_id,
        filename=safe,
        mime_type=(f.mimetype or "application/octet-stream")[:100],
        size_bytes=int(size),
        storage_path="",  # filled below once we know the doc id
        uploaded_by_user_id=actor.id if actor else None,
        actor_label=(actor.full_name if actor and actor.full_name
                     else "partner-session"),
        notes=extra_notes,
    )
    db.add(doc)
    db.flush()  # assign doc.id
    target_dir = _legal_attachments_dir() / str(doc.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe
    f.save(str(target_path))
    doc.storage_path = str(target_path)
    _log_access(db, "upload_document", target_type="legal_document",
                target_id=doc.id,
                details=f"matter={matter_id} filename={safe} bytes={size}")
    db.commit()
    return redirect(redirect_target)


# ============================================================
# Documents library (matter_id IS NULL OR ALL)
# ============================================================
@legal.route("/partner/legal/documents", methods=["GET"])
def legal_documents():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    _set_partner_g()
    scope = (request.args.get("scope") or "all").strip()
    db = SessionLocal()
    try:
        q = db.query(LegalDocument).order_by(desc(LegalDocument.created_at))
        if scope == "library":
            q = q.filter(LegalDocument.matter_id.is_(None))
        elif scope == "matters":
            q = q.filter(LegalDocument.matter_id.isnot(None))
        docs = q.all()
        # Resolve matter titles in one go for the table cell.
        matter_ids = {d.matter_id for d in docs if d.matter_id}
        matter_titles = {}
        if matter_ids:
            for m in (db.query(LegalMatter)
                        .filter(LegalMatter.id.in_(matter_ids)).all()):
                matter_titles[m.id] = m.title
        _log_access(db, "view_documents", details=f"scope={scope}")
        db.commit()
        return render_template(
            "legal_documents.html",
            documents=docs,
            matter_titles=matter_titles,
            scope=scope,
            allowed_extensions=sorted(LEGAL_UPLOAD_EXTENSIONS),
            max_mb=LEGAL_UPLOAD_MAX_BYTES // (1024 * 1024),
            active="legal_documents",
        )
    finally:
        db.close()


@legal.route("/partner/legal/documents/upload", methods=["POST"])
def legal_documents_upload():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    f = request.files.get("file")
    if not f or not f.filename:
        return redirect(url_for("legal.legal_documents",
                                scope=request.form.get("scope") or "all"))
    if not _allowed_legal_upload(f.filename):
        return redirect(url_for("legal.legal_documents",
                                scope=request.form.get("scope") or "all"))
    db = SessionLocal()
    try:
        notes = (request.form.get("notes") or "").strip() or None
        return _store_and_log_document(
            db, f, matter_id=None,
            redirect_target=url_for("legal.legal_documents", scope="library"),
            extra_notes=notes,
        )
    finally:
        db.close()


@legal.route("/partner/legal/documents/<int:doc_id>", methods=["GET"])
def legal_document_download(doc_id: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        doc = db.get(LegalDocument, doc_id)
        if doc is None or not doc.storage_path:
            abort(404)
        p = Path(doc.storage_path)
        if not p.exists():
            abort(404)
        _log_access(db, "download_document", target_type="legal_document",
                    target_id=doc.id,
                    details=f"filename={doc.filename}")
        db.commit()
        return send_file(
            str(p),
            mimetype=doc.mime_type or "application/octet-stream",
            as_attachment=True,
            download_name=doc.filename,
            max_age=0,
        )
    finally:
        db.close()


# ============================================================
# Company structure (single-row record)
# ============================================================
@legal.route("/partner/legal/structure", methods=["GET"])
def legal_structure():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    _set_partner_g()
    db = SessionLocal()
    try:
        s = (db.query(LegalCompanyStructure)
               .order_by(LegalCompanyStructure.id).first())
        _log_access(db, "view_structure",
                    target_type="legal_company_structure",
                    target_id=s.id if s else None)
        db.commit()
        return render_template(
            "legal_structure.html",
            structure=s,
            success=request.args.get("success"),
            active="legal_structure",
        )
    finally:
        db.close()


@legal.route("/partner/legal/structure", methods=["POST"])
def legal_structure_save():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        s = (db.query(LegalCompanyStructure)
               .order_by(LegalCompanyStructure.id).first())
        if s is None:
            s = LegalCompanyStructure()
            db.add(s)
        actor = getattr(g, "current_user", None)
        before = f"entity={s.entity_type} name={s.legal_name}"

        s.entity_type = (request.form.get("entity_type") or "").strip() or None
        s.legal_name = (request.form.get("legal_name") or "").strip() or None
        s.dba = (request.form.get("dba") or "").strip() or None
        s.state_of_formation = (request.form.get("state_of_formation") or "").strip() or None
        s.ein = (request.form.get("ein") or "").strip() or None
        s.formed_on = _parse_date(request.form.get("formed_on"))
        s.registered_agent = (request.form.get("registered_agent") or "").strip() or None
        s.registered_office_address = (request.form.get("registered_office_address") or "").strip() or None
        s.principal_office_address = (request.form.get("principal_office_address") or "").strip() or None
        s.notes = (request.form.get("notes") or "").strip() or None
        s.updated_by_user_id = actor.id if actor else None

        # Ownership: one row per "name | role | percent | notes" line.
        ownership_raw = (request.form.get("ownership") or "").strip()
        owners = []
        for line in ownership_raw.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 1 and parts[0]:
                entry = {"name": parts[0]}
                if len(parts) > 1: entry["role"] = parts[1]
                if len(parts) > 2:
                    try: entry["ownership_pct"] = float(parts[2].rstrip("%"))
                    except (ValueError, TypeError): pass
                if len(parts) > 3: entry["notes"] = parts[3]
                owners.append(entry)
        s.ownership = owners or None

        db.flush()
        _log_access(db, "edit_structure",
                    target_type="legal_company_structure",
                    target_id=s.id, details=before)
        db.commit()
        return redirect(url_for("legal.legal_structure",
                                success="Structure saved."))
    finally:
        db.close()


# ============================================================
# Insurance policies
# ============================================================
_INSURANCE_TYPES = [
    ("general-liability", "General Liability"),
    ("property",          "Property"),
    ("workers-comp",      "Workers Comp"),
    ("auto",              "Commercial Auto"),
    ("cyber",             "Cyber"),
    ("umbrella",          "Umbrella"),
    ("BOP",               "Business Owner Policy"),
    ("epli",              "EPLI"),
    ("other",             "Other"),
]
_INSURANCE_TYPE_KEYS = {k for k, _ in _INSURANCE_TYPES}


@legal.route("/partner/legal/insurance", methods=["GET"])
def legal_insurance():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    _set_partner_g()
    db = SessionLocal()
    try:
        policies = (db.query(LegalInsurancePolicy)
                      .order_by(LegalInsurancePolicy.status,
                                LegalInsurancePolicy.renewal_on)
                      .all())
        today = date.today()
        soon = today + timedelta(days=30)
        _log_access(db, "view_insurance")
        db.commit()
        return render_template(
            "legal_insurance.html",
            policies=policies,
            today_date=today,
            soon_cutoff=soon,
            types=_INSURANCE_TYPES,
            active="legal_insurance",
        )
    finally:
        db.close()


@legal.route("/partner/legal/insurance/new", methods=["GET"])
def legal_insurance_new():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    _set_partner_g()
    db = SessionLocal()
    try:
        _log_access(db, "view_insurance_new_form")
        db.commit()
    finally:
        db.close()
    return render_template(
        "legal_insurance_form.html",
        policy=None,
        types=_INSURANCE_TYPES,
        error=request.args.get("error"),
        active="legal_insurance",
    )


@legal.route("/partner/legal/insurance", methods=["POST"])
def legal_insurance_create():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        actor = getattr(g, "current_user", None)
        ptype = (request.form.get("policy_type") or "other").strip()
        if ptype not in _INSURANCE_TYPE_KEYS:
            ptype = "other"
        p = LegalInsurancePolicy(
            carrier=(request.form.get("carrier") or "").strip() or None,
            policy_number=(request.form.get("policy_number") or "").strip() or None,
            policy_type=ptype,
            coverage_limit=(request.form.get("coverage_limit") or "").strip() or None,
            deductible=(request.form.get("deductible") or "").strip() or None,
            premium=(request.form.get("premium") or "").strip() or None,
            effective_on=_parse_date(request.form.get("effective_on")),
            renewal_on=_parse_date(request.form.get("renewal_on")),
            broker_name=(request.form.get("broker_name") or "").strip() or None,
            broker_email=(request.form.get("broker_email") or "").strip() or None,
            broker_phone=(request.form.get("broker_phone") or "").strip() or None,
            notes=(request.form.get("notes") or "").strip() or None,
            status=(request.form.get("status") or "active").strip(),
            created_by_user_id=actor.id if actor else None,
        )
        if p.status not in ("active", "lapsed", "cancelled"):
            p.status = "active"
        db.add(p)
        db.flush()
        _log_access(db, "create_insurance", target_type="legal_insurance_policy",
                    target_id=p.id,
                    details=f"carrier={p.carrier!r} policy={p.policy_number!r}")
        db.commit()
        return redirect(url_for("legal.legal_insurance"))
    finally:
        db.close()


@legal.route("/partner/legal/insurance/<int:policy_id>", methods=["GET"])
def legal_insurance_edit(policy_id: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    _set_partner_g()
    db = SessionLocal()
    try:
        p = db.get(LegalInsurancePolicy, policy_id)
        if p is None:
            abort(404)
        _log_access(db, "view_insurance_policy",
                    target_type="legal_insurance_policy", target_id=p.id)
        db.commit()
        return render_template(
            "legal_insurance_form.html",
            policy=p,
            types=_INSURANCE_TYPES,
            error=None,
            active="legal_insurance",
        )
    finally:
        db.close()


@legal.route("/partner/legal/insurance/<int:policy_id>", methods=["POST"])
def legal_insurance_update(policy_id: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        p = db.get(LegalInsurancePolicy, policy_id)
        if p is None:
            abort(404)
        before = f"carrier={p.carrier} policy={p.policy_number} status={p.status}"
        ptype = (request.form.get("policy_type") or p.policy_type or "other").strip()
        if ptype in _INSURANCE_TYPE_KEYS:
            p.policy_type = ptype
        p.carrier = (request.form.get("carrier") or "").strip() or None
        p.policy_number = (request.form.get("policy_number") or "").strip() or None
        p.coverage_limit = (request.form.get("coverage_limit") or "").strip() or None
        p.deductible = (request.form.get("deductible") or "").strip() or None
        p.premium = (request.form.get("premium") or "").strip() or None
        p.effective_on = _parse_date(request.form.get("effective_on"))
        p.renewal_on = _parse_date(request.form.get("renewal_on"))
        p.broker_name = (request.form.get("broker_name") or "").strip() or None
        p.broker_email = (request.form.get("broker_email") or "").strip() or None
        p.broker_phone = (request.form.get("broker_phone") or "").strip() or None
        p.notes = (request.form.get("notes") or "").strip() or None
        new_status = (request.form.get("status") or p.status or "active").strip()
        if new_status in ("active", "lapsed", "cancelled"):
            p.status = new_status
        _log_access(db, "edit_insurance", target_type="legal_insurance_policy",
                    target_id=p.id, details=before)
        db.commit()
        return redirect(url_for("legal.legal_insurance"))
    finally:
        db.close()
