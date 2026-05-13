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
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, abort, g, send_file
from werkzeug.utils import secure_filename

from app.db import SessionLocal
from app.models import DeveloperChatMessage, DeveloperChatAttachment

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


@dev_chat.route("/partner/developer/chat", methods=["GET"])
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
def post_message():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    author = (request.form.get("author") or "sam").strip()[:60]
    body = (request.form.get("body") or "").strip()

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
    finally:
        db.close()
    return redirect(url_for("developer_chat.chat_page") + "#bottom")


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
    is_ai = (a_low == "samai") or ("aick" in a_low) or (a_low == "ck") or (a_low == "ck-claude")
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
    """Adds CSS class hints based on author for color coding."""
    d = _msg_to_dict(m)
    a = (m.author or "").lower()
    if a == "sam":
        d["css_class"] = "msg-sam"
    elif a == "masood":
        d["css_class"] = "msg-masood"
    elif a == "samai":
        d["css_class"] = "msg-samai"
    elif "aick" in a:
        d["css_class"] = "msg-aick"
    elif "ck" in a:
        d["css_class"] = "msg-ck"
    elif a == "system":
        d["css_class"] = "msg-system"
    elif "claude" in a:
        d["css_class"] = "msg-aick"
    else:
        d["css_class"] = "msg-other"
    return d


# ============== App Docs (Partner-only) ==============
# Read-only documentation served from Jinja templates. NO secrets in any of
# these templates — tokens, passwords, API keys are referenced by env-var
# name or by their secrets-file path, never by value. Updates only via Sam-
# approved git commits; no edit UI.
DOC_PAGES = [
    ("session-start",            "Session Start",     "doc_session_start"),
    ("session-closeout",         "Session Closeout",  "doc_session_closeout"),
    ("site-map",                 "Site Map",          "doc_site_map"),
    ("readme",                   "README",            "doc_readme"),
    ("architecture",             "Architecture",      "doc_architecture"),
    ("features",                 "Features",          "doc_features"),
    ("tech-stack",               "Tech Stack",        "doc_tech_stack"),
    ("deployment",               "Deployment",        "doc_deployment"),
    ("data-sources",             "Data Sources",      "doc_data_sources"),
    ("ezcater-guidelines",       "ezCater Guidelines", "doc_ezcater_guidelines"),
    ("toast-api-reference",      "Toast API Reference", "doc_toast_api_reference"),
    ("toast-analytics-api",      "Toast Analytics API", "doc_toast_analytics_api"),
    ("agent-bootstrap",          "Agent Bootstrap",   "doc_agent_bootstrap"),
    ("chats",                    "Chats",             "doc_chats"),
]

# Per-session chat handoff docs. Lives in its own list (not DOC_PAGES) so the
# top doc nav stays short — the "Chats" entry in DOC_PAGES is the index page
# that lists all of these. To add a new chat: create
# app/templates/docs/<slug>.html and append a tuple here. The /partner/developer/app/<slug>
# route resolves from both lists, so direct links keep working.
CHAT_PAGES = [
    ("ck-session-2026-05-10",    "ck — 5/10",    "doc_ck_session_2026_05_10"),
    ("aick-session-2026-05-10",  "aick — 5/10",  "doc_aick_session_2026_05_10"),
    ("ck-session-2026-05-11",    "ck — 5/11",    "doc_ck_session_2026_05_11"),
    ("aick-session-2026-05-11",  "aick — 5/11",  "doc_aick_session_2026_05_11"),
    ("samai-session-2026-05-11", "samai — 5/11", "doc_samai_session_2026_05_11"),
    ("ck-session-2026-05-12",    "ck — 5/12",    "doc_ck_session_2026_05_12"),
    ("aick-session-2026-05-12",  "aick — 5/12",  "doc_aick_session_2026_05_12"),
    ("samai-session-2026-05-12", "samai — 5/12", "doc_samai_session_2026_05_12"),
]


@dev_chat.route("/partner/developer/ezcater")
@dev_chat.route("/partner/developer/ezcater/review")
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


@dev_chat.route("/partner/developer/app")
@dev_chat.route("/partner/developer/app/<page>")
def app_doc(page: str = "readme"):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    # Resolve from DOC_PAGES first, then CHAT_PAGES — direct chat slug links
    # (e.g. /partner/developer/app/aick-session-2026-05-10) keep working.
    page_meta = next(((slug, label, active_key) for slug, label, active_key in DOC_PAGES + CHAT_PAGES
                      if slug == page), None)
    if page_meta is None:
        abort(404)
    slug, label, active_key = page_meta
    # Set partner context for the sidebar
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    template_name = f"docs/{slug.replace('-', '_')}.html"
    return render_template(
        template_name,
        active=active_key,
        page_title=label,
        doc_pages=DOC_PAGES,
        chat_pages=CHAT_PAGES,
        current_doc_slug=slug,
    )
