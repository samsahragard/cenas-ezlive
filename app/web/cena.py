"""Cena — Sam's personal operational AI surface (PART 4 of Sam's
2026-05-15 directive).

This file is the Render-side counterpart to cena_gateway.py on AiCk:

 - POST /sam/cena/log  — the gateway POSTs one row per tool invocation
   here. Auth: shared X-Cena-Token header (CENA_GATEWAY_TOKEN env).
 - GET  /sam/cena-audit/ — reverse-chronological feed of CenaActionLog
   for Sam. Auth: SAM_CHAT_USER_ID match, same as /sam/chat.

Why a separate blueprint from sam_chat.py: the audit surface + log
ingest are operationally distinct from the chat surface, even though
they share the SAM_CHAT_USER_ID gate. Keeping them apart makes the
sam_chat file focused on chat and the cena file focused on the agent's
operational tail.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any

from flask import (
    Blueprint, g, jsonify, redirect, render_template, request, url_for,
)

from app.db import SessionLocal
from app.models import (
    CenaActionLog,
    SamChatMessage,
    SamChatSession,
    _VALID_CENA_ACTION_TYPES,
)

logger = logging.getLogger(__name__)

cena_bp = Blueprint("cena", __name__)


# ============================================================
# Access gates
# ============================================================
# Sam-only view (shared semantics with sam_chat.is_sam_chat_user — kept
# inline so cena.py doesn't depend on sam_chat's import surface).

def _sam_chat_user_id() -> int | None:
    raw = (os.getenv("SAM_CHAT_USER_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("cena: SAM_CHAT_USER_ID=%r is not an integer", raw)
        return None


def _is_sam() -> bool:
    sam_id = _sam_chat_user_id()
    if sam_id is None:
        return False
    user = getattr(g, "current_user", None)
    return user is not None and getattr(user, "id", None) == sam_id


def _require_sam_page():
    if not _is_sam():
        return redirect(url_for("auth.access_denied",
                                need="sam_chat", next=request.path))
    return None


def _cena_gateway_token() -> str:
    """Shared secret used between the gateway and this endpoint.

    Read with utf-8-sig because the cena_token.txt on AiCk historically
    had a UTF-8 BOM — kept defensive at the boundary so a future re-add
    of the BOM doesn't silently break auth. (PART 3 lesson.)
    """
    raw = os.getenv("CENA_GATEWAY_TOKEN") or ""
    # Strip BOM defensively (env var values shouldn't have it, but just
    # in case it gets pasted in from a BOM'd file later).
    if raw and raw.startswith("﻿"):
        raw = raw[1:]
    return raw.strip()


def _require_gateway_token():
    """Gate for /sam/cena/log — must match CENA_GATEWAY_TOKEN."""
    want = _cena_gateway_token()
    if not want:
        return jsonify({"ok": False,
                        "error": "gateway log not configured"}), 503
    got = request.headers.get("X-Cena-Token", "")
    if got != want:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    return None


# ============================================================
# POST /sam/cena/log — the gateway calls this after each tool run
# ============================================================
# Body shape (JSON):
#   {
#     "action_type":  "shell_execute" | ...,
#     "parameters":   { ... arbitrary tool args ... },
#     "result":       { ... arbitrary return value, or null on failure },
#     "success":      true | false,
#     "error_text":   "..."  (when success=false),
#     "started_at":   "2026-05-15T17:34:00.123Z"  (ISO 8601 UTC),
#     "finished_at":  "2026-05-15T17:34:00.456Z",
#     "session_id":   <int | null>,
#     "message_id":   <int | null>
#   }
#
# Response: {"ok": true, "id": <row_id>}

def _parse_iso_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Tolerate both "...Z" and "...+00:00"; strip Z if present.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        # Persist as naive UTC (the rest of the schema is naive).
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


@cena_bp.route("/sam/cena/log", methods=["POST"])
def cena_log_endpoint():
    """Ingest one CenaActionLog row from the gateway."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body: Any = request.get_json(silent=True) or {}
    action_type = (body.get("action_type") or "").strip()
    if not action_type:
        return jsonify({"ok": False,
                        "error": "action_type required"}), 400
    if action_type not in _VALID_CENA_ACTION_TYPES:
        # Don't reject — keep the surface forward-compatible — but log.
        logger.warning("cena: unrecognised action_type %r", action_type)

    parameters = body.get("parameters")
    if not isinstance(parameters, (dict, list)):
        parameters = {} if parameters is None else {"value": parameters}

    result = body.get("result")
    if result is not None and not isinstance(result, (dict, list)):
        result = {"value": result}

    started = _parse_iso_utc(body.get("started_at")) or datetime.utcnow()
    finished = _parse_iso_utc(body.get("finished_at"))

    success = body.get("success", True)
    error_text = body.get("error_text")
    session_id = body.get("session_id")
    message_id = body.get("message_id")

    db = SessionLocal()
    try:
        row = CenaActionLog(
            action_type=action_type,
            parameters=parameters,
            result=result,
            success=bool(success),
            error_text=error_text if error_text else None,
            started_at=started,
            finished_at=finished,
            session_id=session_id if isinstance(session_id, int) else None,
            message_id=message_id if isinstance(message_id, int) else None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return jsonify({"ok": True, "id": row.id})
    except Exception:  # noqa: BLE001
        logger.exception("cena: failed to persist action log")
        db.rollback()
        return jsonify({"ok": False, "error": "persist failed"}), 500
    finally:
        db.close()


# ============================================================
# GET /sam/cena-audit/ — the audit feed
# ============================================================

@cena_bp.route("/sam/cena-audit/", methods=["GET"])
@cena_bp.route("/sam/cena-audit", methods=["GET"])
def cena_audit_view():
    """Reverse-chronological feed of Cena tool invocations.

    Filters (query params): action=<type>, success=true|false,
    since=<iso>, until=<iso>, session=<id>, limit=<n>.
    Default limit 100 (max 500).
    """
    gate = _require_sam_page()
    if gate is not None:
        return gate

    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
    except ValueError:
        limit = 100

    db = SessionLocal()
    try:
        q = db.query(CenaActionLog)
        if a := request.args.get("action"):
            q = q.filter(CenaActionLog.action_type == a)
        if s := request.args.get("success"):
            if s.lower() in ("0", "false", "no"):
                q = q.filter(CenaActionLog.success.is_(False))
            elif s.lower() in ("1", "true", "yes"):
                q = q.filter(CenaActionLog.success.is_(True))
        if sid := request.args.get("session"):
            try:
                q = q.filter(CenaActionLog.session_id == int(sid))
            except ValueError:
                pass
        if since := _parse_iso_utc(request.args.get("since")):
            q = q.filter(CenaActionLog.started_at >= since)
        if until := _parse_iso_utc(request.args.get("until")):
            q = q.filter(CenaActionLog.started_at < until)
        rows = (q.order_by(CenaActionLog.started_at.desc(),
                           CenaActionLog.id.desc())
                .limit(limit).all())

        # 24h success/error counts for the header summary.
        cutoff = datetime.utcnow() - timedelta(hours=24)
        recent = db.query(CenaActionLog).filter(
            CenaActionLog.started_at >= cutoff).all()
        summary = {
            "window_hours": 24,
            "total": len(recent),
            "success": sum(1 for r in recent if r.success),
            "error": sum(1 for r in recent if not r.success),
            "by_action": {},
        }
        for r in recent:
            summary["by_action"][r.action_type] = (
                summary["by_action"].get(r.action_type, 0) + 1)

        return render_template(
            "cena_audit.html",
            rows=rows,
            summary=summary,
            limit=limit,
            valid_actions=sorted(_VALID_CENA_ACTION_TYPES),
            filters={
                "action":  request.args.get("action", ""),
                "success": request.args.get("success", ""),
                "session": request.args.get("session", ""),
                "since":   request.args.get("since", ""),
                "until":   request.args.get("until", ""),
            },
        )
    finally:
        db.close()


def install(app):
    """Register the cena blueprint."""
    app.register_blueprint(cena_bp)
