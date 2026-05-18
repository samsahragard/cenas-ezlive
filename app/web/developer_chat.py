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
from app.models import DeveloperChatMessage, DeveloperChatAttachment, PermissionDenial
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
        title="Drivers Page Redesign",
        version="v2",
        date="2026-05-17",
        description="Active/Inactive tab filter, mobile-friendly hamburger inside topbar, 48px touch targets, breakpoint unified to 1024px.",
        url="/static/mockups/drivers_redesign.html",
        type="mockup",
    ),
    dict(
        title="Legal — Overview",
        version=None,
        date="2026-05-16",
        description="Reference implementation of the V2 .lg-* pattern: glass cards, gold uppercase section labels, --ck-ease motion. Top-level legal subsystem entry.",
        url="/partner/legal",
        type="reference",
    ),
    dict(
        title="Legal — Matters List",
        version=None,
        date="2026-05-16",
        description="V2 list pattern with .lg-grid-stats responsive 4→1 at 1024px. Reference for CRUD-list surfaces under V2.",
        url="/partner/legal/matters",
        type="reference",
    ),
    dict(
        title="Legal — Matter Detail",
        version=None,
        date="2026-05-16",
        description="V2 detail-page pattern. Reference for record-detail surfaces.",
        url="/partner/legal/matters/1",
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


# ============== App Docs (Partner-only) ==============
# Read-only documentation served from Jinja templates. NO secrets in any of
# these templates — tokens, passwords, API keys are referenced by env-var
# name or by their secrets-file path, never by value. Updates only via Sam-
# approved git commits; no edit UI.
DOC_PAGES = [
    ("session-start",            "Session Start",     "doc_session_start"),
    ("session-closeout",         "Session Closeout",  "doc_session_closeout"),
    ("operations-reference",     "Operations Reference", "doc_operations_reference"),
    ("site-map",                 "Site Map",          "doc_site_map"),
    ("site-code",                "Site Code",         "doc_site_code"),
    ("architecture-diagrams",    "Architecture Diagrams", "doc_architecture_diagrams"),
    ("arc-code",                 "Arc Code",          "doc_arc_code"),
    ("node-link-diagram",        "Node Link Diagram", "doc_node_link_diagram"),
    ("node-map",                 "Node Map",          "doc_node_map"),
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
    ("permission-system",        "Permission System", "doc_permission_system"),
    ("anomaly-rules",            "Anomaly Rules",     "doc_anomaly_rules"),
    ("anomaly-service-spec",     "Anomaly Service Spec", "doc_anomaly_service_spec"),
    ("morning-brief-composer-spec", "Morning Brief Composer Spec", "doc_morning_brief_composer_spec"),
    ("brief-calibration-runbook", "Brief Calibration Runbook", "doc_brief_calibration_runbook"),
    ("phase-2-directive",        "Phase 2 Directive", "doc_phase_2_directive"),
    ("block-1-precond-role-taxonomy-spec", "Block 1 Precond - Role Taxonomy", "doc_block_1_precond_role_taxonomy_spec"),
    ("block-1-precond-scheduled-event-spec", "Block 1 Precond - ScheduledEvent Model", "doc_block_1_precond_scheduled_event_spec"),
    ("block-1a-task-system-spec", "Block 1A - Task System Spec", "doc_block_1a_task_system_spec"),
    ("block-1b-ribbon-component-spec", "Block 1B - Ribbon Component Spec", "doc_block_1b_ribbon_component_spec"),
    ("block-1c-ribbon-router-spec", "Block 1C - Ribbon Content Router Spec", "doc_block_1c_ribbon_router_spec"),
    ("block-1f-sales-insights-spec", "Block 1F - Sales Insights Spec", "doc_block_1f_sales_insights_spec"),
    ("block-1g-team-tab-spec",    "Block 1G - Team Tab Spec", "doc_block_1g_team_tab_spec"),
    ("block-1h-pay-masking-spec", "Block 1H - Pay Masking Spec", "doc_block_1h_pay_masking_spec"),
    ("block-1j-ambient-signal-spec", "Block 1J - AmbientSignal Refactor", "doc_block_1j_ambient_signal_spec"),
    ("block-2i-recipe-page-spec", "Block 2I - Recipe Page Spec", "doc_block_2i_recipe_page_spec"),
    ("handoff-aick-2026-05-14",  "Aick Handoff — 2026-05-14",  "doc_handoff_aick_2026_05_14"),
    ("handoff-ck-2026-05-14",    "ck Handoff — 2026-05-14",    "doc_handoff_ck_2026_05_14"),
    ("handoff-samai-2026-05-14", "samai Handoff — 2026-05-14", "doc_handoff_samai_2026_05_14"),
    ("cena-operational-spec",    "Cena — Operational Spec",     "doc_cena_operational_spec"),
    ("design-system-reference",   "Design System Reference",     "doc_design_system_reference"),
    ("system-inventory",          "System Inventory",            "doc_system_inventory"),
    ("methodology-rules",         "Methodology Rules",           "doc_methodology_rules"),
    ("dev-section-organization",  "Dev Section — Start Here",    "doc_dev_section_organization"),
    ("denials",                  "Permission Denials", "doc_denials"),
    ("chats",                    "Chats",             "doc_chats"),
    ("spec-dev-samples-page",    "Spec: Dev Samples Page", "doc_spec_dev_samples_page"),
    ("spec-drivers-redesign",     "Spec: Drivers Redesign",  "doc_spec_drivers_redesign"),
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
    ("ck-session-2026-05-13",    "ck — 5/13",    "doc_ck_session_2026_05_13"),
    ("aick-session-2026-05-13",  "aick — 5/13",  "doc_aick_session_2026_05_13"),
    ("samai-session-2026-05-13", "samai — 5/13", "doc_samai_session_2026_05_13"),
]


@dev_chat.route("/partner/developer/samples")
@requires_permission("developer.view_chat")
def developer_samples():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    return render_template(
        "developer_samples.html",
        active="dev_samples",
        samples=SAMPLES,
        doc_pages=DOC_PAGES,
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


@dev_chat.route("/partner/developer/app/download.zip", methods=["GET"])
@requires_permission("developer.view_app_docs")
def app_doc_download():
    """Stream a fresh zip of every file under app/templates/docs/ so Sam can
    download the whole Developer → App docs section in one click. Built
    in-memory at request time, so what you download always matches what's
    currently in git/on disk."""
    gate = _enforce_partner()
    if gate is not None:
        return gate
    import io, zipfile
    from datetime import datetime
    from pathlib import Path
    docs_dir = Path(current_app.root_path) / "templates" / "docs"
    if not docs_dir.exists():
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(docs_dir.rglob("*")):
            if f.is_file():
                # arcname relative to docs/ so the zip unpacks as "docs/...".
                rel = f.relative_to(docs_dir.parent)  # → "docs/foo.html"
                zf.write(f, arcname=str(rel).replace("\\", "/"))
    buf.seek(0)
    stamp = datetime.now().strftime("%Y-%m-%d")
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"developer_docs_{stamp}.zip",
        max_age=0,
    )


# Permission denials surface (samai spec §5.3). Has its own route — not
# a static doc template — so it can render live rows from the
# PermissionDenial table populated by _log_denial(). Registered BEFORE the
# `/<page>` catch-all so Flask routes the exact match first.
@dev_chat.route("/partner/developer/app/denials")
@requires_permission("developer.view_app_docs")
def denials_page():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    db = SessionLocal()
    try:
        # Latest 500 denials, newest first. Older rows still in the table
        # but not surfaced — partners use the page for triage, not history.
        rows = (db.query(PermissionDenial)
                  .order_by(PermissionDenial.created_at.desc())
                  .limit(500)
                  .all())
        total = db.query(PermissionDenial).count()
    finally:
        db.close()
    return render_template(
        "docs/denials.html",
        active="doc_denials",
        page_title="Permission Denials",
        rows=rows,
        total=total,
        doc_pages=DOC_PAGES,
        chat_pages=CHAT_PAGES,
        current_doc_slug="denials",
    )


# Source-view pages: each entry mirrors one of the visual doc pages above
# but renders its raw HTML/Mermaid/JS source so Sam can read or copy the
# code without view-source-ing the rendered page. The source is read from
# disk at request time, so it always matches the live template (no manual
# snapshot to keep in sync).
SOURCE_PAGES = {
    # url-slug:         (mirrored-doc-slug,       label-on-rendered-link)
    "arc-code":          ("architecture-diagrams", "Architecture Diagrams"),
    "site-code":         ("site-map",              "Site Map"),
    "node-map":          ("node-link-diagram",     "Node Link Diagram"),
}


@dev_chat.route("/partner/developer/app")
@dev_chat.route("/partner/developer/app/<page>")
@requires_permission("developer.view_app_docs")
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

    # Source-view pages share a single template; the underlying file is read
    # from disk at request time so the rendered code matches the live page.
    if slug in SOURCE_PAGES:
        mirrored_slug, mirrored_label = SOURCE_PAGES[slug]
        from pathlib import Path
        source_filename = f"docs/{mirrored_slug.replace('-', '_')}.html"
        source_path = Path(current_app.root_path) / "templates" / source_filename
        try:
            source_text = source_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            abort(404)
        return render_template(
            "docs/_source_view.html",
            active=active_key,
            page_title=label,
            mirrored_slug=mirrored_slug,
            mirrored_label=mirrored_label,
            source_filename=source_filename,
            source_text=source_text,
            source_len=len(source_text),
            doc_pages=DOC_PAGES,
            chat_pages=CHAT_PAGES,
            current_doc_slug=slug,
        )

    template_name = f"docs/{slug.replace('-', '_')}.html"
    return render_template(
        template_name,
        active=active_key,
        page_title=label,
        doc_pages=DOC_PAGES,
        chat_pages=CHAT_PAGES,
        current_doc_slug=slug,
    )
