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
        slug="drivers-redesign-v2",
        title="Drivers Page Redesign",
        version="v2",
        date="2026-05-17",
        description="Active/Inactive tab filter, mobile-friendly hamburger inside topbar, 48px touch targets, breakpoint unified to 1024px.",
        url="/static/mockups/drivers_redesign.html",
        type="mockup",
    ),
    dict(
        slug="right-sidebar-plan-v1",
        title="Right-Side Menu Bar — Plan Status",
        version="v1",
        date="2026-05-17",
        description="Right-side navigation panel sourced from plan.md §5 surfaces. Per-section completion counts, status legend (live/soon/planned), 'new' pills on last-24h ships. V2 .ck-sb-* token reuse — sibling-not-foreign to the existing left sidebar.",
        url="/static/mockups/right_sidebar_plan_status.html",
        type="mockup",
    ),
    dict(
        slug="right-sidebar-v3",
        title="Right-Side Roadmap — Implementation-ready Component",
        version="v3",
        date="2026-05-18",
        description="Component-in-isolation desktop view (280px), mobile chip-toggle + drawer-slide-in-from-right at <=720px, interactive states demo (hover/collapse-animation/current-phase pulse). .ck-rsb-* class namespace parallel to .ck-sb-*. Production integration spec in notes block: base_dashboard.html include, ribbon-collapse endpoint reuse, plan.md-at-request-time data source. Per cena #2698 — v3 mockup awaiting structural review then Sam approval before ck implementation.",
        url="/static/mockups/right_sidebar_v3.html",
        type="mockup",
    ),
    dict(
        slug="notifications-page-v1",
        title="Notifications page (replaces ribbon)",
        version="v1",
        date="2026-05-17",
        description="Tabbed Notifications page replacing the inline ribbon. Same 7 categories (To-do / Caterings / Events / Employee / Vendors / Maintenance / Sales) + severity tokens, restructured as V2 .lg-nav pill tabs + glass cards with X/check actions. Per cena #2569 — dck mockup awaiting Sam approval before ck implementation.",
        url="/static/mockups/notifications_page.html",
        type="mockup",
    ),
    dict(
        slug="produce-mobile-redesign-v1",
        title="Produce Mobile Redesign",
        version="v1",
        date="2026-05-17",
        description="Mobile-first Produce order-guide rework: card list <640px (qty stepper 44x44pt + live line subtotal), overflow-fixed table >=640px (sticky ITEM column + scroll-fade), sticky bottom bar collapses vendor trackers once any qty > 0. Per Sam #2607 spec — dck mockup awaiting Sam approval before ck implementation.",
        url="/static/mockups/produce_mobile_redesign.html",
        type="mockup",
    ),
    dict(
        slug="research-01-compare-chip",
        title="Research 01 — Uber Eats Compare Chip",
        version="v1",
        date="2026-05-18",
        description="Uber Eats Manager pattern: every KPI carries a 'vs. last [period]' delta chip with a global range selector at the top. Adapt to per-store dashboards so operators see deltas at a glance instead of raw numbers in isolation.",
        url="/static/mockups/research_01_compare_chip.html",
        type="mockup",
    ),
    dict(
        slug="research-02-ticket-age-color",
        title="Research 02 — Toast KDS Age Coloring",
        version="v1",
        date="2026-05-18",
        description="Toast KDS 4-tier age coloring (fresh / aware / warn / late) with a pulse animation on late tickets. Adapt to EzCater pipeline + driver dispatch queues + produce cart so stale items grab the eye pre-attentively.",
        url="/static/mockups/research_02_ticket_age_color.html",
        type="mockup",
    ),
    dict(
        slug="research-03-dispatch-map-list",
        title="Research 03 — DoorDash Drive Map+List",
        version="v1",
        date="2026-05-18",
        description="DoorDash Drive dual-pane map + list for driver dispatch. Mobile defaults to list with a chip toggle for map. Adapt to /drivers-live + dispatch surfaces where spatial layout helps.",
        url="/static/mockups/research_03_dispatch_map_list.html",
        type="mockup",
    ),
    dict(
        slug="research-04-needs-you-strip",
        title="Research 04 — 7shifts Needs-You Strip",
        version="v1",
        date="2026-05-18",
        description="7shifts pattern: typed pending-action chips at top of dashboard ('Driver no-show: 1 / EzCater unconfirmed: 3'). Each chip deep-links to the action surface. Distinct from /partner/notifications inbox — it's a 'do this now' bar, not a feed.",
        url="/static/mockups/research_04_needs_you_strip.html",
        type="mockup",
    ),
    dict(
        slug="research-05-whos-working",
        title="Research 05 — 7shifts Who's Working",
        version="v1",
        date="2026-05-18",
        description="7shifts 5-bucket Right-Now strip (On / Idle / Break / Late / Off) for drivers, future-extensible to FOH/BOH. Drill-down inline; replaces 'who's actually here right now' as a calculation operators currently do in their heads.",
        url="/static/mockups/research_05_whos_working.html",
        type="mockup",
    ),
    dict(
        slug="research-06-channel-filter-chip",
        title="Research 06 — Olo Channel Filter Chips",
        version="v1",
        date="2026-05-18",
        description="Olo single-pane order view with channel chips (EzCater / Direct / Phone / DoorDash / UberEats). Forward-compatible even when only EzCater is active — chip just renders 0 for unused channels. Avoids per-channel page proliferation.",
        url="/static/mockups/research_06_channel_filter_chip.html",
        type="mockup",
    ),
    dict(
        slug="research-07-location-toggle",
        title="Research 07 — Uber Eats Location Toggle",
        version="v1",
        date="2026-05-18",
        description="Uber Eats Manager persistent Tomball / Copperfield / Both toggle in the top bar that scopes everything app-wide. Sub-split numbers in the Both view show per-store breakdowns inline. Replaces the current per-store URL routing as the primary scope mechanism.",
        url="/static/mockups/research_07_location_toggle.html",
        type="mockup",
    ),
    dict(
        slug="research-08-reorderable-cards",
        title="Research 08 — Square Reorderable Dashboard",
        version="v1",
        date="2026-05-18",
        description="Square per-manager dashboard card reordering via up/down arrows + hide/show controls. Different roles get different defaults; users customize cheaply without admin involvement. Stored per-user; resets cleanly to role default.",
        url="/static/mockups/research_08_reorderable_cards.html",
        type="mockup",
    ),
    dict(
        slug="research-09-text-size-density",
        title="Research 09 — ChowNow Density Controls",
        version="v1",
        date="2026-05-18",
        description="ChowNow top-level a11y/density user settings: Compact / Regular / Large + High contrast + Reduce motion + Bigger tap targets. CSS-variable scaled — single token drives all sizes across the app. Surfaces accessibility without per-page work.",
        url="/static/mockups/research_09_text_size_density.html",
        type="mockup",
    ),
    dict(
        slug="research-10-flash-on-change",
        title="Research 10 — Toast KDS Flash-on-Change",
        version="v1",
        date="2026-05-18",
        description="Toast KDS brief one-shot flash animation when data changes (NEW gold burst / CHANGED gold soft / URGENT red burst). Pre-attentive signal of 'something just moved' tied to the polling layer's row-delta detection.",
        url="/static/mockups/research_10_flash_on_change.html",
        type="mockup",
    ),
    dict(
        slug="manager-pages-shell-v1",
        title="Manager Pages Shared Shell",
        version="v1",
        date="2026-05-18",
        description="Shared shell design for the 14 manager pages (Daily Manager Log, Shift Handoff, Incident Reports, Supply Requests, Daily Goals, Staff Feedback, Pre-shift Checklist, Close-of-day Audit, Recipe Page, Attendance Tracking, Interview Surface, Training Records, Maintenance Requests, Employee Counseling). Same audience gate for all 14: GM / KM / Asst KM / FOH manager (read-write own store), Partner/Corporate (read all). Page header with access pill + New entry CTA, filter row (search + time-range + author-scope chips), 2-col body (list left + detail/form right with click-to-load + mode-flip), color-coded type tags where useful (e.g. Incident Reports: Injury/Theft/Complaint), mobile collapse at \u2264760px. Two example pages applied: Daily Manager Log (no type tags) + Incident Reports (with type tags). Approach A text-heavy v1 per Sam direction \u2014 every page = title + free-text body + auto-stamped author + date.",
        url="/static/mockups/manager_pages_shell_v1.html",
        type="mockup",
    ),
        dict(
        slug="in-house-catering-quote-builder-v1",
        title="In-House Catering Quote Builder",
        version="v1",
        date="2026-05-18",
        description="Full page chrome (sidebar + breadcrumbs + location toggle) for the staff-tool In-House Catering Quote Builder. 3-col desktop layout (220px category nav | flex item grid | 340px cart drawer). All item prices default to $0 with ezCater reference shown as ghost. Modifier modal opens with prominent gold-bordered custom-price input + qty stepper + required-group validation + per-item special instructions. Tray/Individual Packaging omitted per Sam #910. Cart with line items + subtotal + customer form + 3 CTAs locked hierarchy (Quote primary blue / Pay Now secondary green / Pay Later disabled gray with PCI-pending tooltip). Mobile collapse to single-col + bottom-bar 'View cart' opening drawer. Aligns with ck production plan #1049.",
        url="/static/mockups/in_house_catering.html",
        type="mockup",
    ),
    dict(
        slug="legal-overview",
        title="Legal — Overview",
        version=None,
        date="2026-05-16",
        description="Reference implementation of the V2 .lg-* pattern: glass cards, gold uppercase section labels, --ck-ease motion. Top-level legal subsystem entry.",
        url="/partner/legal",
        type="reference",
    ),
    dict(
        slug="legal-matters-list",
        title="Legal — Matters List",
        version=None,
        date="2026-05-16",
        description="V2 list pattern with .lg-grid-stats responsive 4→1 at 1024px. Reference for CRUD-list surfaces under V2.",
        url="/partner/legal/matters",
        type="reference",
    ),
    dict(
        slug="legal-matter-detail",
        title="Legal — Matter Detail",
        version=None,
        date="2026-05-16",
        description="V2 detail-page pattern. Reference for record-detail surfaces.",
        url="/partner/legal/matters/1",
        type="reference",
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
    ("spec-samples-approval-workflow", "Spec: Samples Approval Workflow", "doc_spec_samples_approval_workflow"),
    ("spec-produce-mobile-redesign", "Spec: Produce Mobile Redesign", "doc_spec_produce_mobile_redesign"),
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
    ("ck-session-2026-05-19",    "ck — 5/19",    "doc_ck_session_2026_05_19"),
    ("aick-session-2026-05-19",  "aick — 5/19",  "doc_aick_session_2026_05_19"),
    ("samai-session-2026-05-19", "samai — 5/19", "doc_samai_session_2026_05_19"),
]


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
        doc_pages=DOC_PAGES,
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
        doc_pages=DOC_PAGES,
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
