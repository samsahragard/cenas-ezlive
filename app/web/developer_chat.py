"""Developer chat — Partner-only.

Persistent thread for Sam + AI agents (this Claude, CK Claude, future)
to coordinate. Lives behind the existing `/partner/` partner_auth gate
(see store_routes._partner_gate) so anyone reading or posting must
already be past the Partner password.

Routes:
    GET  /partner/developer/chat                 — chat UI (auto-polls)
    POST /partner/developer/chat/post            — submit a new message (multipart, up to 5 attachments)
    GET  /partner/developer/chat/messages.json   — JSON poll feed (?since_id=N)
    GET  /partner/developer/chat/attachment/<id> — download an attachment

The JSON endpoint is what makes it scriptable: a Claude running on AiCk
or CK can `curl` it (with the partner cookie) and stream new messages
into a local log file.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, abort, g, send_file, current_app
from werkzeug.utils import secure_filename

from app.db import SessionLocal
from app.models import (
    DeveloperChatMessage, DeveloperChatAttachment, PermissionDenial,
    SampleApproval, SampleApprovalAttachment, CenaWakeDecision,
    DevChatTodo, _VALID_DEV_CHAT_TODO_STATUS, _VALID_DEV_CHAT_TODO_ASSIGNEES,
)
# Phase 0 Block 4: gate routes on the permission system. Decorator
# runs first; the in-handler _enforce_partner() helper stays as
# belt-and-suspenders during dark-launch.
from app.services.permissions import requires_permission

log = logging.getLogger(__name__)

dev_chat = Blueprint("developer_chat", __name__)

CT = timezone(timedelta(hours=-5))   # Central time for display

# Attachment upload limits
MAX_ATTACHMENTS_PER_MESSAGE = 5
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024   # 20 MB (5/msg => ~100 MB total)
ALLOWED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",  # images
    ".pdf",                                              # docs
    ".csv", ".txt", ".md", ".log", ".html",              # text dumps + saved HTML
    ".xlsx", ".xls",                                     # spreadsheets
    ".webm", ".ogg", ".mp3", ".wav", ".m4a",             # audio (voice msgs)
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}
AUDIO_EXTENSIONS = {".webm", ".ogg", ".mp3", ".wav", ".m4a"}

# Canonical agent roster. Drives is_ai detection, CSS class lookup,
# and the author dropdown in the UI. Add new agents here — nowhere else.
AGENT_ROSTER = {
    "aick-claude": {"display": "aick",   "css_class": "msg-aick",  "is_ai": True},
    "aick":        {"display": "aick",   "css_class": "msg-aick",  "is_ai": True},
    "ck-claude":   {"display": "ck",     "css_class": "msg-ck",    "is_ai": True},
    "ck":          {"display": "ck",     "css_class": "msg-ck",    "is_ai": True},
    "dck-claude":  {"display": "dck",    "css_class": "msg-dck",   "is_ai": True},
    "dck":         {"display": "dck",    "css_class": "msg-dck",   "is_ai": True},
    "samai":       {"display": "samai",  "css_class": "msg-samai", "is_ai": True},
    "cena":        {"display": "cena",   "css_class": "msg-cena",  "is_ai": True},
    "sam":         {"display": "sam",    "css_class": "msg-sam",   "is_ai": False},
    "masood":      {"display": "masood", "css_class": "msg-masood","is_ai": False},
    "system":      {"display": "system", "css_class": "msg-system","is_ai": False},
}


def _attachments_dir() -> Path:
    """Where attachment files live on disk. Defaults to /var/data/chat-attachments
    (Render persistent disk); override with CHAT_ATTACHMENTS_DIR for local dev."""
    base = os.environ.get("CHAT_ATTACHMENTS_DIR", "/var/data/chat-attachments")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _enforce_partner():
    """Belt-and-suspenders: store_bp.before_request already gates /partner/*,
    but this blueprint isn't under store_bp's URL prefix — these routes are
    registered at /partner/developer/chat directly, so we re-check the
    session flag here. (We can't rely on g.current_store being set since the
    URL doesn't go through the <store> prefix preprocessor.)"""
    from flask import session
    if not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login"))
    return None


SAMPLES = [
    dict(
        slug="dck-interview-tracker",
        title="Interview Tracker",
        version=None,
        date="2026-05-20",
        description="dck's Interview Tracker — 4-stage candidate pipeline (Applied / 1st / 2nd / Hired) with a candidate detail view and interview-history timeline (Sam #5:48). Full-page standalone render.",
        url="/static/mockups/dck_interview_tracker_render.html",
        type="mockup",
    ),
    dict(
        slug="dck-manager-log-redesign",
        title="Manager Log — Redesign",
        version=None,
        date="2026-05-20",
        description="dck's redesign of the Daily Manager Log page (Sam #5:10). Full-page standalone render.",
        url="/static/mockups/dck_manager_log_redesign_render.html",
        type="mockup",
    ),
    dict(
        slug="build-plan",
        title="Build Plan",
        version=None,
        date="2026-05-17",
        description="plan.md — master build specification. Phases, surfaces, constraints, success criteria.",
        url="/partner/developer/plan",
        type="reference",
    ),
]


@dev_chat.route("/partner/developer/chat", methods=["GET"])
@requires_permission("developer.view_chat")
def chat_page():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        messages = (
            db.query(DeveloperChatMessage)
            .order_by(DeveloperChatMessage.id.asc())
            .all()
        )
        rendered = [_render_msg(m) for m in messages]
        last_id = messages[-1].id if messages else 0
    finally:
        db.close()
    # Synthesize the per-store sidebar context. This page lives under
    # /partner/ but the URL doesn't pass through the store_slug prefix,
    # so we set g manually for the sidebar template.
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    return render_template(
        "developer_chat.html",
        active="dev_chat",
        page_title="Developer Chat",
        messages=rendered,
        last_id=last_id,
        upload_error=request.args.get("upload_error"),
        max_attachments=MAX_ATTACHMENTS_PER_MESSAGE,
        max_attachment_mb=int(MAX_ATTACHMENT_BYTES / 1024 / 1024),
        allowed_extensions=sorted(ALLOWED_EXTENSIONS),
    )


@dev_chat.route("/partner/developer/chat/post", methods=["POST"])
@requires_permission("developer.view_chat")
def post_message():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    # Default missing/empty author to "unknown" rather than silently
    # attributing to "sam". Per cena #2014 + aick #2013 diagnosis of the
    # 2026-05-17 attribution incident: a client that forgot the form
    # field landed its post under sam's name, muddying the audit trail.
    # Real fix is per-agent identity (see samai-spec future); this is the
    # smallest-blast-radius bleeding stop.
    author = (request.form.get("author") or "unknown").strip()[:60]
    body = (request.form.get("body") or "").strip()

    # Sam directive 2026-05-23: cena is disconnected from /partner/developer/chat.
    # Server-side belt against every path — bridge mirror, Cena's
    # post_to_dev_chat tool, or any future relay — that might try to
    # surface a cena-authored post here. Reject loudly so the caller
    # (typically Cena's tool) gets the reason back and stops retrying.
    if author.strip().lower() == "cena":
        log.info("dev-chat post: rejected cena (Sam directive 2026-05-23)")
        return _post_error(
            "cena is disconnected from dev chat (Sam directive 2026-05-23) — "
            "post lands on the LAN hub + cena_sam_chat only"
        )

    # Parse uploaded files. Empty FileStorage entries (no filename) get skipped.
    files = [f for f in request.files.getlist("attachments") if f and f.filename]
    if len(files) > MAX_ATTACHMENTS_PER_MESSAGE:
        return _post_error(f"Too many attachments — max {MAX_ATTACHMENTS_PER_MESSAGE} per message")

    # Validate each file before we touch the DB
    validated: list[tuple] = []  # (orig_name, ext, bytes_data)
    for f in files:
        orig = f.filename or ""
        ext = os.path.splitext(orig)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return _post_error(f"Unsupported file type: {ext or '(none)'}. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
        data = f.read()
        if len(data) > MAX_ATTACHMENT_BYTES:
            return _post_error(f"{orig} is {len(data)/1024/1024:.1f} MB — limit is {MAX_ATTACHMENT_BYTES/1024/1024:.0f} MB")
        if not data:
            continue  # skip empties
        validated.append((orig, ext, data))

    # An empty body is OK as long as at least one attachment is present.
    if not body and not validated:
        return redirect(url_for("developer_chat.chat_page"))

    db = SessionLocal()
    try:
        m = DeveloperChatMessage(author=author, body=body)
        db.add(m)
        db.flush()   # need m.id for the file paths

        for orig, ext, data in validated:
            safe = secure_filename(orig) or f"file{ext}"
            # Avoid filename collisions inside one message
            safe = _ensure_unique_in_msg(safe, m.id)
            msg_dir = _attachments_dir() / str(m.id)
            msg_dir.mkdir(parents=True, exist_ok=True)
            target = msg_dir / safe
            target.write_bytes(data)
            mime = (mimetypes.guess_type(safe)[0] or "application/octet-stream")
            att = DeveloperChatAttachment(
                message_id=m.id,
                filename=orig[:255],
                mime_type=mime[:100],
                size_bytes=len(data),
                storage_path=f"{m.id}/{safe}",
                is_image=(ext in IMAGE_EXTENSIONS),
            )
            db.add(att)

        db.commit()
        log.info(
            "dev-chat post: %s wrote %d chars + %d attachments",
            author, len(body), len(validated),
        )
        # Capture id/author/body before db close — the thread reads
        # primitives, never a SQLAlchemy row (avoids detached-instance
        # errors when the session closes in finally).
        wake_args = (m.id, author, body)

        # Rolling cap per Sam dev chat 2026-05-19 4:07pm: hot table
        # capped at 200; when count exceeds it, archive the oldest 100
        # then delete those from live. Archive is append-only.
        # samai #2980 spec — INSERT into archive before DELETE.
        try:
            live_count = db.query(DeveloperChatMessage).count()
            if live_count > 200:
                from app.models import DeveloperChatMessageArchive as _DCMA
                oldest = (
                    db.query(DeveloperChatMessage)
                    .order_by(DeveloperChatMessage.id.asc())
                    .limit(100)
                    .all()
                )
                ids_to_drop = [o.id for o in oldest]
                for old in oldest:
                    db.add(_DCMA(
                        original_id=old.id,
                        created_at=old.created_at,
                        author=old.author,
                        body=old.body,
                    ))
                db.flush()
                db.query(DeveloperChatMessage).filter(
                    DeveloperChatMessage.id.in_(ids_to_drop)
                ).delete(synchronize_session=False)
                db.commit()
                log.info(
                    "dev-chat trim: archived+deleted %d oldest (live was %d)",
                    len(ids_to_drop), live_count,
                )
        except Exception:
            log.exception("dev-chat trim trigger failed (non-fatal)")
            db.rollback()
    finally:
        db.close()
    # Sam #2342: wake cena on EVERY new dev-chat post (immediate, no
    # 30s watcher poll wait). Cena decides whether to respond.
    # Skip-author guard prevents cena ↔ cena infinite loop.
    _wake_cena_on_post(*wake_args)
    return redirect(url_for("developer_chat.chat_page") + "#bottom")


def _wake_cena_on_post(msg_id: int, author: str, body: str) -> None:
    """Fire-and-forget POST to cena gateway on every dev-chat post.

    Sam #2342 spec: "as soon as any text is written on this chat. it
    pings cena, she reads it and decides to respond or not." Cuts the
    30s cena_chat_watcher poll latency to ~immediate for every post.

    Loop prevention: skip authors {cena, cena-watcher}. Otherwise
    cena's own post_to_dev_chat would re-wake her ad infinitum.

    Fire-and-forget: spawns a daemon thread so the HTTP-redirect
    response to the user is not blocked by cena's tool turn (which
    can take 10-30s). Failure to fire is logged but never raises.
    Watcher (cena_chat_watcher.py) stays running as a 30s fallback
    safety net in case the in-process fire fails."""
    SKIP = {"cena", "cena-watcher"}
    a = (author or "").strip().lower()
    if a in SKIP:
        return
    gateway_url = (os.getenv("CENA_GATEWAY_URL") or "").strip().rstrip("/")
    if not gateway_url:
        return
    token = (os.getenv("CENA_GATEWAY_TOKEN") or "").strip()
    if not token:
        return

    import json as _json_wake
    import threading as _threading_wake
    import urllib.request as _urlreq_wake

    prompt = (
        f"NEW DEV CHAT MESSAGE\n"
        f"author: {author}\n"
        f"id: {msg_id}\n"
        f"body:\n{body}\n\n"
        f"If this message names you or asks for your action, respond "
        f"now via post_to_dev_chat. If it's team cross-talk that "
        f"doesn't need you, briefly acknowledge in your internal text "
        f"reply but don't post — silence is fine."
    )
    payload = _json_wake.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "model": "claude-opus-4-7",
        "max_tokens": 3000,
    }).encode()
    req = _urlreq_wake.Request(
        f"{gateway_url}/cena/stream",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Cena-Token": token,
        },
        method="POST",
    )

    def _fire():
        try:
            with _urlreq_wake.urlopen(req, timeout=180) as r:
                r.read()  # drain SSE so cena's tool calls run
        except Exception as e:  # noqa: BLE001
            log.warning("cena wake fire failed for msg_id=%s: %s",
                        msg_id, e)

    t = _threading_wake.Thread(target=_fire, daemon=True,
                               name=f"cena-wake-{msg_id}")
    t.start()


def _ensure_unique_in_msg(name: str, msg_id: int) -> str:
    """If <attachments_dir>/<msg_id>/<name> already exists, suffix -2, -3, ..."""
    base = _attachments_dir() / str(msg_id)
    if not (base / name).exists():
        return name
    stem, ext = os.path.splitext(name)
    i = 2
    while (base / f"{stem}-{i}{ext}").exists():
        i += 1
    return f"{stem}-{i}{ext}"


def _post_error(msg: str):
    """Pass an error back via flash-style query param so the chat page can surface it."""
    from urllib.parse import quote
    return redirect(url_for("developer_chat.chat_page") + f"?upload_error={quote(msg)}")


@dev_chat.route("/partner/developer/chat/attachment/<int:att_id>", methods=["GET"])
@requires_permission("developer.view_chat")
def download_attachment(att_id: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        att = db.get(DeveloperChatAttachment, att_id)
        if not att:
            abort(404)
        # Resolve safely against attachments dir — if storage_path tries to
        # escape via .. or absolute path, refuse.
        base = _attachments_dir().resolve()
        full = (base / att.storage_path).resolve()
        try:
            full.relative_to(base)
        except ValueError:
            abort(404)
        if not full.is_file():
            abort(404)
        # For images and audio, serve inline so the browser can render
        # thumbnails / play in <audio> tags. For everything else, attach so
        # the browser downloads.
        ext = os.path.splitext(att.filename or "")[1].lower()
        is_audio = (ext in AUDIO_EXTENSIONS) or (att.mime_type or "").startswith("audio/")
        as_attachment = not att.is_image and not is_audio
        return send_file(
            str(full),
            mimetype=att.mime_type or "application/octet-stream",
            as_attachment=as_attachment,
            download_name=att.filename,
            max_age=0,
        )
    finally:
        db.close()


@dev_chat.route("/partner/developer/chat/messages.json", methods=["GET"])
@requires_permission("developer.view_chat")
def messages_json():
    """Poll endpoint for the chat UI's JS auto-refresh AND for AI agents
    using a `chat_tail.py` style script."""
    gate = _enforce_partner()
    if gate is not None:
        # Don't render an HTML login page in JSON context — return 401 so
        # callers (incl. AI agents) can detect the session expired.
        return jsonify({"error": "partner_auth_required"}), 401
    since_id = int(request.args.get("since_id") or 0)
    db = SessionLocal()
    try:
        q = db.query(DeveloperChatMessage)
        if since_id:
            q = q.filter(DeveloperChatMessage.id > since_id)
        msgs = q.order_by(DeveloperChatMessage.id.asc()).all()
        out = [_msg_to_dict(m) for m in msgs]
        last_id = msgs[-1].id if msgs else since_id
    finally:
        db.close()
    return jsonify({"messages": out, "last_id": last_id})


def _msg_to_dict(m: DeveloperChatMessage) -> dict:
    atts = []
    for a in m.attachments or []:
        ext = os.path.splitext(a.filename or "")[1].lower()
        is_audio = (ext in AUDIO_EXTENSIONS) or (a.mime_type or "").startswith("audio/")
        atts.append({
            "id": a.id,
            "filename": a.filename,
            "mime_type": a.mime_type,
            "size_bytes": a.size_bytes,
            "is_image": a.is_image,
            "is_audio": is_audio,
            "url": url_for("developer_chat.download_attachment", att_id=a.id),
        })
    a_low = (m.author or "").lower()
    roster_entry = AGENT_ROSTER.get(a_low)
    is_ai = roster_entry["is_ai"] if roster_entry else False
    return {
        "id": m.id,
        "author": m.author,
        "body": m.body,
        "is_ai": is_ai,
        "created_at_iso": m.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "created_at_display": m.created_at.replace(tzinfo=timezone.utc).astimezone(CT).strftime("%a %b %d, %I:%M %p"),
        "attachments": atts,
    }


def _render_msg(m: DeveloperChatMessage) -> dict:
    """Adds CSS class hints based on author for color coding. Driven by AGENT_ROSTER."""
    d = _msg_to_dict(m)
    a = (m.author or "").lower()
    entry = AGENT_ROSTER.get(a)
    if entry:
        d["css_class"] = entry["css_class"]
    elif "aick" in a:
        d["css_class"] = "msg-aick"
    elif "claude" in a:
        d["css_class"] = "msg-aick"
    else:
        d["css_class"] = "msg-other"
    return d


# ============== App Docs — REMOVED (Sam directive #241 2026-05-23) ==============
# The /partner/developer/app/* docs subtree was retired in favor of the
# consolidated /sam/docs surface under the Cena page. Every template
# under app/templates/docs/ was backed up to
# aick:Desktop/docssamonly/ before deletion. The three serving routes
# (download.zip, denials, app/<page>) were removed alongside DOC_PAGES,
# CHAT_PAGES, and SOURCE_PAGES.
#
# The dev chat itself (/partner/developer/chat) is unaffected; only the
# App-docs per-page nav + routes went away. Surfaces that previously
# linked into /partner/developer/app/<slug>:
#   * sidebar.html — App-docs section disabled in the same batch.
#   * access_denied.html — permission-system link dropped.
#   * Module docstring breadcrumbs in app/models.py + app/services/*
#     kept as comments (no live links) — they document spec provenance,
#     not navigation, and removing them would lose history.
# Recovery: aick:Desktop/docssamonly/developer_chat.py.bak holds the
# pre-deletion file; aick:Desktop/docssamonly/<slug>.html holds each
# template. Sam approved the move + delete (#241).
#
# Stub kept here so existing imports of DOC_PAGES + CHAT_PAGES from
# this module don't ImportError at startup. The lists are empty; any
# code that still iterates them produces zero rows (safe degrade).
DOC_PAGES: list = []
CHAT_PAGES: list = []


@dev_chat.route("/partner/developer/plan")
@requires_permission("developer.view_chat")
def developer_plan():
    """Serve plan.md as a formatted readable view (Cena #2549 Item 3)."""
    import pathlib as _pl
    from flask import current_app
    plan_path = _pl.Path(current_app.root_path).parent / "plan.md"
    if plan_path.exists():
        content = plan_path.read_text(encoding="utf-8")
    else:
        content = "plan.md not found in repo root."
    gate = _enforce_partner()
    if gate is not None:
        return gate
    return render_template("plan_viewer.html", plan_content=content, active="doc_plan")


@dev_chat.route("/partner/developer/samples")
@requires_permission("developer.view_chat")
def developer_samples():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    from app.web.sam_chat import is_sam_chat_user
    db = SessionLocal()
    try:
        approvals_by_slug = {
            a.sample_slug: a for a in db.query(SampleApproval).all()
        }
        atts_by_approval_id: dict[int, list] = {}
        for att in db.query(SampleApprovalAttachment).all():
            atts_by_approval_id.setdefault(att.sample_approval_id, []).append(att)
        enriched = []
        for s in SAMPLES:
            ap = approvals_by_slug.get(s.get("slug"))
            enriched.append({
                **s,
                "approval_status": ap.status if ap else "pending",
                "approval_notes": ap.notes if ap else None,
                "approval_attachments": atts_by_approval_id.get(ap.id, []) if ap else [],
                "approval_marked_at": ap.updated_at if ap else None,
                "approval_marked_by": ap.marked_by_user_id if ap else None,
            })
    finally:
        db.close()

    # Status filter per Sam #2677 + cena #2681 + #2710: hide approved
    # cards by default so the Samples page becomes an active work queue;
    # "Show approved" toggle reveals them for audit/pattern reference.
    # Don't hard-delete SAMPLES dict entries (history preserved).
    show_approved = request.args.get("show_approved", "").lower() in (
        "1", "true", "yes", "on"
    )
    approved_total = sum(
        1 for s in enriched if s["approval_status"] == "approved"
    )
    visible = enriched if show_approved else [
        s for s in enriched if s["approval_status"] != "approved"
    ]

    return render_template(
        "developer_samples.html",
        active="dev_samples",
        samples=visible,
        all_samples=enriched,
        approved_total=approved_total,
        show_approved=show_approved,
        is_sam=is_sam_chat_user(),
    )


# ============================================================
# Samples approval workflow (spec_samples_approval_workflow)
# ============================================================

SAMPLE_APPROVAL_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per file (spec §2.2)
SAMPLE_APPROVAL_MAX_TOTAL = 20 * 1024 * 1024  # 20 MB total per approval
SAMPLE_APPROVAL_ALLOWED_MIMES = {"image/png", "image/jpeg", "image/webp"}
SAMPLE_APPROVAL_ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _sample_approval_attachments_dir() -> Path:
    """Where sample-approval attachments live on disk. Defaults to
    /var/data/sample-approval-attachments (Render persistent disk).
    Override with SAMPLE_APPROVAL_ATTACHMENTS_DIR for local dev."""
    base = os.environ.get(
        "SAMPLE_APPROVAL_ATTACHMENTS_DIR",
        "/var/data/sample-approval-attachments",
    )
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_sample_slug(slug: str) -> dict | None:
    """Return the SAMPLES dict matching <slug>, or None."""
    for s in SAMPLES:
        if s.get("slug") == slug:
            return s
    return None


def _get_or_create_approval(db, slug: str, user_id: int | None) -> SampleApproval:
    """Fetch the approval row for this slug, or create one with status='pending'."""
    ap = db.query(SampleApproval).filter(SampleApproval.sample_slug == slug).one_or_none()
    if ap is None:
        ap = SampleApproval(sample_slug=slug, status="pending",
                            marked_by_user_id=user_id)
        db.add(ap)
        db.flush()  # need ap.id for attachments
    return ap


def _require_sam_for_approval():
    """Sam-only gate on POST routes per spec §3 auth. Returns a Response to
    short-circuit (403), or None to proceed."""
    from app.web.sam_chat import is_sam_chat_user
    if not is_sam_chat_user():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return None


def _save_approval_attachments(db, ap: SampleApproval, files: list, user_id: int | None) -> tuple[list, str | None]:
    """Validate + persist uploaded files. Returns (saved_attachments, error_msg)."""
    if not files:
        return [], None
    total = 0
    for f in files:
        try:
            f.stream.seek(0, os.SEEK_END)
            size = f.stream.tell()
            f.stream.seek(0)
        except Exception:
            size = 0
        total += size
        if size > SAMPLE_APPROVAL_MAX_BYTES:
            return [], f"{f.filename} is {size/1024/1024:.1f} MB — per-file limit is 5 MB"
    if total > SAMPLE_APPROVAL_MAX_TOTAL:
        return [], f"total upload {total/1024/1024:.1f} MB exceeds 20 MB cap"
    saved = []
    base_dir = _sample_approval_attachments_dir() / str(ap.id)
    base_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        orig = f.filename or ""
        ext = os.path.splitext(orig)[1].lower()
        if ext not in SAMPLE_APPROVAL_ALLOWED_EXTS:
            continue
        data = f.read()
        if not data:
            continue
        safe = secure_filename(orig) or f"upload{ext}"
        # collision dodge
        i = 2
        cand = safe
        while (base_dir / cand).exists():
            stem, e = os.path.splitext(safe)
            cand = f"{stem}-{i}{e}"
            i += 1
        (base_dir / cand).write_bytes(data)
        mime = mimetypes.guess_type(cand)[0] or "application/octet-stream"
        if mime not in SAMPLE_APPROVAL_ALLOWED_MIMES:
            # unknown image subtype — keep the file but log
            log.info("sample-approval attach: unusual mime %s for %s", mime, orig)
        att = SampleApprovalAttachment(
            sample_approval_id=ap.id,
            filename=orig[:255],
            mime_type=mime[:64],
            byte_size=len(data),
            storage_path=f"{ap.id}/{cand}",
            created_by_user_id=user_id,
        )
        db.add(att)
        saved.append(att)
    return saved, None


def _approval_payload(ap: SampleApproval, atts: list[SampleApprovalAttachment]) -> dict:
    """Serialize an approval + its attachments for JSON responses."""
    return {
        "slug": ap.sample_slug,
        "status": ap.status,
        "notes": ap.notes,
        "updated_at": ap.updated_at.isoformat() if ap.updated_at else None,
        "attachments": [
            {"id": a.id, "filename": a.filename, "mime_type": a.mime_type, "byte_size": a.byte_size}
            for a in atts
        ],
    }


@dev_chat.route("/partner/developer/samples/<slug>/approve", methods=["POST"])
@requires_permission("developer.view_chat")
def sample_approve(slug: str):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    sam_gate = _require_sam_for_approval()
    if sam_gate is not None:
        return sam_gate
    if _resolve_sample_slug(slug) is None:
        return jsonify({"ok": False, "error": "unknown_slug"}), 404
    user = getattr(g, "current_user", None)
    uid = getattr(user, "id", None)
    notes = (request.form.get("notes") or "").strip() or None
    files = [f for f in request.files.getlist("files") if f and f.filename]
    db = SessionLocal()
    try:
        ap = _get_or_create_approval(db, slug, uid)
        ap.status = "approved"
        if notes is not None:
            ap.notes = notes
        ap.marked_by_user_id = uid
        ap.updated_at = datetime.now(timezone.utc)
        db.flush()
        _, err = _save_approval_attachments(db, ap, files, uid)
        if err:
            db.rollback()
            return jsonify({"ok": False, "error": err}), 400
        db.commit()
        atts = db.query(SampleApprovalAttachment).filter(
            SampleApprovalAttachment.sample_approval_id == ap.id
        ).all()
        return jsonify({"ok": True, "approval": _approval_payload(ap, atts)})
    finally:
        db.close()


@dev_chat.route("/partner/developer/samples/<slug>/reject", methods=["POST"])
@requires_permission("developer.view_chat")
def sample_reject(slug: str):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    sam_gate = _require_sam_for_approval()
    if sam_gate is not None:
        return sam_gate
    if _resolve_sample_slug(slug) is None:
        return jsonify({"ok": False, "error": "unknown_slug"}), 404
    user = getattr(g, "current_user", None)
    uid = getattr(user, "id", None)
    notes = (request.form.get("notes") or "").strip() or None
    files = [f for f in request.files.getlist("files") if f and f.filename]
    db = SessionLocal()
    try:
        ap = _get_or_create_approval(db, slug, uid)
        ap.status = "rejected"
        if notes is not None:
            ap.notes = notes
        ap.marked_by_user_id = uid
        ap.updated_at = datetime.now(timezone.utc)
        db.flush()
        _, err = _save_approval_attachments(db, ap, files, uid)
        if err:
            db.rollback()
            return jsonify({"ok": False, "error": err}), 400
        db.commit()
        atts = db.query(SampleApprovalAttachment).filter(
            SampleApprovalAttachment.sample_approval_id == ap.id
        ).all()
        return jsonify({"ok": True, "approval": _approval_payload(ap, atts)})
    finally:
        db.close()


@dev_chat.route("/partner/developer/samples/<slug>/notes", methods=["POST"])
@requires_permission("developer.view_chat")
def sample_notes(slug: str):
    """Save notes + attachments WITHOUT changing approval status."""
    gate = _enforce_partner()
    if gate is not None:
        return gate
    sam_gate = _require_sam_for_approval()
    if sam_gate is not None:
        return sam_gate
    if _resolve_sample_slug(slug) is None:
        return jsonify({"ok": False, "error": "unknown_slug"}), 404
    user = getattr(g, "current_user", None)
    uid = getattr(user, "id", None)
    notes = (request.form.get("notes") or "").strip() or None
    files = [f for f in request.files.getlist("files") if f and f.filename]
    db = SessionLocal()
    try:
        ap = _get_or_create_approval(db, slug, uid)
        if notes is not None:
            ap.notes = notes
            ap.updated_at = datetime.now(timezone.utc)
        db.flush()
        _, err = _save_approval_attachments(db, ap, files, uid)
        if err:
            db.rollback()
            return jsonify({"ok": False, "error": err}), 400
        db.commit()
        atts = db.query(SampleApprovalAttachment).filter(
            SampleApprovalAttachment.sample_approval_id == ap.id
        ).all()
        return jsonify({"ok": True, "approval": _approval_payload(ap, atts)})
    finally:
        db.close()


@dev_chat.route("/partner/developer/samples/<slug>/attachments/<int:att_id>/delete", methods=["POST"])
@requires_permission("developer.view_chat")
def sample_attachment_delete(slug: str, att_id: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    sam_gate = _require_sam_for_approval()
    if sam_gate is not None:
        return sam_gate
    db = SessionLocal()
    try:
        att = db.get(SampleApprovalAttachment, att_id)
        if att is None:
            return jsonify({"ok": False, "error": "not_found"}), 404
        # Verify the attachment is on the requested slug
        ap = db.get(SampleApproval, att.sample_approval_id)
        if ap is None or ap.sample_slug != slug:
            return jsonify({"ok": False, "error": "slug_mismatch"}), 404
        # Remove file (best-effort)
        try:
            base = _sample_approval_attachments_dir().resolve()
            full = (base / att.storage_path).resolve()
            full.relative_to(base)
            if full.is_file():
                full.unlink()
        except Exception as e:
            log.warning("sample-approval attachment delete: file unlink failed: %s", e)
        db.delete(att)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@dev_chat.route("/partner/developer/samples/attachments/<int:att_id>", methods=["GET"])
@requires_permission("developer.view_chat")
def sample_attachment_serve(att_id: int):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        att = db.get(SampleApprovalAttachment, att_id)
        if att is None:
            abort(404)
        base = _sample_approval_attachments_dir().resolve()
        full = (base / att.storage_path).resolve()
        try:
            full.relative_to(base)
        except ValueError:
            abort(404)
        if not full.is_file():
            abort(404)
        return send_file(
            str(full),
            mimetype=att.mime_type or "application/octet-stream",
            as_attachment=False,
            download_name=att.filename,
        )
    finally:
        db.close()


# ============================================================
# Cena wake-decision telemetry dashboard
# ============================================================
# Read-only viewer for cena_wake_decisions (migration 29). Per Sam #2576
# 6-piece proposal Phase A #4 + cena #2572 + #2575 refinements: shows
# the data we'll need to decide when to flip the watcher from shadow to
# enforce. Per-author label breakdown + daily cost estimate at top per
# cena's two asks.

# Haiku 4.5 pricing (USD per 1M tokens, as of 2026-01). Update these
# when Anthropic publishes new rates.
_HAIKU_INPUT_PRICE_PER_MTOK = 1.00
_HAIKU_OUTPUT_PRICE_PER_MTOK = 5.00
_HAIKU_CACHE_READ_PRICE_PER_MTOK = 0.10
_HAIKU_CACHE_WRITE_5M_PRICE_PER_MTOK = 1.25


def _cena_decision_cost_usd(d: CenaWakeDecision) -> float:
    """Per-row USD cost from token columns. Returns 0.0 if all token
    columns are None (e.g., classifier_label='error' before API call)."""
    inp = d.classifier_input_tokens or 0
    outp = d.classifier_output_tokens or 0
    cache_r = d.classifier_cache_read_tokens or 0
    cache_c = d.classifier_cache_create_tokens or 0
    return (
        inp * _HAIKU_INPUT_PRICE_PER_MTOK / 1_000_000
        + outp * _HAIKU_OUTPUT_PRICE_PER_MTOK / 1_000_000
        + cache_r * _HAIKU_CACHE_READ_PRICE_PER_MTOK / 1_000_000
        + cache_c * _HAIKU_CACHE_WRITE_5M_PRICE_PER_MTOK / 1_000_000
    )


@dev_chat.route("/partner/developer/samples/approval-events", methods=["GET"])
@requires_permission("developer.view_chat")
def sample_approval_events():
    """Polling feed of recent SampleApproval status changes for the dck
    samples_watch.py consumer (per cena #2735 + scope #2736 + dck #2737).

    Query params:
      - since: ISO-8601 timestamp. Returns rows where updated_at > since.
               If omitted or unparseable, returns all rows (treat as "since
               epoch"). Server returns `now` field — caller stores that as
               their next-poll cursor to close the race window where an
               approval lands mid-serialization (cena #2739).

    CONSUMER GOTCHA — URL-encode the `since` parameter (cena #2760 +
    samai #2756 + dck #2759):
      The `now` field returned in the response is a server UTC ISO with
      explicit `+00:00` suffix (e.g. "2026-05-18T16:14:16.603406+00:00").
      Consumers passing it back as `?since=<now>` MUST urlencode — the
      raw `+` decodes to space on the server side, the resulting string
      fails ISO parse, the route silently falls back to no-filter, and
      the consumer appears stuck replaying every event each poll.
      Canonical pattern: `urllib.parse.quote(since_ts)` →
      `+` becomes `%2B`. See scripts/samples_watch.py:129 for the
      reference consumer implementation.

    Response:
      {
        "now": "2026-05-18T11:05:00.123456+00:00",
        "events": [
          {
            "slug": "drivers-redesign-v2",
            "title": "Drivers Page Redesign",
            "status": "approved"|"rejected"|"pending",
            "notes": "...",
            "marked_by_user_id": 1,
            "marked_at": "2026-05-18T10:30:00+00:00",
            "attachments": [
              {"id": 12, "filename": "shot.png",
               "url": "/partner/developer/samples/attachments/12"}
            ]
          },
          ...
        ]
      }

    Latest-state-only: one row per slug, no event history. If Sam flips
    approve→reject→approve, consumer sees only the final state. Per dck
    #2737 + scope #2736 trade-off (full audit would need a separate
    SampleApprovalEvent log table — out of scope for v1).
    """
    gate = _enforce_partner()
    if gate is not None:
        return gate
    since_raw = (request.args.get("since") or "").strip()
    since_dt: datetime | None = None
    if since_raw:
        # Tolerate "Z" suffix + with-or-without microseconds. Anything we
        # can't parse means "from the beginning" — safer than 400'ing the
        # consumer mid-poll.
        try:
            since_dt = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
        except ValueError:
            since_dt = None
    db = SessionLocal()
    try:
        q = db.query(SampleApproval)
        if since_dt is not None:
            q = q.filter(SampleApproval.updated_at > since_dt)
        rows = q.order_by(SampleApproval.updated_at.desc()).all()
        # Eager-load attachments to avoid N+1
        att_ids = [r.id for r in rows]
        atts_by_approval: dict[int, list] = {}
        if att_ids:
            for att in db.query(SampleApprovalAttachment).filter(
                SampleApprovalAttachment.sample_approval_id.in_(att_ids)
            ).all():
                atts_by_approval.setdefault(att.sample_approval_id, []).append(att)
        # title lookup from SAMPLES dict for human-readable consumer output
        title_by_slug = {s.get("slug"): s.get("title") for s in SAMPLES if s.get("slug")}
        events = []
        for r in rows:
            events.append({
                "slug": r.sample_slug,
                "title": title_by_slug.get(r.sample_slug, r.sample_slug),
                "status": r.status,
                "notes": r.notes,
                "marked_by_user_id": r.marked_by_user_id,
                "marked_at": r.updated_at.isoformat() if r.updated_at else None,
                "attachments": [
                    {
                        "id": a.id,
                        "filename": a.filename,
                        "url": f"/partner/developer/samples/attachments/{a.id}",
                    }
                    for a in atts_by_approval.get(r.id, [])
                ],
            })
        # Capture server time AFTER the query so cursor never skips a row
        # that landed during serialization.
        server_now = datetime.now(timezone.utc).isoformat()
    finally:
        db.close()
    return jsonify({"now": server_now, "events": events})


@dev_chat.route("/partner/developer/cena-stats")
@requires_permission("developer.view_chat")
def developer_cena_stats():
    """Cena wake-decision shadow-mode dashboard. Per Sam #2576 6-piece
    Phase A #4 (cena #2572 + #2575 refinements).

    Surfaces:
      - 24h aggregate: total decisions, label distribution, total cost
      - Per-author label breakdown (cena #2575 ask #1)
      - Disagreement count: classifier-would-fire vs watcher-did-fire
        delta — the headline number that gates the cutover decision
      - Recent 50 decisions: timestamp, author, snippet, label,
        confidence, would_fire, did_fire, tokens, latency
    """
    gate = _enforce_partner()
    if gate is not None:
        return gate

    from datetime import datetime, timedelta
    db = SessionLocal()
    try:
        cutoff_24h = datetime.utcnow() - timedelta(hours=24)

        # 24-hour aggregate set
        rows_24h = (
            db.query(CenaWakeDecision)
              .filter(CenaWakeDecision.created_at >= cutoff_24h)
              .all()
        )
        total_24h = len(rows_24h)
        cost_24h = sum(_cena_decision_cost_usd(d) for d in rows_24h)

        # Label counts (24h)
        label_counts_24h: dict[str, int] = {}
        for d in rows_24h:
            label_counts_24h[d.classifier_label] = (
                label_counts_24h.get(d.classifier_label, 0) + 1
            )

        # Per-author x label breakdown (24h) — cena #2575 ask #1
        author_label_counts: dict[str, dict[str, int]] = {}
        for d in rows_24h:
            a = d.author or "(unknown)"
            author_label_counts.setdefault(a, {})
            author_label_counts[a][d.classifier_label] = (
                author_label_counts[a].get(d.classifier_label, 0) + 1
            )
        # Order authors by total descending for table stability
        author_breakdown = sorted(
            ((a, counts, sum(counts.values()))
             for a, counts in author_label_counts.items()),
            key=lambda x: -x[2],
        )

        # Would-fire vs did-fire delta (24h) — the cutover signal
        would_fire_24h = sum(1 for d in rows_24h if d.would_fire)
        did_fire_24h = sum(1 for d in rows_24h if d.did_fire)
        # Disagreements: classifier said skip but watcher fired, OR
        # classifier said wake but watcher didn't (shouldn't happen in
        # shadow mode under current rules, but useful to surface either
        # direction).
        false_negative_count = sum(
            1 for d in rows_24h
            if d.did_fire and not d.would_fire
            and d.classifier_label != "error"
        )  # watcher fired, classifier would've skipped → "wasted wake"
        false_positive_count = sum(
            1 for d in rows_24h
            if d.would_fire and not d.did_fire
            and d.classifier_label != "error"
        )  # classifier says wake, watcher didn't (rare under current rules)
        error_count = sum(
            1 for d in rows_24h if d.classifier_label == "error"
        )

        # Latency stats (24h, ignoring error rows since they often have 0ms)
        latencies = sorted(
            d.classifier_latency_ms for d in rows_24h
            if d.classifier_latency_ms is not None
            and d.classifier_label != "error"
        )
        latency_p50 = latencies[len(latencies) // 2] if latencies else None
        latency_p95 = (
            latencies[int(len(latencies) * 0.95)]
            if latencies else None
        )

        # Recent 50 decisions (most recent first for display)
        recent_rows = (
            db.query(CenaWakeDecision)
              .order_by(CenaWakeDecision.created_at.desc())
              .limit(50)
              .all()
        )
        recent_decisions = []
        for d in recent_rows:
            recent_decisions.append({
                "id": d.id,
                "created_at_ct": (
                    d.created_at.replace(tzinfo=timezone.utc)
                                .astimezone(CT).strftime("%H:%M:%S")
                    if d.created_at else "—"
                ),
                "created_at_date": (
                    d.created_at.replace(tzinfo=timezone.utc)
                                .astimezone(CT).strftime("%Y-%m-%d")
                    if d.created_at else "—"
                ),
                "author": d.author or "(unknown)",
                "snippet": (d.message_snippet or "")[:120],
                "label": d.classifier_label,
                "confidence": d.classifier_confidence,
                "reason": d.classifier_reason,
                "would_fire": d.would_fire,
                "did_fire": d.did_fire,
                "rule_trigger": d.actual_rule_trigger,
                "input_tokens": d.classifier_input_tokens,
                "output_tokens": d.classifier_output_tokens,
                "latency_ms": d.classifier_latency_ms,
                "shadow_mode": d.shadow_mode,
                "row_cost_usd": _cena_decision_cost_usd(d),
            })
    finally:
        db.close()

    return render_template(
        "developer_cena_stats.html",
        active="dev_cena_stats",
        total_24h=total_24h,
        cost_24h=cost_24h,
        label_counts_24h=label_counts_24h,
        author_breakdown=author_breakdown,
        would_fire_24h=would_fire_24h,
        did_fire_24h=did_fire_24h,
        false_negative_count=false_negative_count,
        false_positive_count=false_positive_count,
        error_count=error_count,
        latency_p50=latency_p50,
        latency_p95=latency_p95,
        recent_decisions=recent_decisions,
        haiku_input_price=_HAIKU_INPUT_PRICE_PER_MTOK,
        haiku_output_price=_HAIKU_OUTPUT_PRICE_PER_MTOK,
    )


@dev_chat.route("/partner/developer/ezcater")
@dev_chat.route("/partner/developer/ezcater/review")
@requires_permission("developer.view_chat")
def ezcater_review_queue():
    """Partner-only Ezcater review queue. Lists orders the auto-resolver
    couldn't auto-clear (Claude flagged at least one warning as real).
    Replaces the old per-store /review queue Sam retired."""
    gate = _enforce_partner()
    if gate is not None:
        return gate
    from datetime import datetime
    from app.db import get_db
    from app.models import Order
    db = next(get_db())
    try:
        today_iso = datetime.now().strftime("%Y-%m-%d")
        orders = (
            db.query(Order)
            .filter(Order.delivery_date >= today_iso)
            .filter(Order.status != "cancelled")
            .filter(Order.needs_review.is_(True))
            .order_by(Order.delivery_date.asc(), Order.deliver_at)
            .all()
        )
    finally:
        db.close()
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    return render_template(
        "ezcater_review_queue.html",
        active="dev_ezcater_review",
        page_title="Ezcater · Review Queue",
        orders=orders,
    )


# /partner/developer/app/* routes (download.zip, denials, app/<page>)
# and the SOURCE_PAGES mapping were removed per Sam directive #241
# (2026-05-23). The full subtree was backed up to
# aick:Desktop/docssamonly/ — recovery via that directory if ever
# needed. The denials VIEW is gone but the PermissionDenial table is
# untouched (rows continue to write from _log_denial()); a future
# replacement surface can read from there.


# ============================================================
# Dev chat TODO list (Sam #1066, 2026-05-26)
# ============================================================
# Sam adds items via the widget on /partner/developer/chat; each item
# can be assigned to a specific agent (aick / ck / cena) or left
# unassigned (any agent grabs it). The assigned agent reads the list
# when they refresh the page. Body fields:
#   title:       short text (required)
#   body:        optional details
#   assigned_to: one of aick / ck / cena, or null
#   status:      open / in_progress / done / cancelled

def _todo_render(t: "DevChatTodo") -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "body": t.body or "",
        "assigned_to": t.assigned_to,
        "status": t.status,
        "created_by": t.created_by,
        "created_at": (t.created_at.isoformat()
                       if t.created_at else None),
        "completed_at": (t.completed_at.isoformat()
                         if t.completed_at else None),
    }


@dev_chat.route("/partner/developer/chat/todos", methods=["GET"])
@requires_permission("developer.view_chat")
def dev_chat_todos_list():
    """List dev chat todos. Returns {open: [...], done: [...]} so the
    widget can render two sections. Open sorted newest-first; done
    sorted by completed_at desc."""
    db = SessionLocal()
    try:
        active = (db.query(DevChatTodo)
                  .filter(DevChatTodo.status.in_(
                      ("open", "in_progress")))
                  .order_by(DevChatTodo.created_at.desc(),
                            DevChatTodo.id.desc())
                  .all())
        done = (db.query(DevChatTodo)
                .filter(DevChatTodo.status.in_(("done", "cancelled")))
                .order_by(DevChatTodo.completed_at.desc().nullslast(),
                          DevChatTodo.id.desc())
                .limit(50)
                .all())
        return jsonify({
            "ok": True,
            "open": [_todo_render(t) for t in active],
            "done": [_todo_render(t) for t in done],
        })
    finally:
        db.close()


@dev_chat.route("/partner/developer/chat/todos", methods=["POST"])
@requires_permission("developer.view_chat")
def dev_chat_todos_add():
    """Add a new dev chat todo. Body: {title, body?, assigned_to?,
    created_by?}."""
    body_in = request.get_json(silent=True) or {}
    title = (body_in.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "title required"}), 400
    extra = (body_in.get("body") or "").strip() or None
    assigned_to = (body_in.get("assigned_to") or "").strip().lower() or None
    if assigned_to and assigned_to not in _VALID_DEV_CHAT_TODO_ASSIGNEES:
        return jsonify({"ok": False,
                        "error": f"assigned_to must be one of "
                                  f"{sorted(_VALID_DEV_CHAT_TODO_ASSIGNEES)} "
                                  f"or empty"}), 400
    created_by = (body_in.get("created_by") or "").strip()[:80] or None
    db = SessionLocal()
    try:
        row = DevChatTodo(
            title=title[:500],
            body=extra,
            assigned_to=assigned_to,
            status="open",
            created_by=created_by,
        )
        db.add(row)
        db.flush()
        # ONLY post a chat-message ping when an agent is explicitly
        # assigned. Unassigned ("any") todos don't auto-post — Sam
        # asked at #1071 to avoid that spam path. Sam #1066 follow-up.
        if assigned_to:
            author_label = created_by or "someone"
            body_msg = (f"📌 @{assigned_to}-claude — {author_label} "
                        f"assigned you TODO #{row.id}: {title[:280]}")
            db.add(DeveloperChatMessage(author="system", body=body_msg))
        db.commit()
        db.refresh(row)
        return jsonify({"ok": True, "todo": _todo_render(row)}), 201
    finally:
        db.close()


@dev_chat.route("/partner/developer/chat/todos/<int:tid>", methods=["PATCH"])
@requires_permission("developer.view_chat")
def dev_chat_todos_edit(tid: int):
    """Edit a todo. Body fields are all optional. Setting status to
    'done' or 'cancelled' stamps completed_at; setting back to 'open'/
    'in_progress' clears it."""
    body_in = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        row = db.get(DevChatTodo, tid)
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        if "title" in body_in:
            new_title = (body_in.get("title") or "").strip()
            if not new_title:
                return jsonify({"ok": False,
                                "error": "title cannot be empty"}), 400
            row.title = new_title[:500]
        if "body" in body_in:
            row.body = (body_in.get("body") or "").strip() or None
        if "assigned_to" in body_in:
            new_assignee = (body_in.get("assigned_to") or "").strip().lower() or None
            if new_assignee and new_assignee not in _VALID_DEV_CHAT_TODO_ASSIGNEES:
                return jsonify({"ok": False,
                                "error": f"assigned_to must be one of "
                                          f"{sorted(_VALID_DEV_CHAT_TODO_ASSIGNEES)} "
                                          f"or empty"}), 400
            row.assigned_to = new_assignee
        if "status" in body_in:
            new_status = (body_in.get("status") or "").strip().lower()
            if new_status not in _VALID_DEV_CHAT_TODO_STATUS:
                return jsonify({"ok": False,
                                "error": f"status must be one of "
                                          f"{sorted(_VALID_DEV_CHAT_TODO_STATUS)}"}), 400
            row.status = new_status
            if new_status in ("done", "cancelled"):
                if row.completed_at is None:
                    row.completed_at = datetime.utcnow()
            else:
                row.completed_at = None
        db.commit()
        db.refresh(row)
        return jsonify({"ok": True, "todo": _todo_render(row)})
    finally:
        db.close()


@dev_chat.route("/partner/developer/chat/todos/<int:tid>", methods=["DELETE"])
@requires_permission("developer.view_chat")
def dev_chat_todos_delete(tid: int):
    db = SessionLocal()
    try:
        row = db.get(DevChatTodo, tid)
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        db.delete(row)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────
# Permissions page (Sam #1676) - PARTNER-ONLY page shell + catalog provider.
# The roster / load / save API lives in app/web/permissions_admin.py. Binds
# to the authoritative catalog in app/services/permission_catalog.py. ezCater
# driver perms are coded-locked + NOT represented here (Sam #1676).
# ──────────────────────────────────────────────────────────────────────
def _build_permissions_catalog():
    """Flatten the category-grouped CATALOG (app/services/permission_catalog.py)
    into the flat permissions[] the template consumes, lifting category ->
    {num,name}. ROLES, STORES + the per-perm `sensitive` flag pass through as
    the locked contract (ck #1680). Never 500s: returns an explicit empty
    error catalog (no fake data) if the module fails to import."""
    try:
        from app.services import permission_catalog as pc
    except Exception:
        logging.getLogger(__name__).exception("permission_catalog import failed")
        return {"roles": [], "stores": [], "permissions": [],
                "is_placeholder": True, "catalog_source": "error"}

    permissions = []
    for group in pc.CATALOG:
        cat = {"num": group.get("id"), "name": group.get("name")}
        for perm in group.get("perms", []):
            permissions.append({
                "id": perm.get("id"),
                "key": perm.get("key"),
                "category": cat,
                "label": perm.get("label"),
                "notes": perm.get("notes", ""),
                "maps_to": perm.get("maps_to", {}),
                "status": perm.get("status", "live"),
                "default_roles": list(perm.get("default_roles", [])),
                "sensitive": bool(perm.get("sensitive")),
            })

    return {
        "roles": [dict(r) for r in pc.ROLES],
        "stores": [dict(s) for s in pc.STORES],
        "permissions": permissions,
        "is_placeholder": False,
        "catalog_source": "permission_catalog",
    }


@dev_chat.route("/partner/developer/permissions", methods=["GET"])
@requires_permission("developer.manage_permissions")
def permissions_page():
    # PARTNER-ONLY (Sam #1694). developer.manage_permissions is held by NO
    # explicit role set, so only partner clears it (the {"*"} wildcard). The
    # partner-User checks below are belt-and-suspenders.
    gate = _enforce_partner()
    if gate is not None:
        return gate
    # A password-only Tier-2 session clears the tag, but the roster/load/save
    # APIs require a real partner User row - deny cleanly so the grid never
    # renders then 403s on every AJAX call.
    u = getattr(g, "current_user", None)
    if not (u is not None and getattr(u, "permission_level", None) == "partner"):
        return redirect(url_for("auth.access_denied",
                                need="developer.manage_permissions",
                                next=request.path))
    # Synthesize per-store sidebar context (this URL doesn't pass through the
    # <store> prefix preprocessor).
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    return render_template(
        "developer_permissions.html",
        active="dev_permissions",
        page_title="Permissions",
        catalog=_build_permissions_catalog(),
    )
