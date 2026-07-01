from __future__ import annotations

import os
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
    session,
    url_for,
)
from werkzeug.datastructures import MultiDict
from werkzeug.utils import secure_filename

from app.db import SessionLocal
from app.models import User, WebsiteFormSubmission


website_forms_bp = Blueprint("website_forms", __name__)

FORM_LABELS = OrderedDict([
    ("career", "Careers"),
    ("catering", "Catering"),
    ("spirit", "Spirit Days"),
    ("donation", "Donations"),
    ("contact", "Contact"),
    ("email-list", "Email List"),
])

FORM_SHORT_LABELS = {
    "career": "Career",
    "catering": "Catering",
    "spirit": "Spirit",
    "donation": "Donate",
    "contact": "Contact",
    "email-list": "Email",
}

STATUS_FILTERS = OrderedDict([
    ("", "All"),
    ("new", "New"),
    ("archived", "Archived"),
    ("deleted", "Deleted"),
])

STATUS_ACTIONS = {"new", "reviewed", "archived", "deleted"}

LOCATION_FILTERS = OrderedDict([
    ("", "Both"),
    ("Copperfield", "Copperfield"),
    ("Tomball", "Tomball"),
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
    "email-list": "email-list",
    "email_list": "email-list",
    "email list": "email-list",
    "emails": "email-list",
    "newsletter": "email-list",
    "mailing-list": "email-list",
    "mailing_list": "email-list",
    "signup": "email-list",
    "sign-up": "email-list",
}

FULL_ACCESS_EMAILS = {
    "sam@cenaskitchen.com",
    "samsahragard@gmail.com",
    "masood@cenaskitchen.com",
}

FULL_ACCESS_NAMES = {
    "sam",
    "sam sahragard",
    "masood",
    "masood sahragard",
}

ANGELICA_FORM_VISIBILITY_EMAILS = {
    "angelica@cenaskitchen.com",
}

ANGELICA_FORM_VISIBILITY_NAMES = {
    "angelica",
    "angelica barton",
}

FULL_ACCESS_ROLES = {"partner", "corporate"}
STORE_FORM_ROLES = {
    "corporate_chef",
    "gm",
    "manager",
    "km",
    "assistant_km",
    "foh_manager",
}

SHARE_LABELS = OrderedDict([
    ("tomball", "Tomball"),
    ("copperfield", "Copperfield"),
])


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


def _share_slug(raw: str | None) -> str | None:
    loc = _normalize_location(raw)
    if loc == "Tomball":
        return "tomball"
    if loc == "Copperfield":
        return "copperfield"
    return None


def _share_labels(slugs: list[str] | None) -> list[str]:
    return [SHARE_LABELS[s] for s in (slugs or []) if s in SHARE_LABELS]


def _identity_key(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _current_user() -> User | None:
    user = getattr(g, "current_user", None)
    if user is not None:
        return user
    uid = session.get("user_id")
    if uid is None:
        return None
    db = SessionLocal()
    try:
        user = db.get(User, int(uid))
        g.current_user = user if user and user.active else None
        return g.current_user
    except Exception:
        g.current_user = None
        return None
    finally:
        db.close()


def _has_full_form_access(user: User | None) -> bool:
    if user is None:
        return False
    role = (getattr(user, "permission_level", None) or "").strip().lower()
    if role in FULL_ACCESS_ROLES:
        return True
    email = _identity_key(getattr(user, "email", None))
    name = _identity_key(getattr(user, "full_name", None))
    if email in FULL_ACCESS_EMAILS or name in FULL_ACCESS_NAMES:
        return True
    return False


def _has_angelica_form_visibility(user: User | None) -> bool:
    if user is None:
        return False
    email = _identity_key(getattr(user, "email", None))
    name = _identity_key(getattr(user, "full_name", None))
    return (
        email in ANGELICA_FORM_VISIBILITY_EMAILS
        or name in ANGELICA_FORM_VISIBILITY_NAMES
    )


def _has_store_form_access(user: User | None) -> bool:
    if user is None:
        return False
    if _has_full_form_access(user):
        return True
    if _has_angelica_form_visibility(user):
        return True
    role = (getattr(user, "permission_level", None) or "").strip().lower()
    return role in STORE_FORM_ROLES


def _user_share_locations(user: User | None) -> list[str]:
    if user is None:
        return []
    if _has_full_form_access(user):
        return list(SHARE_LABELS.keys())
    scopes = {
        (scope or "").strip().lower()
        for scope in (getattr(user, "store_scope", None) or "").split(",")
    }
    if "both" in scopes:
        return list(SHARE_LABELS.keys())
    out = [slug for slug in SHARE_LABELS if slug in scopes]
    if not out and getattr(user, "permission_level", None) == "corporate":
        out = list(SHARE_LABELS.keys())
    return out


def _can_user_see_row(
    row: WebsiteFormSubmission,
    user_locations: list[str],
    *,
    require_share: bool = True,
) -> bool:
    row_location = _share_slug(row.location)
    if row.form_type == "career":
        return bool(row_location and row_location in user_locations)
    if not require_share:
        return row_location is None or row_location in user_locations
    shared = set(row.shared_locations or [])
    if not shared.intersection(user_locations):
        return False
    return row_location is None or row_location in user_locations


def _location_filters_for_user(
    full_access: bool,
    user_locations: list[str],
) -> OrderedDict[str, str]:
    if full_access:
        return LOCATION_FILTERS
    filters: OrderedDict[str, str] = OrderedDict()
    if len(user_locations) > 1:
        filters[""] = "Both"
    for slug in SHARE_LABELS:
        if slug in user_locations:
            filters[SHARE_LABELS[slug]] = SHARE_LABELS[slug]
    return filters


def _status_capabilities(
    row: WebsiteFormSubmission,
    *,
    full_access: bool,
    user_locations: list[str],
) -> dict[str, bool]:
    manager_career_access = (
        row.form_type == "career"
        and _can_user_see_row(row, user_locations)
    )
    can_archive_delete = full_access or manager_career_access
    return {
        "reviewed": full_access,
        "archived": can_archive_delete,
        "deleted": can_archive_delete,
        "new": full_access,
    }


def _require_forms_user() -> tuple[User, bool, list[str]] | Response:
    user = _current_user()
    if user is None:
        target = request.full_path if request.query_string else request.path
        return redirect("/keypad-login?next=" + quote(target, safe="/?=&"))
    full_access = _has_full_form_access(user)
    locations = _user_share_locations(user)
    if not full_access and not _has_store_form_access(user):
        abort(403)
    if not full_access and not locations:
        abort(403)
    return user, full_access, locations


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
    elif form_type == "email-list":
        subject = "Email list signup"

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
    if canonical == "email-list" and not summary.get("email"):
        abort(400)
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
    label = "email list signup" if form_type == "email-list" else FORM_LABELS.get(form_type, "message")
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
    if form_type == "email-list":
        return "Email list"
    if form_type in {"catering", "spirit", "donation"}:
        return location
    return row.subject or "General messages"


@website_forms_bp.route("/partner/website-forms", methods=["GET"])
def partner_forms():
    access = _require_forms_user()
    if isinstance(access, Response):
        return access
    user, full_access, user_locations = access
    angelica_form_visibility = _has_angelica_form_visibility(user)
    _set_partner_context()

    form_type = _canonical_form_type(request.args.get("type")) or "career"
    requested_location = _normalize_location(request.args.get("location"))
    requested_location_slug = _share_slug(requested_location)
    visible_location_filters = _location_filters_for_user(full_access, user_locations)
    if full_access:
        location_filter = requested_location if requested_location in LOCATION_FILTERS else None
        location_filter_slug = _share_slug(location_filter)
    elif requested_location_slug in user_locations:
        location_filter_slug = requested_location_slug
        location_filter = SHARE_LABELS[requested_location_slug]
    elif len(user_locations) == 1:
        location_filter_slug = user_locations[0]
        location_filter = SHARE_LABELS[location_filter_slug]
    else:
        location_filter_slug = None
        location_filter = None
    requested_status = (request.args.get("status") or "").strip().lower()
    status_filter = requested_status if requested_status in STATUS_FILTERS else ""

    db = SessionLocal()
    try:
        q = db.query(WebsiteFormSubmission).filter(
            WebsiteFormSubmission.form_type == form_type
        )
        if location_filter and full_access:
            q = q.filter(WebsiteFormSubmission.location == location_filter)
        if status_filter:
            q = q.filter(WebsiteFormSubmission.status == status_filter)
        else:
            q = q.filter(WebsiteFormSubmission.status != "deleted")
        raw_rows = q.order_by(WebsiteFormSubmission.created_at.desc()).limit(250).all()
        if full_access:
            rows = raw_rows
        else:
            rows = [
                row for row in raw_rows
                if _can_user_see_row(
                    row,
                    user_locations,
                    require_share=not angelica_form_visibility,
                )
                and (
                    not location_filter_slug
                    or location_filter_slug in (row.shared_locations or [])
                    or _share_slug(row.location) == location_filter_slug
                )
            ]
        status_capabilities = {
            row.id: _status_capabilities(
                row,
                full_access=full_access,
                user_locations=user_locations,
            )
            for row in rows
        }

        counts = {}
        for key in FORM_LABELS:
            count_q = db.query(WebsiteFormSubmission).filter(
                WebsiteFormSubmission.form_type == key
            )
            if location_filter and full_access:
                count_q = count_q.filter(WebsiteFormSubmission.location == location_filter)
            if status_filter:
                count_q = count_q.filter(WebsiteFormSubmission.status == status_filter)
            else:
                count_q = count_q.filter(WebsiteFormSubmission.status != "deleted")
            count_rows = count_q.all()
            counts[key] = (
                len(count_rows)
                if full_access
                else sum(
                    1 for row in count_rows
                    if _can_user_see_row(
                        row,
                        user_locations,
                        require_share=not angelica_form_visibility,
                    )
                    and (
                        not location_filter_slug
                        or location_filter_slug in (row.shared_locations or [])
                        or _share_slug(row.location) == location_filter_slug
                    )
                )
            )
        actor_ids = {
            row.status_changed_by_user_id
            for row in rows
            if row.status_changed_by_user_id
        }
        status_actor_names = {}
        if actor_ids:
            status_actor_names = {
                actor.id: actor.full_name
                for actor in db.query(User).filter(User.id.in_(actor_ids)).all()
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
        form_short_labels=FORM_SHORT_LABELS,
        status_filters=STATUS_FILTERS,
        location_filters=visible_location_filters,
        active_type=form_type,
        active_label=FORM_LABELS[form_type],
        counts=counts,
        groups=groups,
        rows=rows,
        selected_location=location_filter or "",
        selected_status=status_filter,
        status_actor_names=status_actor_names,
        status_capabilities=status_capabilities,
        store_label=g.store_label,
        full_access=full_access,
        user_locations=user_locations,
        share_labels=SHARE_LABELS,
        share_labels_for=_share_labels,
    )


@website_forms_bp.route(
    "/partner/website-forms/<int:submission_id>/attachment/<int:attachment_index>",
    methods=["GET"],
)
def partner_form_attachment(submission_id: int, attachment_index: int):
    access = _require_forms_user()
    if isinstance(access, Response):
        return access
    _user, full_access, user_locations = access
    angelica_form_visibility = _has_angelica_form_visibility(_user)

    db = SessionLocal()
    try:
        row = db.get(WebsiteFormSubmission, submission_id)
        if row is None:
            abort(404)
        if not full_access and not _can_user_see_row(
            row,
            user_locations,
            require_share=not angelica_form_visibility,
        ):
            abort(403)
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
    access = _require_forms_user()
    if isinstance(access, Response):
        return access
    user, full_access, _user_locations = access
    user_locations = _user_locations
    status = (request.form.get("status") or "").strip().lower()
    if status not in STATUS_ACTIONS:
        abort(400)
    db = SessionLocal()
    try:
        row = db.get(WebsiteFormSubmission, submission_id)
        if row is None:
            abort(404)
        capabilities = _status_capabilities(
            row,
            full_access=full_access,
            user_locations=user_locations,
        )
        if not capabilities.get(status):
            abort(403)
        now = datetime.utcnow()
        row.status = status
        row.status_changed_at = now
        row.status_changed_by_user_id = getattr(user, "id", None)
        row.reviewed_at = now if status == "reviewed" else row.reviewed_at
        row.reviewed_by_user_id = getattr(user, "id", None) if status == "reviewed" else row.reviewed_by_user_id
        db.commit()
        form_type = row.form_type
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return redirect(url_for("website_forms.partner_forms", type=form_type), code=303)


@website_forms_bp.route("/partner/website-forms/<int:submission_id>/share", methods=["POST"])
def partner_form_share(submission_id: int):
    access = _require_forms_user()
    if isinstance(access, Response):
        return access
    user, full_access, _user_locations = access
    if not full_access:
        abort(403)

    target = (request.form.get("share_target") or "").strip().lower()
    if target == "both":
        shared = list(SHARE_LABELS.keys())
    elif target in SHARE_LABELS:
        shared = [target]
    else:
        abort(400)

    db = SessionLocal()
    try:
        row = db.get(WebsiteFormSubmission, submission_id)
        if row is None:
            abort(404)
        row.shared_locations = shared
        row.shared_by_user_id = getattr(user, "id", None)
        row.shared_at = datetime.utcnow()
        db.commit()
        form_type = row.form_type
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return redirect(url_for("website_forms.partner_forms", type=form_type), code=303)
