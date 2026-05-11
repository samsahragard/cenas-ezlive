"""Partner-only mirror of ock's WhatsApp inbox.

Two endpoints:

- POST /api/inbox/whatsapp        — Bearer-token ingest (called by the
                                    daemon on the CK Mini PC; takes new
                                    WhatsApp messages from awareness.db
                                    and lands them in the EZLive copy).
- GET  /partner/operations/whatsapp — Partner-gated UI. Conversation
                                      list on the left, thread view on
                                      the right, auto-refresh every 5s.

Phase 2 will add /api/whatsapp/send for outbound replies routed back
through ock via cloudflared. Until then this is read-only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from sqlalchemy import func, desc

from app.db import SessionLocal
from app.models import WhatsAppMessage


ck_whatsapp_bp = Blueprint("ck_whatsapp", __name__)


_INGEST_TOKEN_FILE = Path("/var/data/secrets/ck_whatsapp_ingest_token.txt")


def _ingest_token() -> str:
    """File first, env fallback (matches the pattern used by other secrets)."""
    if _INGEST_TOKEN_FILE.exists():
        return _INGEST_TOKEN_FILE.read_text(encoding="utf-8").strip()
    return os.getenv("CK_WHATSAPP_INGEST_TOKEN", "").strip()


def _enforce_partner():
    """Partner-only gate — same shape as developer_chat._enforce_partner.
    Will become a one-line decorator swap after aick's role-based auth
    redesign (migration 13) lands.
    """
    if not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login"))
    return None


@ck_whatsapp_bp.route("/partner/operations/whatsapp", methods=["GET"])
def whatsapp_inbox():
    gate = _enforce_partner()
    if gate is not None:
        return gate

    db = SessionLocal()
    try:
        # Latest message per chat_id, ordered by recency. Limit at 60
        # conversations on the sidebar so the UI doesn't drown if the
        # mirror grows.
        sub = (
            db.query(
                WhatsAppMessage.chat_id.label("chat_id"),
                func.max(WhatsAppMessage.ts).label("latest_ts"),
            )
            .group_by(WhatsAppMessage.chat_id)
            .subquery()
        )
        latest_per_chat = (
            db.query(WhatsAppMessage)
            .join(
                sub,
                (WhatsAppMessage.chat_id == sub.c.chat_id)
                & (WhatsAppMessage.ts == sub.c.latest_ts),
            )
            .order_by(desc(sub.c.latest_ts))
            .limit(60)
            .all()
        )

        active_chat_id = request.args.get("chat") or (
            latest_per_chat[0].chat_id if latest_per_chat else None
        )

        thread = []
        if active_chat_id:
            thread = (
                db.query(WhatsAppMessage)
                .filter(WhatsAppMessage.chat_id == active_chat_id)
                .order_by(WhatsAppMessage.ts.asc())
                .limit(500)
                .all()
            )
    finally:
        db.close()

    return render_template(
        "ck_whatsapp.html",
        chats=latest_per_chat,
        active_chat_id=active_chat_id,
        thread=thread,
    )


@ck_whatsapp_bp.route("/api/inbox/whatsapp", methods=["POST"])
def whatsapp_ingest():
    """Bearer-token gated ingest from the CK daemon.

    Body: JSON array of message dicts (or a single dict). Each dict:
        external_id (required, unique — dedupe key)
        ts          (required, ISO8601)
        chat_id     (required)
        chat_type   (required)
        chat_name   (optional)
        sender_id   (required)
        sender_name (optional)
        body        (optional, may be empty for media-only)
        media_kind  (optional)
        direction   (optional, default 'inbound')
        sent_by_user (optional, only meaningful for direction='outbound')
        reply_to_external_id (optional)
        raw_metadata (optional dict)
    """
    tok = _ingest_token()
    if not tok:
        return jsonify({"error": "ingest token not configured"}), 503
    auth = (request.headers.get("Authorization") or "").strip()
    if auth != f"Bearer {tok}":
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return jsonify({"error": "json body required"}), 400
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return jsonify({"error": "expected JSON array or single object"}), 400

    now_iso = datetime.now(timezone.utc).isoformat()
    inserted = 0
    skipped = 0
    db = SessionLocal()
    try:
        for m in payload:
            ext = (m.get("external_id") or "").strip()
            if not ext:
                skipped += 1
                continue
            exists = (
                db.query(WhatsAppMessage.id)
                .filter(WhatsAppMessage.external_id == ext)
                .first()
            )
            if exists:
                skipped += 1
                continue
            row = WhatsAppMessage(
                external_id=ext,
                ts=m["ts"],
                chat_id=m["chat_id"],
                chat_type=m["chat_type"],
                chat_name=m.get("chat_name"),
                sender_id=m["sender_id"],
                sender_name=m.get("sender_name"),
                body=m.get("body"),
                media_kind=m.get("media_kind"),
                direction=m.get("direction", "inbound"),
                sent_by_user=m.get("sent_by_user"),
                reply_to_external_id=m.get("reply_to_external_id"),
                raw_metadata=json.dumps(m.get("raw_metadata") or {}),
                ingested_at=now_iso,
            )
            db.add(row)
            inserted += 1
        db.commit()
    finally:
        db.close()

    return jsonify({"inserted": inserted, "skipped": skipped})
