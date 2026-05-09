"""Developer chat — Partner-only.

Persistent thread for Sam + AI agents (this Claude, CK Claude, future)
to coordinate. Lives behind the existing `/partner/` partner_auth gate
(see store_routes._partner_gate) so anyone reading or posting must
already be past the Partner password.

Routes:
    GET  /partner/developer/chat                 — chat UI (auto-polls)
    POST /partner/developer/chat/post            — submit a new message
    GET  /partner/developer/chat/messages.json   — JSON poll feed (?since_id=N)

The JSON endpoint is what makes it scriptable: a Claude running on AiCk
or CK can `curl` it (with the partner cookie) and stream new messages
into a local log file.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, abort, g

from app.db import SessionLocal
from app.models import DeveloperChatMessage

log = logging.getLogger(__name__)

dev_chat = Blueprint("developer_chat", __name__)

CT = timezone(timedelta(hours=-5))   # Central time for display


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
    )


@dev_chat.route("/partner/developer/chat/post", methods=["POST"])
def post_message():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    author = (request.form.get("author") or "sam").strip()[:60]
    body = (request.form.get("body") or "").strip()
    if not body:
        return redirect(url_for("developer_chat.chat_page"))
    db = SessionLocal()
    try:
        m = DeveloperChatMessage(author=author, body=body)
        db.add(m)
        db.commit()
        log.info("dev-chat post: %s wrote %d chars", author, len(body))
    finally:
        db.close()
    return redirect(url_for("developer_chat.chat_page") + "#bottom")


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
    return {
        "id": m.id,
        "author": m.author,
        "body": m.body,
        "created_at_iso": m.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "created_at_display": m.created_at.replace(tzinfo=timezone.utc).astimezone(CT).strftime("%a %b %d, %I:%M %p"),
    }


def _render_msg(m: DeveloperChatMessage) -> dict:
    """Adds CSS class hints based on author for color coding."""
    d = _msg_to_dict(m)
    a = (m.author or "").lower()
    if a == "sam":
        d["css_class"] = "msg-sam"
    elif "claude" in a or "aick" in a:
        d["css_class"] = "msg-aick"
    elif "ck" in a:
        d["css_class"] = "msg-ck"
    elif a == "system":
        d["css_class"] = "msg-system"
    else:
        d["css_class"] = "msg-other"
    return d
