from __future__ import annotations

import os
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.datastructures import MultiDict
from werkzeug.utils import secure_filename

from app.db import SessionLocal
from app.models import WebsiteFormSubmission
from app.web.developer_chat import _enforce_partner


website_forms_bp = Blueprint("website_forms", __name__)

FORM_LABELS = OrderedDict([
    ("career", "Careers"),
    ("catering", "Catering"),
    ("spirit", "Spirit Days"),
    ("donation", "Donations"),
    ("contact", "Contact"),
])

FORM_ALIASES = {
    "careers": "career",
    "career": "career",
    "application": "career",
    "apply": "career",
    "catering": "catering",
    "cater": "catering",
    "spirit": "spirit",
    "spirit-day": "spirit",
    "spirit_day": "spirit",
    "donation": "donation",
    "donations": "donation",
    "contact": "contact",
    "feedback": "contact",
}


def _canonical_form_type(raw: str | None) -> str | None:
    key = (raw or "").strip().lower()
    return FORM_ALIASES.get(key)


def _as_plain_fields(form: MultiDict[str, str]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    ignored = {"_gotcha", "_next"}
    for key in form.keys():
        if key in ignored or key.startswith("_"):
            continue
        values = [v.strip() for v in form.getlist(key) if (v or "").strip()]
        if not values:
            continue
        fields[key] = values if len(values) > 1 else values[0]
    return fields


def _field(fields: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = fields.get(name)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if str(v).strip())
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _normalize_location(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    key = value.lower()
    if "tomball" in key or key in {"dos", "store_2", "store 2"}:
        return "Tomball"
    if "copper" in key or key in {"uno", "store_1", "store 1"}:
        return "Copperfield"
    if key in {"both", "either", "any"}:
        return "Either location"
    return value[:80]


def _summary(form_type: str, fields: dict[str, Any]) -> dict[str, str | None]:
    first = _field(fields, "first_name")
    last = _field(fields, "last_name")
    full_name = " ".join(part for part in (first, last) if part)
    name = full_name or _field(fields, "name", "contact_name")
    organization = _field(fields, "organization", "org_name")
    contact_name = _field(fields, "contact_name") or name

    subject = _field(fields, "subject", "support_type", "event_name", "package")
    if form_type == "career":
        subject = _field(fields, "desired_position", "position") or "Career application"
    elif form_type == "spirit":
        subject = "Spirit Day request"
    elif form_type == "donation":
        subject = _field(fields, "support_type") or "Donation request"
    elif form_type == "catering":
        subject = "Catering request"

    return {
        "location": _normalize_location(_field(fields, "location", "preferred_location")),
        "position": _field(fields, "desired_position", "position"),
        "subject": subject,
        "applicant_name": name,
        "organization": organization,
        "contact_name": contact_name,
        "email": _field(fields, "email"),
        "phone": _field(fields, "phone", "mobile"),
    }


def _upload_root() -> Path:
    configured = (os.getenv("FORM_UPLOAD_DIR") or "").strip()
    if configured:
        root = Path(configured)
    elif os.getenv("RENDER"):
        root = Path("/var/data/website_form_uploads")
    else:
        root = Path(current_app.instance_path) / "website_form_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _save_attachments(submission_id: int) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    upload_dir = _upload_root() / str(submission_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    for field_name, storage in request.files.items(multi=True):
        if not storage or not storage.filename:
            continue
        original = storage.filename
        safe_name = secure_filename(original) or "upload"
        stored_name = f"{uuid.uuid4().hex}_{safe_name}"
        path = upload_dir / stored_name
        storage.save(path)
        saved.append({
            "field": field_name,
            "filename": original,
            "stored_name": stored_name,
            "path": str(path),
            "content_type": storage.mimetype,
            "size": path.stat().st_size if path.exists() else None,
        })
    return saved


def _safe_next(default_path: str) -> str:
    raw = (request.form.get("_next") or request.args.get("next") or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    allowed = (
        "https://cenaskitchen.com/",
        "https://www.cenaskitchen.com/",
        "http://127.0.0.1:5050/",
        "http://localhost:5050/",
    )
    if raw.startswith(allowed):
        return raw
    return default_path


@website_forms_bp.route("/public/forms/<form_type>", methods=["POST"])
def public_submit(form_type: str):
    canonical = _canonical_form_type(form_type)
    if canonical is None:
        abort(404)
    if (request.form.get("_gotcha") or "").strip():
        return redirect(_safe_next(url_for("website_forms.thanks", type=canonical)))

    fields = _as_plain_fields(request.form)
    summary = _summary(canonical, fields)
    source_page = (request.form.get("_source_page") or request.referrer or "").strip()[:255] or None

    db = SessionLocal()
    try:
        row = WebsiteFormSubmission(
            form_type=canonical,
            source_page=source_page,
            fields=fields,
            user_agent=(request.headers.get("User-Agent") or "")[:255] or None,
            referrer=(request.referrer or "")[:500] or None,
            **summary,
        )
        db.add(row)
        db.flush()
        row.attachments = _save_attachments(row.id)
        db.commit()
        target = _safe_next(url_for("website_forms.thanks", type=canonical, id=row.id))
        return redirect(target, code=303)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@website_forms_bp.route("/public/forms/thanks", methods=["GET"])
def thanks():
    form_type = _canonical_form_type(request.args.get("type")) or "contact"
    label = FORM_LABELS.get(form_type, "message")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thank you - Cenas Kitchen</title>
<style>
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#3C1014;color:#F3E7D1;display:grid;min-height:100vh;place-items:center;padding:24px}}
.card{{max-width:560px;background:#5C1A21;border:1px solid rgba(233,162,59,.35);border-radius:16px;padding:34px;box-shadow:0 24px 70px rgba(0,0,0,.35)}}
h1{{font-family:Georgia,serif;font-size:42px;line-height:1;margin:0 0 12px;color:#E9A23B}}
p{{font-size:18px;line-height:1.55;margin:0 0 24px}}a{{display:inline-flex;background:#E9A23B;color:#3a1409;border-radius:999px;padding:12px 18px;text-decoration:none;font-weight:800}}
</style></head><body><main class="card"><h1>Gracias.</h1><p>Your {label.lower()} submission was received. Our team will review it and follow up soon.</p><a href="https://www.cenaskitchen.com/">Back to Cenas Kitchen</a></main></body></html>"""
    return Response(html, mimetype="text/html")


def _set_partner_context() -> None:
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"


def _group_key(row: WebsiteFormSubmission, form_type: str) -> str:
    location = row.location or "No location selected"
    if form_type == "career":
        return f"{location} - {row.position or 'General application'}"
    if form_type in {"catering", "spirit", "donation"}:
        return location
    return row.subject or "General messages"


@website_forms_bp.route("/partner/website-forms", methods=["GET"])
def partner_forms():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    _set_partner_context()

    form_type = _canonical_form_type(request.args.get("type")) or "career"
    location_filter = _normalize_location(request.args.get("location"))
    status_filter = (request.args.get("status") or "").strip().lower()

    db = SessionLocal()
    try:
        q = db.query(WebsiteFormSubmission).filter(
            WebsiteFormSubmission.form_type == form_type
        )
        if location_filter:
            q = q.filter(WebsiteFormSubmission.location == location_filter)
        if status_filter:
            q = q.filter(WebsiteFormSubmission.status == status_filter)
        rows = q.order_by(WebsiteFormSubmission.created_at.desc()).limit(250).all()

        counts = {
            key: db.query(WebsiteFormSubmission)
            .filter(WebsiteFormSubmission.form_type == key)
            .count()
            for key in FORM_LABELS
        }
    finally:
        db.close()

    groups: OrderedDict[str, list[WebsiteFormSubmission]] = OrderedDict()
    for row in rows:
        groups.setdefault(_group_key(row, form_type), []).append(row)

    return render_template(
        "website_forms.html",
        active="website_forms",
        form_labels=FORM_LABELS,
        active_type=form_type,
        active_label=FORM_LABELS[form_type],
        counts=counts,
        groups=groups,
        rows=rows,
        selected_location=location_filter or "",
        selected_status=status_filter,
        store_label=g.store_label,
    )


@website_forms_bp.route(
    "/partner/website-forms/<int:submission_id>/attachment/<int:attachment_index>",
    methods=["GET"],
)
def partner_form_attachment(submission_id: int, attachment_index: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate

    db = SessionLocal()
    try:
        row = db.get(WebsiteFormSubmission, submission_id)
        if row is None:
            abort(404)
        attachments = row.attachments or []
        if attachment_index < 0 or attachment_index >= len(attachments):
            abort(404)
        meta = attachments[attachment_index]
    finally:
        db.close()

    path = Path(meta.get("path") or "")
    if not path.is_file():
        abort(404)
    return send_file(
        path,
        as_attachment=True,
        download_name=meta.get("filename") or path.name,
        mimetype=meta.get("content_type") or None,
    )


@website_forms_bp.route("/partner/website-forms/<int:submission_id>/status", methods=["POST"])
def partner_form_status(submission_id: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    status = (request.form.get("status") or "").strip().lower()
    if status not in {"new", "reviewed", "archived"}:
        abort(400)
    db = SessionLocal()
    try:
        row = db.get(WebsiteFormSubmission, submission_id)
        if row is None:
            abort(404)
        row.status = status
        row.reviewed_at = datetime.utcnow() if status == "reviewed" else row.reviewed_at
        user = getattr(g, "current_user", None)
        row.reviewed_by_user_id = getattr(user, "id", None) if status == "reviewed" else row.reviewed_by_user_id
        db.commit()
        form_type = row.form_type
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return redirect(url_for("website_forms.partner_forms", type=form_type), code=303)
