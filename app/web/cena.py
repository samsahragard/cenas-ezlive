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
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import (
    Blueprint, Response, abort, current_app, g, jsonify, redirect, render_template, request, url_for,
)
from sqlalchemy import func

from app.db import SessionLocal
from app.models import (
    AccessRequest,
    CenaActionLog,
    DeveloperChatMessage,
    Driver,
    Order,
    SamChatMessage,
    SamChatSession,
    User,
    UserAuditLog,
    _VALID_CENA_ACTION_TYPES,
    _VALID_SAM_CHAT_ROLES,
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


def _require_gateway_token_or_partner():
    """Gate that accepts EITHER X-Cena-Token (cena gateway path) OR a
    partner-authenticated Flask session (developer-tier observers like
    dck who only have partner_password.txt + site cookie, not the
    CENA_GATEWAY_TOKEN). Used by /sam/cena/sam-chat per Sam #2204 + Track 8
    spec — dck can self-auth without a cross-user token copy.

    Per-agent X-Author tokens are samai-spec future-lane; this dual-path
    gate is the immediate unblock so dck observes /sam/chat with just
    partner-tier credentials she already has."""
    from flask import session as _flask_session
    # Path 1: X-Cena-Token (cena gateway + scripts with the token)
    want = _cena_gateway_token()
    got = request.headers.get("X-Cena-Token", "")
    if want and got == want:
        return None
    # Path 2: partner-authenticated session (chat_tail-style auth)
    if _flask_session.get("partner_auth_ok"):
        return None
    return jsonify({"ok": False, "error": "unauthorized"}), 403


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
        # astimezone() with no arg converts to local time; force UTC so
        # the resulting naive datetime is comparable to the rest of the
        # schema (which stores naive-UTC throughout).
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
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

        # Cena start point for the audit display.
        cena_start_point = (
            db.query(func.min(DeveloperChatMessage.created_at))
              .filter(DeveloperChatMessage.author == "cena")
              .scalar()
        )

        return render_template(
            "cena_audit.html",
            rows=rows,
            summary=summary,
            limit=limit,
            valid_actions=sorted(_VALID_CENA_ACTION_TYPES),
            cena_start_point=(
                cena_start_point.isoformat() + "Z"
                if cena_start_point else None
            ),
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


# ============================================================
# POST /sam/cena/db-probe/driver-row — Sam-only diagnostic
# ============================================================
# Lookup ONE Driver row by id / phone / name and return the diagnostic
# fields aick needs to debug PIN-reset failures (samai #1503, 2026-05-15).
# Read-only. Returns no plaintext credentials — passcode_hash is reported
# as length + 8-char prefix only, enough to confirm "yes a fresh bcrypt
# landed here" without leaking the hash itself.
#
# Body (JSON), exactly one of:
#   {"id": <int>}
#   {"phone": "<10-digit-or-raw>"}     (normalized via _norm before compare)
#   {"name": "<substring>"}            (case-insensitive LIKE)
#
# Response (JSON):
#   {"ok": true, "rows": [{...diagnostic fields...}, ...]}
#
# Auth: X-Cena-Token header. Same gate as /sam/cena/log.

@cena_bp.route("/sam/cena/db-probe/driver-row", methods=["POST"])
def cena_db_probe_driver():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        q = db.query(Driver)
        if (did := body.get("id")) is not None:
            try:
                q = q.filter(Driver.id == int(did))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "id must be int"}), 400
        elif (phone_raw := body.get("phone")):
            # Match on normalized digits — re-uses the same normalize_phone
            # logic the login flow uses, so the query mirrors what login sees.
            from app.services.ezcater_known_drivers_seed import normalize_phone
            target = normalize_phone(phone_raw)
            # SQL can't easily do the normalization, so pull candidates by
            # substring and filter in Python. Reasonable for diagnostic use.
            rows = [d for d in q.all()
                    if d.phone and normalize_phone(d.phone) == target]
            return _driver_probe_response(rows)
        elif (name := body.get("name")):
            q = q.filter(Driver.name.ilike(f"%{name}%"))
        else:
            return jsonify({"ok": False,
                            "error": "need id, phone, or name"}), 400

        return _driver_probe_response(q.all())
    finally:
        db.close()


def _driver_probe_response(rows):
    """Serialize Driver rows to the diagnostic view aick needs. Never
    returns the full passcode_hash — only its length + 8-char prefix
    (enough to compare two hashes by eye, not enough to reverse)."""
    out = []
    for d in rows:
        ph = d.passcode_hash or ""
        out.append({
            "id": d.id,
            "name": d.name,
            "email": d.email,
            "phone": d.phone,
            "active": bool(d.active),
            "status": d.status,
            "first_login_done": bool(d.first_login_done),
            "failed_attempts": d.failed_attempts,
            "lockout_until": (d.lockout_until.isoformat()
                              if d.lockout_until else None),
            "passcode_hash_length": len(ph),
            "passcode_hash_prefix": ph[:8] if ph else "",
            "password_hash_set": bool(d.password_hash),
            "session_version": d.session_version,
            "created_at": (d.created_at.isoformat()
                           if d.created_at else None),
            # Driver model doesn't have updated_at; the closest signal of
            # "recently reset" is session_version increments + passcode_hash
            # prefix change. Return what we have.
        })
    return jsonify({"ok": True, "count": len(out), "rows": out})


# ============================================================
# POST /sam/cena/db-probe/verify-pin — Sam-only diagnostic
# ============================================================
# Run werkzeug.security.check_password_hash() against ONE driver row's
# stored passcode_hash. Returns boolean match + hash algorithm prefix
# so aick can confirm scrypt/bcrypt/pbkdf2 round-trip works in the
# same Python process the login route uses.
#
# Body (JSON):
#   {"driver_id": <int>, "pin": "<plaintext>"}
#
# Response (JSON):
#   {"ok": true, "match": true|false, "hash_prefix": "scrypt:3..." }
#
# The plaintext PIN is NEVER persisted or logged here — it lives only
# in this request's body for the duration of the check, then is dropped.
# Auth: X-Cena-Token, same as the other probe.

@cena_bp.route("/sam/cena/db-probe/verify-pin", methods=["POST"])
def cena_db_probe_verify_pin():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    try:
        driver_id = int(body.get("driver_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "driver_id required"}), 400
    pin = body.get("pin")
    if not isinstance(pin, str) or not pin:
        return jsonify({"ok": False, "error": "pin required"}), 400

    from werkzeug.security import check_password_hash

    db = SessionLocal()
    try:
        d = db.get(Driver, driver_id)
        if d is None:
            return jsonify({"ok": False, "error": "driver not found"}), 404
        ph = d.passcode_hash or ""
        if not ph:
            return jsonify({"ok": True, "match": False,
                            "hash_prefix": "",
                            "reason": "passcode_hash empty"})
        match = check_password_hash(ph, pin)
        return jsonify({
            "ok": True,
            "match": bool(match),
            "hash_prefix": ph[:32],  # enough to identify algorithm + params
            "driver_id": d.id,
            "driver_active": bool(d.active),
            "first_login_done": bool(d.first_login_done),
        })
    finally:
        db.close()


# ============================================================
# POST /sam/cena/db-probe/user-row — Sam-only diagnostic
# ============================================================
# Parallel to /sam/cena/db-probe/driver-row but for the users table.
# Issue 4 architectural integrity check (2026-05-15).
#
# Body (JSON), one of: {"id": int} / {"email": str} / {"phone": str} /
#                      {"name": str (substring)}
# Returns the diagnostic fields for matching User rows. No plaintext
# credentials — passcode_hash is reported as length + 8-char prefix.

@cena_bp.route("/sam/cena/db-probe/user-row", methods=["POST"])
def cena_db_probe_user():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        q = db.query(User)
        if (uid := body.get("id")) is not None:
            try:
                q = q.filter(User.id == int(uid))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "id must be int"}), 400
        elif (email := body.get("email")):
            q = q.filter(User.email.ilike(email))
        elif (phone := body.get("phone")):
            from app.services.ezcater_known_drivers_seed import normalize_phone
            target = normalize_phone(phone)
            rows = [u for u in q.all()
                    if u.phone and normalize_phone(u.phone) == target]
            return _user_probe_response(rows)
        elif (name := body.get("name")):
            q = q.filter(User.full_name.ilike(f"%{name}%"))
        else:
            return jsonify({"ok": False,
                            "error": "need id, email, phone, or name"}), 400
        return _user_probe_response(q.all())
    finally:
        db.close()


def _user_probe_response(rows):
    out = []
    for u in rows:
        ph = u.passcode_hash or ""
        out.append({
            "id": u.id,
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "permission_level": u.permission_level,
            "store_scope": u.store_scope,
            "active": bool(u.active),
            "first_login_done": bool(u.first_login_done),
            "failed_attempts": u.failed_attempts,
            "lockout_until": (u.lockout_until.isoformat()
                              if u.lockout_until else None),
            "passcode_hash_length": len(ph),
            "passcode_hash_prefix": ph[:8] if ph else "",
            "session_version": u.session_version,
            "created_at": (u.created_at.isoformat()
                           if u.created_at else None),
            "updated_at": (u.updated_at.isoformat()
                           if u.updated_at else None),
            "last_login_at": (u.last_login_at.isoformat()
                              if u.last_login_at else None),
        })
    return jsonify({"ok": True, "count": len(out), "rows": out})


# ============================================================
# POST /sam/cena/db-probe/access-requests — Sam-only diagnostic
# ============================================================
# Pull recent rows from the access_requests table (driver self-signup
# overflow path). Issue 4 SYMPTOM A — Sam's self-signup attempt with
# samsahragard@gmail.com may or may not have written a row.
#
# Body (JSON): {"email": str?, "since_minutes": int?, "limit": int?}

@cena_bp.route("/sam/cena/db-probe/access-requests", methods=["POST"])
def cena_db_probe_access_requests():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    limit = max(1, min(50, int(body.get("limit") or 20)))
    db = SessionLocal()
    try:
        q = db.query(AccessRequest)
        if (email := body.get("email")):
            q = q.filter(AccessRequest.email.ilike(email))
        if (since_min := body.get("since_minutes")):
            cutoff = datetime.utcnow() - timedelta(minutes=int(since_min))
            q = q.filter(AccessRequest.created_at >= cutoff)
        rows = q.order_by(AccessRequest.created_at.desc()).limit(limit).all()
        out = [{
            "id": r.id,
            "full_name": r.full_name,
            "email": r.email,
            "phone": r.phone,
            "requested_role": r.requested_role,
            "reason": r.reason,
            "status": r.status,
            "created_at": (r.created_at.isoformat()
                           if r.created_at else None),
        } for r in rows]
        return jsonify({"ok": True, "count": len(out), "rows": out})
    finally:
        db.close()


# ============================================================
# POST /sam/cena/db-probe/deactivate-users — Sam-only ops
# ============================================================
# Issue 4 cleanup tool (samai #1511, 2026-05-15). Deactivates users
# with permission_level="driver" — the architectural leak. Always
# scans for matching rows; deactivation only happens when dry_run
# is false AND the id is in `ids` AND the row still qualifies.
#
# Body (JSON):
#   {"ids": [<int>, ...], "dry_run": <bool>}
#
# Response (HTTP 200, JSON):
#   {
#     "ok": true,
#     "scanned_matching": [<int>, ...],
#     "requested_ids":    [<int>, ...],
#     "deactivated_ids":  [<int>, ...],
#     "skipped":          [{"id": <int>, "reason": "<enum>"}, ...],
#     "dry_run":          <bool>,
#     "actor":            "cena",
#     "ts":               "<ISO-8601 UTC>"
#   }
#
# skipped.reason is a closed enum: "not_found" | "already_inactive"
#                                   | "not_a_driver_user"

_SKIP_NOT_FOUND          = "not_found"
_SKIP_ALREADY_INACTIVE   = "already_inactive"
_SKIP_NOT_A_DRIVER_USER  = "not_a_driver_user"


def _role_state(level, store_scope) -> str:
    """Mirror team_routes._role_state for audit-log before/after parity."""
    return f"{level or ''}|{store_scope or ''}"


@cena_bp.route("/sam/cena/db-probe/deactivate-users", methods=["POST"])
def cena_db_probe_deactivate_users():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    raw_ids = body.get("ids") or []
    if not isinstance(raw_ids, list):
        return jsonify({"ok": False, "error": "ids must be a list"}), 400
    requested_ids: list[int] = []
    for v in raw_ids:
        try:
            requested_ids.append(int(v))
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": f"id {v!r} is not an int"}), 400
    dry_run = bool(body.get("dry_run", False))

    db = SessionLocal()
    try:
        # scanned_matching: all currently-active users with the driver leak.
        # Independent of `ids`; pure audit signal so Sam knows the surface.
        scanned_rows = (
            db.query(User)
              .filter(User.permission_level == "driver")
              .filter(User.active.is_(True))
              .all()
        )
        scanned_ids = sorted(u.id for u in scanned_rows)

        deactivated_ids: list[int] = []
        skipped: list[dict] = []

        for uid in requested_ids:
            u = db.get(User, uid)
            if u is None:
                skipped.append({"id": uid, "reason": _SKIP_NOT_FOUND})
                continue
            if u.permission_level != "driver":
                skipped.append({"id": uid,
                                "reason": _SKIP_NOT_A_DRIVER_USER})
                continue
            if not u.active:
                skipped.append({"id": uid,
                                "reason": _SKIP_ALREADY_INACTIVE})
                continue
            if dry_run:
                # In dry-run we explicitly do NOT count it as deactivated.
                continue
            # Apply the deactivation + atomic audit row.
            before = _role_state(u.permission_level, u.store_scope)
            after = before + " [inactive]"
            u.active = False
            u.session_version = (u.session_version or 1) + 1
            db.add(UserAuditLog(
                target_user_id=u.id,
                target_label=u.full_name,
                actor_user_id=None,
                actor_label="cena",
                action="deactivate_driver_leak",
                before_value=before,
                after_value=after,
                details="Issue 4 cleanup (samai #1511 spec); "
                        "permission_level=driver on users table is a leak; "
                        "drivers belong in the drivers table.",
                ip=(request.remote_addr or None) if request else None,
            ))
            deactivated_ids.append(uid)

        if dry_run:
            db.rollback()
        else:
            db.commit()

        return jsonify({
            "ok": True,
            "scanned_matching": scanned_ids,
            "requested_ids": requested_ids,
            "deactivated_ids": sorted(deactivated_ids),
            "skipped": skipped,
            "dry_run": dry_run,
            "actor": "cena",
            "ts": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: deactivate-users failed")
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


# ============================================================
# POST /sam/cena/db-probe/list-drivers — Sam-only diagnostic
# ============================================================
# Returns a compact view of every row in the drivers table. Body
# accepts an optional {active_only: bool} flag — when True, only
# rows where Driver.active is True are returned. Used as the
# pre-flight survey for the deactivate-drivers bulk pass per
# samai #1562 spec.

@cena_bp.route("/sam/cena/db-probe/list-drivers", methods=["POST"])
def cena_db_probe_list_drivers():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    active_only = bool(body.get("active_only", False))

    db = SessionLocal()
    try:
        q = db.query(Driver).order_by(Driver.id.asc())
        if active_only:
            q = q.filter(Driver.active.is_(True))
        return _driver_probe_response(q.all())
    finally:
        db.close()


# ============================================================
# POST /sam/cena/db-probe/deactivate-drivers — Sam-only write
# ============================================================
# Bulk-deactivates rows in the drivers table per samai #1562 spec.
# Counterpart to deactivate-users for the drivers table; same
# safety pattern (dry-run-first → confirm → live).
#
# Body (JSON):
#   {ids: [int] | null, all_active: bool, dry_run: bool,
#    audit_reason: str}
#
# Scope resolution: if all_active is True, the target is every
# row where Driver.active is True (ids is ignored). Otherwise
# `ids` enumerates the targets. Exactly one of the two MUST be
# supplied.
#
# Response (JSON):
#   {ok, scanned_matching_active, requested_scope,
#    deactivated_ids, skipped, dry_run, actor, ts}
#
# Each deactivation flips Driver.active = False, bumps
# session_version (invalidates any live session), and writes a
# UserAuditLog row (DriverAuditLog doesn't exist yet — pending
# follow-up table; using UserAuditLog with action="deactivate
# _driver_bulk" + driver_id stamped in details per samai spec).

@cena_bp.route("/sam/cena/db-probe/deactivate-drivers", methods=["POST"])
def cena_db_probe_deactivate_drivers():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    all_active = bool(body.get("all_active", False))
    raw_ids = body.get("ids")
    dry_run = bool(body.get("dry_run", False))
    audit_reason = body.get("audit_reason") or ""

    if all_active and raw_ids:
        return jsonify({"ok": False,
                        "error": "supply ids OR all_active, not both"}), 400
    if not all_active and not raw_ids:
        return jsonify({"ok": False,
                        "error": "need ids or all_active=true"}), 400

    requested_ids: list[int] = []
    if raw_ids:
        if not isinstance(raw_ids, list):
            return jsonify({"ok": False,
                            "error": "ids must be a list"}), 400
        for v in raw_ids:
            try:
                requested_ids.append(int(v))
            except (TypeError, ValueError):
                return jsonify({"ok": False,
                                "error": f"id {v!r} is not an int"}), 400

    db = SessionLocal()
    try:
        scanned_rows = (
            db.query(Driver)
              .filter(Driver.active.is_(True))
              .order_by(Driver.id.asc())
              .all()
        )
        scanned_ids = [d.id for d in scanned_rows]

        if all_active:
            requested_scope = "all_active"
            target_ids = list(scanned_ids)
        else:
            requested_scope = "ids"
            target_ids = requested_ids

        deactivated_ids: list[int] = []
        skipped: list[dict] = []

        for did in target_ids:
            d = db.get(Driver, did)
            if d is None:
                skipped.append({"id": did, "reason": _SKIP_NOT_FOUND})
                continue
            if not d.active:
                skipped.append({"id": did,
                                "reason": _SKIP_ALREADY_INACTIVE})
                continue
            if dry_run:
                continue
            before = f"active=true|status={d.status or ''}"
            after = f"active=false|status={d.status or ''} [inactive]"
            d.active = False
            d.session_version = (d.session_version or 1) + 1
            db.add(UserAuditLog(
                target_user_id=None,
                target_label=d.name,
                actor_user_id=None,
                actor_label="cena",
                action="deactivate_driver_bulk",
                before_value=before,
                after_value=after,
                details=(f"driver_id={d.id}; phone={d.phone}; "
                         f"reason={audit_reason}"),
                ip=(request.remote_addr or None) if request else None,
            ))
            deactivated_ids.append(did)

        if dry_run:
            db.rollback()
        else:
            db.commit()

        return jsonify({
            "ok": True,
            "scanned_matching_active": scanned_ids,
            "requested_scope": requested_scope,
            "requested_ids": (requested_ids if requested_scope == "ids"
                              else target_ids),
            "deactivated_ids": sorted(deactivated_ids),
            "skipped": skipped,
            "dry_run": dry_run,
            "actor": "cena",
            "ts": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: deactivate-drivers failed")
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


# ============================================================
# POST /sam/cena/db-probe/list-orders — Sam-only diagnostic
# ============================================================
# Lists Order rows for finding IDs by date/client/status. Read-
# only, no audit row. Pre-flight survey for set-order-status.
#
# Body (JSON, all optional):
#   {status: str | null, client_contains: str | null,
#    deliver_at_contains: str | null, limit: int (default 25)}
#
# Response (JSON):
#   {ok, orders: [{id, status, client, deliver_at,
#                  reported_store, total_amount, potential_payout}],
#    count, actor, ts}

@cena_bp.route("/sam/cena/db-probe/list-orders", methods=["POST"])
def cena_db_probe_list_orders():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    status_filter = (body.get("status") or "").strip() or None
    client_substr = (body.get("client_contains") or "").strip() or None
    deliver_substr = (body.get("deliver_at_contains") or "").strip() or None
    try:
        limit = int(body.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    limit = max(1, min(limit, 200))

    delivery_date_gte = (body.get("delivery_date_gte") or "").strip() or None

    db = SessionLocal()
    try:
        q = db.query(Order)
        if status_filter is not None:
            q = q.filter(Order.status == status_filter)
        if client_substr is not None:
            q = q.filter(Order.client.ilike(f"%{client_substr}%"))
        if deliver_substr is not None:
            q = q.filter(Order.deliver_at.ilike(f"%{deliver_substr}%"))
        if delivery_date_gte is not None:
            q = q.filter(Order.delivery_date >= delivery_date_gte)
        rows = q.order_by(Order.delivery_date.asc().nullslast(),
                          Order.deliver_at.asc(), Order.id.asc()).limit(limit).all()
        return jsonify({
            "ok": True,
            "orders": [{
                "id": o.id,
                "status": o.status,
                "client": o.client,
                "delivery_date": o.delivery_date,
                "deliver_at": o.deliver_at,
                "reported_store": o.reported_store,
                "total_amount": o.total_amount,
                "potential_payout": o.potential_payout,
            } for o in rows],
            "count": len(rows),
            "actor": "cena",
            "ts": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: list-orders failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


# ============================================================
# POST /sam/cena/db-probe/set-order-status — Sam-only write
# ============================================================
# Sets Order.status to a documented lifecycle value. Used to
# unblock testing when ingest leaves orders in an undocumented
# state (e.g. 'processed' from the ezCater pipeline, which isn't
# in the {new, available, requested, approved, picked_up, en_route,
# delivered, cancelled, no_show} taxonomy and so fails
# lifecycle._check on every transition).
#
# Body (JSON):
#   {id: int, status: str, audit_reason: str}
#
# Response (JSON):
#   {ok, order_id, before_status, after_status, dry_run, actor, ts}

_VALID_ORDER_STATUSES = {
    "new", "available", "requested", "approved", "picked_up",
    "en_route", "delivered", "cancelled", "no_show",
    # Undocumented ingest artifact — accepted so rollback after testing
    # can restore the original state until T2 (ingest trace + samai
    # spec) properly removes 'processed' from the data plane.
    "processed",
}


@cena_bp.route("/sam/cena/db-probe/set-order-status", methods=["POST"])
def cena_db_probe_set_order_status():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    raw_id = body.get("id")
    new_status = (body.get("status") or "").strip()
    audit_reason = body.get("audit_reason") or ""
    dry_run = bool(body.get("dry_run", False))

    try:
        order_id = int(raw_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False,
                        "error": f"id {raw_id!r} is not an int"}), 400

    if new_status not in _VALID_ORDER_STATUSES:
        return jsonify({"ok": False,
                        "error": (f"status {new_status!r} not in "
                                  f"{sorted(_VALID_ORDER_STATUSES)}")}), 400

    db = SessionLocal()
    try:
        o = db.get(Order, order_id)
        if o is None:
            return jsonify({"ok": False,
                            "error": f"order id={order_id} not found"}), 404

        before_status = o.status
        if before_status == new_status:
            return jsonify({
                "ok": True,
                "order_id": order_id,
                "before_status": before_status,
                "after_status": new_status,
                "noop": True,
                "dry_run": dry_run,
                "actor": "cena",
                "ts": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            })

        if not dry_run:
            o.status = new_status
            db.add(UserAuditLog(
                target_user_id=None,
                target_label=f"order_id={order_id}",
                actor_user_id=None,
                actor_label="cena",
                action="set_order_status",
                before_value=f"status={before_status or ''}",
                after_value=f"status={new_status}",
                details=(f"order_id={order_id}; client={o.client}; "
                         f"deliver_at={o.deliver_at}; reason={audit_reason}"),
                ip=(request.remote_addr or None) if request else None,
            ))
            db.commit()
        else:
            db.rollback()

        return jsonify({
            "ok": True,
            "order_id": order_id,
            "before_status": before_status,
            "after_status": new_status,
            "noop": False,
            "dry_run": dry_run,
            "actor": "cena",
            "ts": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: set-order-status failed")
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


# ============================================================
# POST /sam/cena/db-probe/sam-chat-cache-tokens — gate-3 verify
# ============================================================
# Returns the most-recent N SamChatMessage assistant rows with their
# cache-token columns so samai's gate-3 probe can confirm migration 25
# is wired end-to-end (post Render deploy of 59f8022): cache_creation
# > 0 on the cold-start turn, cache_read > 0 on subsequent warm turns.
# Read-only. No content, no PII — just the cost-column counters + id +
# created_at + model.
#
# Body (JSON):
#   {"limit": <int, default 5, max 50>}
#
# Response (JSON):
#   {"ok": true, "rows": [{id, created_at, model,
#                          cost_input_tokens, cost_output_tokens,
#                          cost_cache_creation_tokens,
#                          cost_cache_read_tokens, cost_usd}, ...]}
#
# Auth: X-Cena-Token header (same gate as the other db-probes).

@cena_bp.route("/sam/cena/db-probe/sam-chat-cache-tokens",
               methods=["POST"])
def cena_db_probe_sam_chat_cache_tokens():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get("limit", 5))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit must be int"}), 400
    limit = max(1, min(limit, 50))

    from app.models import SamChatMessage
    db = SessionLocal()
    try:
        rows = (db.query(SamChatMessage)
                  .filter(SamChatMessage.role == "assistant")
                  .order_by(SamChatMessage.created_at.desc(),
                            SamChatMessage.id.desc())
                  .limit(limit)
                  .all())
        out = []
        for r in rows:
            out.append({
                "id": r.id,
                "created_at": (r.created_at.isoformat()
                               if r.created_at else None),
                "model": r.model,
                "cost_input_tokens": r.cost_input_tokens,
                "cost_output_tokens": r.cost_output_tokens,
                "cost_cache_creation_tokens": r.cost_cache_creation_tokens,
                "cost_cache_read_tokens": r.cost_cache_read_tokens,
                "cost_usd": (str(r.cost_usd) if r.cost_usd is not None
                             else None),
            })
        return jsonify({"ok": True, "rows": out, "count": len(out)})
    finally:
        db.close()


# ============================================================
# POST /sam/cena/db-probe/dev-chat-attribution-corrections
# ============================================================
# Returns the most-recent N DevChatAttributionCorrection rows so
# samai's gate-3 probe on the d693c7e sidecar commit can confirm:
# (a) migration 26 table CREATE landed (endpoint returns 200, not 500),
# (b) the seed for the 2026-05-17 incident rows landed (4 entries:
#     message_id ∈ {2051, 2056, 2097, 2098} with original_author='sam',
#     corrected_author='ck', corrected_by='samai').
#
# Body (JSON):
#   {"limit": <int, default 10, max 100>}
#
# Response (JSON):
#   {"ok": true, "rows": [{id, message_id, original_author,
#                          corrected_author, correction_reason,
#                          corrected_at, corrected_by}, ...]}
#
# Auth: X-Cena-Token header (same gate as the other db-probes).

@cena_bp.route("/sam/cena/db-probe/dev-chat-attribution-corrections",
               methods=["POST"])
def cena_db_probe_dev_chat_attribution_corrections():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get("limit", 10))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit must be int"}), 400
    limit = max(1, min(limit, 100))

    from app.models import DevChatAttributionCorrection
    db = SessionLocal()
    try:
        rows = (db.query(DevChatAttributionCorrection)
                  .order_by(DevChatAttributionCorrection.corrected_at.desc(),
                            DevChatAttributionCorrection.id.desc())
                  .limit(limit)
                  .all())
        out = []
        for r in rows:
            out.append({
                "id": r.id,
                "message_id": r.message_id,
                "original_author": r.original_author,
                "corrected_author": r.corrected_author,
                "correction_reason": r.correction_reason,
                "corrected_at": (r.corrected_at.isoformat()
                                 if r.corrected_at else None),
                "corrected_by": r.corrected_by,
            })
        return jsonify({"ok": True, "rows": out, "count": len(out)})
    finally:
        db.close()


# ============================================================
# GET /sam/cena/sam-chat — Sam Chat read access for dck (and team)
# ============================================================
# Returns recent /sam/chat (Sam<>Cena) messages in chronological
# order. Built per cena #1907 + samai #1959 Track 8 spec: cursor-
# tracked, X-Cena-Token gated, observer-only (no posting).
#
# DIVERGENCE FROM /sam/cena/dev-chat: read_dev_chat anchors its
# "start_point" at cena's earliest post. dck (the primary reader
# of /sam/chat) never posts to /sam/chat — observer-only by Sam's
# Track 8 spec — so there's no equivalent anchor. Default window
# = last SAM_CHAT_READ_DEFAULT_HOURS hours (env-configurable, 24
# default). Callers can override with explicit `since=` or
# include_all=true.
#
# Query params:
#   limit: int, default 30, max 200
#   since: ISO datetime string; default = now - default-window
#   include_all: bool ("1"/"true"/"yes"); default false. If true,
#     ignores the start-point filter and returns the most recent
#     `limit` rows across the whole table.
#   session_id: int, optional. If set, restricts to one session.
#
# Response:
#   {"ok": true, "messages": [...], "count": n,
#    "window_start": "<ISO>Z" | null,
#    "default_window_hours": <int>}
# Each message: {id, session_id, role, content (truncated 2000ch),
#                model, created_at}
#
# Auth: X-Cena-Token header (same gate as /sam/cena/log + other
# /sam/cena/* endpoints). Per-agent token (DCK_TOKEN) is samai-
# spec future-lane.

@cena_bp.route("/sam/cena/sam-chat", methods=["GET"])
def cena_sam_chat_read():
    # Dual-path auth (Sam #2204): X-Cena-Token OR partner session,
    # so dck (partner-tier observer, no CENA_GATEWAY_TOKEN) can self-
    # auth with the partner_password she already has. Per-agent token
    # remains samai-spec future-lane.
    gate = _require_gateway_token_or_partner()
    if gate is not None:
        return gate

    try:
        limit = max(1, min(200, int(request.args.get("limit", 30))))
    except (ValueError, TypeError):
        limit = 30

    try:
        default_window_hours = int(
            os.getenv("SAM_CHAT_READ_DEFAULT_HOURS", "24"))
    except (ValueError, TypeError):
        default_window_hours = 24

    include_all = (request.args.get("include_all", "").lower()
                   in ("1", "true", "yes"))

    session_id_raw = request.args.get("session_id", "").strip()
    session_id = None
    if session_id_raw:
        try:
            session_id = int(session_id_raw)
        except ValueError:
            return jsonify({"ok": False,
                            "error": "session_id must be int"}), 400

    from app.models import SamChatMessage
    db = SessionLocal()
    try:
        window_start = None
        if include_all:
            window_start = None
        elif (raw_since := request.args.get("since", "")):
            window_start = _parse_iso_utc(raw_since)
            if window_start is None:
                window_start = (datetime.utcnow()
                                - timedelta(hours=default_window_hours))
        else:
            window_start = (datetime.utcnow()
                            - timedelta(hours=default_window_hours))

        q = db.query(SamChatMessage)
        if window_start is not None:
            q = q.filter(SamChatMessage.created_at >= window_start)
        if session_id is not None:
            q = q.filter(SamChatMessage.session_id == session_id)
        # Newest N first, then reverse to chronological in the response.
        rows = (q.order_by(SamChatMessage.created_at.desc(),
                           SamChatMessage.id.desc())
                .limit(limit).all())
        rows.reverse()

        # Per Sam #837 item 5 — surface attachment IDs alongside each
        # message so aick/ck/samai polling tails can fetch the same
        # image cena saw at API time. Eager-load attachments for the
        # window we're returning in one query.
        from app.models import SamChatAttachment as _SCA
        attach_by_msg: dict[int, list[dict]] = {}
        if rows:
            msg_ids = [m.id for m in rows]
            for a in (db.query(_SCA)
                        .filter(_SCA.message_id.in_(msg_ids))
                        .all()):
                attach_by_msg.setdefault(a.message_id, []).append({
                    "id": a.id,
                    "content_type": a.content_type,
                    "filename": a.filename,
                    "url": f"/sam/cena/sam-chat-attachment/{a.id}",
                })

        messages = []
        # 8000-char cap per samai #2199 spec input — covers ~95% of
        # Cena's longer /sam/chat turns without truncation, and when
        # truncation does happen the explicit marker keeps dck's
        # reasoning from confabulating off incomplete context
        # (confabulation-substrate failure mode per #1865/#2042/...).
        _TRUNC_CAP = 8000
        for m in rows:
            body = m.content or ""
            full_len = len(body)
            truncated = full_len > _TRUNC_CAP
            if truncated:
                body = body[:_TRUNC_CAP]
            messages.append({
                "id": m.id,
                "session_id": m.session_id,
                "role": m.role,
                "content": body + (
                    f"\n…[truncated {_TRUNC_CAP}/{full_len} chars]"
                    if truncated else ""),
                "model": m.model,
                "created_at": (m.created_at.isoformat() + "Z"
                               if m.created_at else None),
                "attachments": attach_by_msg.get(m.id, []),
            })

        return jsonify({
            "ok": True,
            "messages": messages,
            "count": len(messages),
            "window_start": (window_start.isoformat() + "Z"
                             if window_start else None),
            "default_window_hours": default_window_hours,
        })
    finally:
        db.close()


# ============================================================
# POST /sam/cena/db-probe/table-exists — generic schema check
# ============================================================
# Per samai #2272 + Track 3 migration 27 gate-3: confirm the
# whatsapp_messages table was actually dropped on Render Postgres
# (not just silently skipped via the boot-time IF EXISTS guard).
# Generic so it can verify other schema migrations down the road.
#
# Body (JSON):
#   {"table_name": "<str>"}        required, simple-identifier only
#
# Response (JSON):
#   {"ok": true, "exists": true|false,
#    "checked_table": "<str>"}
#
# Auth: X-Cena-Token (same gate as the other db-probes).

import re as _re_table

_VALID_TABLE_NAME_RE = _re_table.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


# ============================================================
# POST /sam/cena/db-probe/ezcater-webhook-recent — Track 5 verify
# ============================================================
# Per cena #2340 follow-up to Track 5 partial-kill: prove the
# /ezcater/webhook endpoint on Render has been receiving real POSTs
# (which closes the gap when ezcater_idle.py IMAP IDLE on AiCk gets
# killed). Reads the tail of WEBHOOK_LOG (jsonl) and returns the
# most-recent N entries with their server-side timestamps.
#
# Body (JSON):
#   {"limit": <int 1..200, default 20>}
#
# Response (JSON):
#   {"ok": true, "log_path": "<str>",
#    "log_exists": true|false,
#    "log_size_bytes": <int>,
#    "entries": [{"ts": "<iso>", "key": "<str>",
#                 "entity_type": "<str>", "entity_id": "<str>",
#                 ...}, ...],
#    "count": <int>}
#
# Auth: X-Cena-Token (same gate as other db-probes; covered by
# /sam/cena/db-probe/ EXEMPT_PREFIXES entry).

@cena_bp.route("/sam/cena/db-probe/ezcater-webhook-recent",
               methods=["POST"])
def cena_db_probe_ezcater_webhook_recent():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    try:
        limit = int(body.get("limit", 20))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit must be int"}), 400
    limit = max(1, min(limit, 200))

    from app.web.ezcater_webhook import WEBHOOK_LOG
    path_str = str(WEBHOOK_LOG)

    if not WEBHOOK_LOG.exists():
        return jsonify({
            "ok": True,
            "log_path": path_str,
            "log_exists": False,
            "log_size_bytes": 0,
            "entries": [],
            "count": 0,
        })

    import json as _json_eh
    try:
        size = WEBHOOK_LOG.stat().st_size
        # Read full file then slice tail — log is jsonl and entries
        # are small (~1KB each); full read is cheap for <50MB.
        # For larger files this could be optimized to seek-from-end,
        # but the webhook log historically stays well under 10MB.
        lines = WEBHOOK_LOG.read_text(encoding="utf-8",
                                      errors="replace").splitlines()
        recent = lines[-limit:]
        entries = []
        for ln in recent:
            ln = ln.strip()
            if not ln:
                continue
            try:
                entries.append(_json_eh.loads(ln))
            except Exception:
                # Malformed line — surface a stub so the caller sees
                # the line existed even if it can't be parsed.
                entries.append({"_parse_error": True,
                                "raw_head": ln[:200]})
        return jsonify({
            "ok": True,
            "log_path": path_str,
            "log_exists": True,
            "log_size_bytes": size,
            "entries": entries,
            "count": len(entries),
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: ezcater-webhook-recent probe failed")
        return jsonify({"ok": False, "error": str(e),
                        "log_path": path_str}), 500


# ============================================================
# POST /sam/cena/db-probe/cena-wake-env — diagnose the f8328b9 hook
# ============================================================
# Per samai #2369 + aick #2371 (C): the wake-on-post hook in
# developer_chat.py reads CENA_GATEWAY_URL + CENA_GATEWAY_TOKEN env
# vars and silently no-ops if either is empty. cena_gateway_err.log
# on AiCk shows zero hits today (2026-05-17) despite many dev-chat
# posts, suggesting one of the env vars is missing on Render or the
# URL is unreachable.
#
# Returns presence flags + URL+token *shape* (length/prefix/suffix)
# WITHOUT exposing the actual values. Also fires a synthetic POST to
# the configured URL and reports the HTTP code so we know whether
# the URL is reachable from inside Render.
#
# Body: {} (no input needed)
# Response (JSON):
#   {"ok": true,
#    "gateway_url_set": true|false,
#    "gateway_url_len": <int>,
#    "gateway_url_prefix": "<first 30 chars>" | null,
#    "gateway_token_set": true|false,
#    "gateway_token_len": <int>,
#    "probe_status": <int>  HTTP code from a HEAD/GET to gateway,
#    "probe_error": "<str>" | null,
#    "probe_ms": <int>}
#
# Auth: X-Cena-Token (same gate as other db-probes).

@cena_bp.route("/sam/cena/db-probe/cena-wake-env",
               methods=["POST"])
def cena_db_probe_cena_wake_env():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    gw_url = (os.getenv("CENA_GATEWAY_URL") or "").strip().rstrip("/")
    gw_tok = (os.getenv("CENA_GATEWAY_TOKEN") or "").strip()

    out = {
        "ok": True,
        "gateway_url_set": bool(gw_url),
        "gateway_url_len": len(gw_url),
        "gateway_url_prefix": (gw_url[:30] if gw_url else None),
        "gateway_token_set": bool(gw_tok),
        "gateway_token_len": len(gw_tok),
        "probe_status": None,
        "probe_error": None,
        "probe_ms": None,
    }

    if gw_url:
        import time as _time_probe
        import urllib.request as _urlreq_probe
        import urllib.error as _urlerr_probe
        # Hit the gateway's /health endpoint (or root if /health 404s)
        # to see if it's reachable from Render. Use HEAD-ish via short
        # GET timeout. /cena/stream itself is POST-only and would
        # consume a Claude turn, which we don't want as a probe side
        # effect.
        for probe_path in ("/health", "/"):
            req = _urlreq_probe.Request(f"{gw_url}{probe_path}",
                                        method="GET")
            t0 = _time_probe.time()
            try:
                with _urlreq_probe.urlopen(req, timeout=8) as r:
                    out["probe_status"] = r.status
                    out["probe_ms"] = int((_time_probe.time() - t0) * 1000)
                    out["probe_path"] = probe_path
                break
            except _urlerr_probe.HTTPError as e:
                out["probe_status"] = e.code
                out["probe_ms"] = int((_time_probe.time() - t0) * 1000)
                out["probe_path"] = probe_path
                # 404 on /health is fine — try /
                if e.code != 404:
                    break
            except Exception as e:  # noqa: BLE001
                out["probe_error"] = f"{type(e).__name__}: {e}"
                out["probe_ms"] = int((_time_probe.time() - t0) * 1000)
                break

    return jsonify(out)


@cena_bp.route("/sam/cena/db-probe/table-exists", methods=["POST"])
def cena_db_probe_table_exists():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    table_name = (body.get("table_name") or "").strip()
    if not _VALID_TABLE_NAME_RE.match(table_name):
        # Strict identifier check so the value can't be a SQL injection
        # vector when passed to to_regclass(). simple-identifier-only.
        return jsonify({"ok": False,
                        "error": "table_name required (simple identifier)"}), 400

    from sqlalchemy import inspect as _sa_insp_te
    db = SessionLocal()
    try:
        insp = _sa_insp_te(db.bind)
        existing = set(insp.get_table_names())
        return jsonify({
            "ok": True,
            "exists": table_name in existing,
            "checked_table": table_name,
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: table-exists probe failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


# ============================================================
# POST /sam/cena/telegram-test-fire — Track 2 test-fire surface
# ============================================================
# Track 2 per cena #2245: "Move the Telegram sender into cenas-ezlive.
# Send one test message to confirm." The app-hosted sender already
# exists (app/web/produce_order.py:419 telegram_send) — this endpoint
# is the gateway-callable trigger so samai's gate-3 can be "one test
# alert lands on Sam's Telegram via the app-hosted path".
#
# Body (JSON):
#   {"text": "<message body>"}            required, 1..1000 chars
#
# Response:
#   {"ok": true|false, "telegram_response": <api dict-or-str>}
#
# Auth: X-Cena-Token (same gate as /sam/cena/log + other /sam/cena/*).
# Not partner-session-eligible — sending Telegram to Sam is a higher-
# privilege action than reading /sam/chat, and the gateway-token path
# is the canonical caller for ops-style triggers.

@cena_bp.route("/sam/cena/telegram-test-fire", methods=["POST"])
def cena_telegram_test_fire():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"ok": False,
                        "error": "text required (non-empty str)"}), 400
    if len(text) > 1000:
        return jsonify({"ok": False,
                        "error": "text too long (max 1000 chars)"}), 400

    try:
        from app.web.produce_order import telegram_send
    except ImportError as e:
        return jsonify({"ok": False,
                        "error": f"telegram_send not importable: {e}"}), 500

    ok, resp = telegram_send(text)
    return jsonify({"ok": bool(ok),
                    "telegram_response": resp if isinstance(resp, dict)
                                         else str(resp)})


# ============================================================
# POST /sam/cena/sam-chat-post — dck (and team) writes to /sam/chat
# ============================================================
# Track 8b per Sam #2236: dck reads /sam/chat via the GET endpoint
# above, and writes back here when summoned. Inserts ONE
# SamChatMessage row with role="dck" (or other non-user/assistant
# role explicitly permitted). Does NOT trigger any model turn — Cena's
# next reply happens when Sam (or dck-on-summon-by-Cena) next POSTs to
# /sam/chat via the normal user pathway.
#
# The "only respond when called" discipline is agent-side (each
# agent's prompt), not server-side. This endpoint just enables the
# write pathway. dck-side wrapper: scripts/post_sam_chat.py.
#
# Body (JSON):
#   {
#     "session_id": <int>,            required
#     "content":    "<str>",          required, non-empty, max 30000ch
#     "role":       "dck",            optional, default "dck"
#   }
#
# Response:
#   {"ok": true, "id": <row_id>, "session_id": <int>,
#    "role": "dck", "created_at": "<ISO>Z"}
#
# Auth: dual-path (X-Cena-Token OR partner session), same gate as
# the read endpoint. role MUST be "dck" — "user"/"assistant"/"system"
# are reserved for the canonical Sam<>Cena flow (writing those via
# this endpoint would bypass the cost+streaming pathway, which is
# not what this endpoint is for).

_VALID_POST_ROLES = {"dck", "cena", "aick"}


@cena_bp.route("/sam/cena/sam-chat-attachment/<int:att_id>", methods=["GET"])
def cena_sam_chat_attachment_get(att_id: int):
    """Serve a persisted /sam/chat attachment as raw binary.

    Per Sam #837 item 5 — the cena gateway / aick polling tail / ck /
    samai can fetch any image Sam attached. Gateway-token gated like
    the other /sam/cena/* endpoints (added to auth EXEMPT_PREFIXES via
    /sam/cena/sam-chat-attachment/ in the next push).
    """
    gate = _require_gateway_token()
    if gate is not None:
        return gate
    import base64 as _b64
    from app.models import SamChatAttachment as _SCA
    db = next(get_db())
    try:
        att = db.get(_SCA, att_id)
        if not att:
            return jsonify({"ok": False, "error": f"attachment {att_id} not found"}), 404
        try:
            data = _b64.b64decode(att.data_base64 or "")
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False,
                            "error": f"base64 decode failed: {type(e).__name__}: {e}"}), 500
        return Response(data, mimetype=att.content_type or "application/octet-stream")
    except Exception as e:  # noqa: BLE001
        logger.exception("cena_sam_chat_attachment_get crashed att_id=%s", att_id)
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        db.close()


@cena_bp.route("/sam/cena/sam-chat-post", methods=["POST"])
def cena_sam_chat_post():
    gate = _require_gateway_token_or_partner()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}

    raw_sid = body.get("session_id")
    try:
        session_id = int(raw_sid)
    except (TypeError, ValueError):
        return jsonify({"ok": False,
                        "error": "session_id required (int)"}), 400

    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return jsonify({"ok": False,
                        "error": "content required (non-empty str)"}), 400
    # Cap inbound dck posts at 30000 chars — generous, matches the
    # ck/samai-style observation comment length, but bounded so a
    # runaway agent can't flood the table.
    if len(content) > 30000:
        return jsonify({"ok": False,
                        "error": "content too long (max 30000 chars)"}), 400

    role = (body.get("role") or "dck").strip()
    if role not in _VALID_POST_ROLES:
        return jsonify({"ok": False,
                        "error": (f"role {role!r} not allowed via this "
                                  f"endpoint; allowed: "
                                  f"{sorted(_VALID_POST_ROLES)}")}), 400
    # Defense-in-depth: also reject if the model layer wouldn't accept it.
    if role not in _VALID_SAM_CHAT_ROLES:
        return jsonify({"ok": False,
                        "error": f"role {role!r} not in model whitelist"}), 400

    db = SessionLocal()
    try:
        sess = db.get(SamChatSession, session_id)
        if sess is None:
            return jsonify({"ok": False,
                            "error": f"session_id={session_id} not found"}), 404

        now = datetime.utcnow()
        row = SamChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            created_at=now,
        )
        db.add(row)
        sess.last_message_at = now
        sess.updated_at = now
        db.commit()
        db.refresh(row)
        return jsonify({
            "ok": True,
            "id": row.id,
            "session_id": session_id,
            "role": row.role,
            "created_at": row.created_at.isoformat() + "Z",
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: sam-chat-post failed")
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


# ============================================================
# GET /sam/cena/dev-chat — dev chat read access for the gateway
# ============================================================
# Returns dev chat messages in chronological order.
# Start-point filter: by default only messages from when Cena first
# posted (earliest author='cena' row), so she doesn't re-read all
# history on every call.
#
# Query params:
#   limit: int, default 30, max 200
#   since: ISO datetime string; default = cena start point
#   include_pre_start: bool (0/1/true/false), default false —
#     if true, ignores the start-point filter
#   author: comma-separated author names to restrict results
#
# Response:
#   {"ok": true, "messages": [...], "count": n,
#    "cena_start_point": "<ISO>Z"}
# Each message: {id, author, body (truncated at 2000 chars),
#                created_at, attachment_count}
#
# Auth: X-Cena-Token header (same token as /sam/cena/log).

@cena_bp.route("/sam/cena/dev-chat", methods=["GET"])
def cena_dev_chat_read():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    try:
        limit = max(1, min(200, int(request.args.get("limit", 30))))
    except (ValueError, TypeError):
        limit = 30

    include_pre_start = (
        request.args.get("include_pre_start", "").lower()
        in ("1", "true", "yes")
    )

    author_raw = request.args.get("author", "").strip()
    author_filter = (
        [a.strip() for a in author_raw.split(",") if a.strip()]
        if author_raw else []
    )

    db = SessionLocal()
    try:
        # Cena start point: earliest message authored by cena, or now.
        cena_start = (
            db.query(func.min(DeveloperChatMessage.created_at))
              .filter(DeveloperChatMessage.author == "cena")
              .scalar()
        ) or datetime.utcnow()

        # Resolve the effective since bound.
        if include_pre_start:
            since = None
        elif (raw_since := request.args.get("since", "")):
            since = _parse_iso_utc(raw_since) or cena_start
        else:
            since = cena_start

        q = db.query(DeveloperChatMessage)
        if since is not None:
            q = q.filter(DeveloperChatMessage.created_at >= since)
        if author_filter:
            q = q.filter(DeveloperChatMessage.author.in_(author_filter))

        # Most recent `limit` messages, returned in chronological order.
        rows = (
            q.order_by(DeveloperChatMessage.created_at.desc(),
                       DeveloperChatMessage.id.desc())
             .limit(limit)
             .all()
        )
        rows.reverse()

        messages = []
        for m in rows:
            body = m.body
            truncated = len(body) > 2000
            if truncated:
                body = body[:2000]
            messages.append({
                "id": m.id,
                "author": m.author,
                "body": body + (" [truncated]" if truncated else ""),
                "created_at": (m.created_at.isoformat() + "Z"
                               if m.created_at else None),
                "attachment_count": len(m.attachments),
            })

        return jsonify({
            "ok": True,
            "messages": messages,
            "count": len(messages),
            "cena_start_point": cena_start.isoformat() + "Z",
        })
    finally:
        db.close()


# ============================================================
# POST /sam/cena/cena-wake-decision-log — telemetry write endpoint
# ============================================================
# Per Sam #2576 6-piece proposal (greenlight 2026-05-17) + cena #2572
# refinements: the AiCk-side watcher (cena_chat_watcher.py) calls
# Haiku-4.5 to classify each dev chat msg as wake/skip/uncertain, then
# POSTs the classifier's verdict + token counts + latency + the
# watcher's own decision into the cena_wake_decisions table here.
#
# In Phase A (shadow mode) the watcher still fires under current rules
# regardless of classifier label; the dashboard reads this table to
# compute would-have-fired vs did-fire delta before we promote the
# classifier to gate the wake.
#
# Body (JSON):
#   {
#     "dev_chat_message_id": <int|null>,    optional FK
#     "author":              "<str|null>",  optional
#     "message_snippet":     "<str|null>",  optional, first 200ch
#
#     "classifier_label":    "wake|skip|uncertain|error",   required
#     "classifier_confidence":      <float|null>,
#     "classifier_reason":          "<str|null>",
#     "classifier_model":           "<str|null>",
#     "classifier_input_tokens":    <int|null>,
#     "classifier_output_tokens":   <int|null>,
#     "classifier_cache_create_tokens": <int|null>,
#     "classifier_cache_read_tokens":   <int|null>,
#     "classifier_latency_ms":      <int|null>,
#
#     "would_fire":          <bool>,        default false
#     "did_fire":             <bool>,        default false
#     "actual_rule_trigger":  "<str|null>",  optional
#     "shadow_mode":          <bool>,        default true
#   }
#
# Response:
#   {"ok": true, "id": <row_id>, "created_at": "<ISO>Z"}
#
# Auth: X-Cena-Token header (same gate as the db-probes).

_VALID_CLASSIFIER_LABELS = {"wake", "skip", "uncertain", "error"}


@cena_bp.route("/sam/cena/cena-wake-decision-log", methods=["POST"])
def cena_wake_decision_log():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}

    label = (body.get("classifier_label") or "").strip()
    if label not in _VALID_CLASSIFIER_LABELS:
        return jsonify({"ok": False,
                        "error": (f"classifier_label required, one of "
                                  f"{sorted(_VALID_CLASSIFIER_LABELS)}")}), 400

    def _opt_int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _opt_float(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _opt_str(v, cap=None):
        if v is None:
            return None
        s = str(v)
        if cap is not None and len(s) > cap:
            s = s[:cap]
        return s

    from app.models import CenaWakeDecision
    db = SessionLocal()
    try:
        row = CenaWakeDecision(
            dev_chat_message_id=_opt_int(body.get("dev_chat_message_id")),
            author=_opt_str(body.get("author"), cap=64),
            message_snippet=_opt_str(body.get("message_snippet"), cap=2000),
            classifier_label=label,
            classifier_confidence=_opt_float(body.get("classifier_confidence")),
            classifier_reason=_opt_str(body.get("classifier_reason"), cap=4000),
            classifier_model=_opt_str(body.get("classifier_model"), cap=64),
            classifier_input_tokens=_opt_int(body.get("classifier_input_tokens")),
            classifier_output_tokens=_opt_int(body.get("classifier_output_tokens")),
            classifier_cache_create_tokens=_opt_int(
                body.get("classifier_cache_create_tokens")),
            classifier_cache_read_tokens=_opt_int(
                body.get("classifier_cache_read_tokens")),
            classifier_latency_ms=_opt_int(body.get("classifier_latency_ms")),
            would_fire=bool(body.get("would_fire", False)),
            did_fire=bool(body.get("did_fire", False)),
            actual_rule_trigger=_opt_str(
                body.get("actual_rule_trigger"), cap=64),
            shadow_mode=bool(body.get("shadow_mode", True)),
            created_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return jsonify({
            "ok": True,
            "id": row.id,
            "created_at": row.created_at.isoformat() + "Z",
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: wake-decision-log failed")
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


# ============================================================
# POST /sam/cena/db-probe/sam-chat-env — diagnose Sam's /sam/chat input lock
# ============================================================
# Per Sam #2690 2026-05-18: Sam reported he cannot text in /sam/chat from
# phone after the Render outage cycle. Diagnostic surface to verify:
#   - SAM_CHAT_USER_ID env var is set on Render
#   - The Sam user row exists in the DB with that ID
#   - Anthropic API key is present (sam_chat_send returns 503 if missing)
# Returns sanitized state — never echo the API key or any PIN.
#
# Auth: X-Cena-Token (same gate as other db-probes).

@cena_bp.route("/sam/cena/db-probe/sam-chat-env", methods=["POST"])
def cena_db_probe_sam_chat_env():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    sam_id_raw = (os.getenv("SAM_CHAT_USER_ID") or "").strip()
    anthropic_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

    out = {
        "ok": True,
        "sam_chat_user_id_set": bool(sam_id_raw),
        "sam_chat_user_id_value": sam_id_raw or None,
        "sam_chat_user_id_is_int": False,
        "anthropic_api_key_set": bool(anthropic_key),
        "anthropic_api_key_len": len(anthropic_key),
        "sam_user_row_exists": None,
        "sam_user_full_name": None,
        "sam_user_active": None,
    }

    sam_id_int = None
    try:
        sam_id_int = int(sam_id_raw) if sam_id_raw else None
        out["sam_chat_user_id_is_int"] = sam_id_int is not None
    except (TypeError, ValueError):
        out["sam_chat_user_id_is_int"] = False

    if sam_id_int is not None:
        db = SessionLocal()
        try:
            from app.models import User
            u = db.get(User, sam_id_int)
            if u is None:
                out["sam_user_row_exists"] = False
            else:
                out["sam_user_row_exists"] = True
                out["sam_user_full_name"] = u.full_name
                out["sam_user_active"] = bool(getattr(u, "is_active", True))
        except Exception as e:  # noqa: BLE001
            logger.exception("cena: sam-chat-env probe failed")
            out["sam_user_row_exists"] = f"error: {type(e).__name__}: {e}"
        finally:
            db.close()

    return jsonify(out)


# ============================================================
# POST /sam/cena/run-seed-test-drivers — one-shot seed trigger
# ============================================================
# Per Sam direct ask 2026-05-18: run scripts/seed_test_drivers.py in
# the LIVE service container (which has prod Postgres DATABASE_URL).
# Render Jobs API doesn't work — those run in ephemeral containers
# without prod env vars (got "no such table: drivers" SQLite default).
#
# Auth: X-Cena-Token (same gate as the other db-probes).
# Body: {} (no params).
# Response: {"ok": true, "stdout": "<markdown table>", "summary": "..."}
#
# Scope: this is a deliberate one-shot trigger for the 10-Test-Driver
# seed. Idempotent (rotates PIN on name+location collision). Soft-
# delete cleanup (active=false) is a separate manual step per cena
# #2685 step 8 — NOT in this trigger.

@cena_bp.route("/sam/cena/run-seed-test-drivers", methods=["POST"])
def cena_run_seed_test_drivers():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    import io
    import contextlib
    import sys as _sys
    import pathlib as _pl

    # Ensure repo root is on sys.path so the script can import app.*
    repo_root = _pl.Path(current_app.root_path).parent
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))

    try:
        from scripts import seed_test_drivers as _seed
    except ImportError as e:
        return jsonify({"ok": False,
                        "error": f"import failed: {e}"}), 500

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _seed.main()
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: seed_test_drivers crashed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "stdout": buf.getvalue()}), 500

    return jsonify({
        "ok": rc == 0,
        "return_code": rc,
        "stdout": buf.getvalue(),
    })


# ============================================================
# POST /sam/cena/run-flip-buildplan-approval — one-shot flip trigger
# ============================================================
# Same pattern as run-seed-test-drivers. Flips the build-plan sample
# approval row from REJECTED → PENDING per Sam #2687.

@cena_bp.route("/sam/cena/run-flip-buildplan-approval", methods=["POST"])
def cena_run_flip_buildplan_approval():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    import io
    import contextlib
    import sys as _sys
    import pathlib as _pl

    repo_root = _pl.Path(current_app.root_path).parent
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))

    try:
        from scripts import flip_buildplan_approval as _flip
    except ImportError as e:
        return jsonify({"ok": False,
                        "error": f"import failed: {e}"}), 500

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _flip.main()
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: flip_buildplan_approval crashed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "stdout": buf.getvalue()}), 500

    return jsonify({
        "ok": rc == 0,
        "return_code": rc,
        "stdout": buf.getvalue(),
    })


@cena_bp.route("/sam/cena/usage-log", methods=["POST"])
def cena_usage_log():
    """Persist one row of per-turn token usage from the cena gateway.

    Body: {model, in_tokens, out_tokens, cache_read_tokens,
           cache_write_tokens, tool_rounds, started_at, finished_at,
           session_id?, message_id?}
    Per Sam /sam/chat session 13 #11 — cost + usage telemetry on cena.
    """
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    from app.models import CenaUsageLog
    body = request.get_json(silent=True) or {}
    from datetime import datetime as _dt
    def _parse_iso(s):
        if not s: return None
        try:
            return _dt.fromisoformat(s.rstrip("Z"))
        except Exception:
            return None

    db = next(get_db())
    try:
        row = CenaUsageLog(
            model=str(body.get("model") or "unknown")[:64],
            in_tokens=int(body.get("in_tokens") or 0),
            out_tokens=int(body.get("out_tokens") or 0),
            cache_read_tokens=int(body.get("cache_read_tokens") or 0),
            cache_write_tokens=int(body.get("cache_write_tokens") or 0),
            tool_rounds=int(body.get("tool_rounds") or 0),
            started_at=_parse_iso(body.get("started_at")) or _dt.utcnow(),
            finished_at=_parse_iso(body.get("finished_at")),
            session_id=body.get("session_id"),
            message_id=body.get("message_id"),
        )
        db.add(row)
        db.commit()
        return jsonify({"ok": True, "id": row.id})
    finally:
        db.close()


# Claude 4 pricing per 1M tokens. Adjust here when Anthropic re-prices.
_PRICE_PER_MTOK = {
    "claude-opus-4-7":   {"in": 15.0,  "out": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4-6": {"in":  3.0,  "out": 15.0, "cache_read": 0.30, "cache_write":  3.75},
    "claude-haiku-4-5":  {"in":  0.80, "out":  4.0, "cache_read": 0.08, "cache_write":  1.00},
    "claude-haiku-4-5-20251001": {"in":  0.80, "out":  4.0, "cache_read": 0.08, "cache_write":  1.00},
}
_DEFAULT_PRICE = _PRICE_PER_MTOK["claude-opus-4-7"]


def _cost_for_row(r) -> float:
    p = _PRICE_PER_MTOK.get(r.model, _DEFAULT_PRICE)
    return (
        (r.in_tokens or 0)         * p["in"]          / 1_000_000.0 +
        (r.out_tokens or 0)        * p["out"]         / 1_000_000.0 +
        (r.cache_read_tokens or 0) * p["cache_read"]  / 1_000_000.0 +
        (r.cache_write_tokens or 0)* p["cache_write"] / 1_000_000.0
    )


@cena_bp.route("/partner/cena-usage", methods=["GET"])
def cena_usage_view():
    """Partner-tier view of cena's daily cost + token usage.

    Sam-facing rollup of CenaUsageLog rows. Shows today / 7-day / 30-day
    spend, breakdown by model and by session.
    """
    from flask import g, render_template
    if (getattr(g, "permission_level", None) or "") not in ("partner", "corporate"):
        abort(403)

    from app.models import CenaUsageLog
    from sqlalchemy import func as _f
    from datetime import datetime as _dt, timedelta as _td
    db = next(get_db())
    try:
        now = _dt.utcnow()
        since_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since_7d = now - _td(days=7)
        since_30d = now - _td(days=30)

        def _agg(since):
            rows = (db.query(CenaUsageLog)
                .filter(CenaUsageLog.started_at >= since).all())
            total_in = sum(r.in_tokens for r in rows)
            total_out = sum(r.out_tokens for r in rows)
            total_cache_read = sum(r.cache_read_tokens for r in rows)
            total_cache_write = sum(r.cache_write_tokens for r in rows)
            total_cost = sum(_cost_for_row(r) for r in rows)
            return {
                "turns": len(rows),
                "in_tokens": total_in,
                "out_tokens": total_out,
                "cache_read_tokens": total_cache_read,
                "cache_write_tokens": total_cache_write,
                "cost_usd": total_cost,
            }

        today = _agg(since_today)
        week = _agg(since_7d)
        month = _agg(since_30d)

        by_model_rows = (db.query(
                CenaUsageLog.model,
                _f.count(CenaUsageLog.id).label("turns"),
                _f.sum(CenaUsageLog.in_tokens).label("in_tok"),
                _f.sum(CenaUsageLog.out_tokens).label("out_tok"),
                _f.sum(CenaUsageLog.cache_read_tokens).label("cache_read"),
                _f.sum(CenaUsageLog.cache_write_tokens).label("cache_write"),
            )
            .filter(CenaUsageLog.started_at >= since_30d)
            .group_by(CenaUsageLog.model)
            .all())
        by_model = []
        for r in by_model_rows:
            p = _PRICE_PER_MTOK.get(r.model, _DEFAULT_PRICE)
            cost = (
                (r.in_tok or 0)*p["in"] + (r.out_tok or 0)*p["out"] +
                (r.cache_read or 0)*p["cache_read"] + (r.cache_write or 0)*p["cache_write"]
            ) / 1_000_000.0
            by_model.append({
                "model": r.model,
                "turns": r.turns,
                "in_tokens": r.in_tok or 0,
                "out_tokens": r.out_tok or 0,
                "cache_read_tokens": r.cache_read or 0,
                "cache_write_tokens": r.cache_write or 0,
                "cost_usd": cost,
            })

        recent = (db.query(CenaUsageLog)
            .order_by(CenaUsageLog.started_at.desc())
            .limit(25).all())
        recent_view = [{
            "id": r.id,
            "started_at": r.started_at,
            "model": r.model,
            "in_tokens": r.in_tokens,
            "out_tokens": r.out_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "cache_write_tokens": r.cache_write_tokens,
            "tool_rounds": r.tool_rounds,
            "session_id": r.session_id,
            "cost_usd": _cost_for_row(r),
        } for r in recent]

        return render_template(
            "cena_usage.html",
            today=today, week=week, month=month,
            by_model=by_model, recent=recent_view,
        )
    finally:
        db.close()


@cena_bp.route("/sam/cena/db-probe/query", methods=["POST"])
def cena_db_probe_query():
    """Read-only SQL surface for the cena gateway's sql_query tool.

    Body: {"sql": "SELECT ...", "limit": 200}
    Refuses anything that isn't a bare SELECT (no semicolons, no
    INSERT/UPDATE/DELETE/CREATE/ALTER/DROP/PRAGMA/ATTACH/etc.). Caps
    result row count to `limit` (max 1000). Returns {ok, rows, columns,
    row_count}. Per Sam /sam/chat session 13 — cena wants a clean SQL
    surface against the live DB instead of needing a per-question
    Python script."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    sql = (body.get("sql") or "").strip()
    if not sql:
        return jsonify({"ok": False, "error": "sql required"}), 400

    upper = sql.upper()
    if not upper.startswith("SELECT") and not upper.startswith("WITH"):
        return jsonify({"ok": False,
                        "error": "only SELECT/WITH queries are allowed"}), 400
    if ";" in sql.rstrip(";"):
        return jsonify({"ok": False,
                        "error": "multi-statement queries not allowed"}), 400
    banned = ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ",
              "DROP ", "TRUNCATE ", "PRAGMA ", "ATTACH ", "DETACH ",
              "REPLACE ", "VACUUM ", "REINDEX ")
    if any(b in upper for b in banned):
        return jsonify({"ok": False,
                        "error": "DDL/DML keywords not allowed"}), 400

    limit = int(body.get("limit") or 200)
    if limit < 1 or limit > 1000:
        limit = 200

    from sqlalchemy import text as _sa_text
    from app.db import SessionLocal as _SL
    db = _SL()
    try:
        try:
            result = db.execute(_sa_text(sql))
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False,
                            "error": f"sql error: {type(e).__name__}: {e}"}), 400
        cols = list(result.keys())
        rows = []
        for i, row in enumerate(result):
            if i >= limit:
                break
            rows.append([_jsonable(v) for v in row])
        return jsonify({
            "ok": True,
            "columns": cols,
            "rows": rows,
            "row_count": len(rows),
            "truncated": (i + 1 > limit) if rows else False,
        })
    finally:
        db.close()


def _jsonable(v):
    from datetime import date, datetime
    from decimal import Decimal
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    return v


# ============================================================
# OQ-5 — cena resolve_* endpoints (spec section 8)
# ============================================================
# Five read-only GET resolvers so Cena can disambiguate an
# ambiguous reference (a name, a date) to concrete record(s)
# before composing a SQL query. Common envelope per spec 8.1;
# token-authed via the existing gateway gate. Column targets
# reconciled to the live cenas_kitchen.db schema (samai spec
# section 8 reconciliation, 2026-05-22). [aick]


def _resolve_rank(query, display):
    """Relevance rank for spec 8.1 ordering: 0 exact, 1 prefix,
    2 substring. Case-insensitive."""
    q = (query or "").strip().lower()
    d = (display or "").strip().lower()
    if not q or not d:
        return 2
    if d == q:
        return 0
    if d.startswith(q):
        return 1
    return 2


def _resolve_envelope(entity, query, candidates):
    """Spec 8.1 resolve envelope: cap candidates at 10, report the
    true total, flag truncation. `candidates` must already be
    relevance-ordered by the caller."""
    total = len(candidates)
    return jsonify({
        "entity": entity,
        "query": query,
        "candidates": candidates[:10],
        "total_matches": total,
        "more_available": total > 10,
    })


def _resolve_missing(param):
    """Spec 8.1 missing-required-param error (HTTP 400)."""
    return jsonify({"error": "missing_query_param", "param": param}), 400


@cena_bp.route("/sam/cena/resolve/employee", methods=["GET"])
def cena_resolve_employee():
    """OQ-5 / spec 8.2 — resolve a person's name to roster record(s):
    case-insensitive substring match on users.full_name and
    drivers.name, each candidate tagged with its source."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate
    name = (request.args.get("name") or "").strip()
    if not name:
        return _resolve_missing("name")
    from sqlalchemy import text as _sa_text
    from app.db import SessionLocal as _SL
    pat = "%" + name.lower() + "%"
    db = _SL()
    try:
        cands = []
        for r in db.execute(_sa_text(
                "SELECT id, full_name, permission_level, store_scope, active "
                "FROM users WHERE full_name IS NOT NULL "
                "AND lower(full_name) LIKE :pat"), {"pat": pat}):
            cands.append({"id": r[0], "display": r[1], "source": "user",
                          "role": r[2], "store_scope": r[3],
                          "active": bool(r[4])})
        for r in db.execute(_sa_text(
                "SELECT id, name, status, active FROM drivers "
                "WHERE name IS NOT NULL AND lower(name) LIKE :pat"),
                {"pat": pat}):
            cands.append({"id": r[0], "display": r[1], "source": "driver",
                          "status": r[2], "active": bool(r[3])})
    finally:
        db.close()
    cands.sort(key=lambda c: (_resolve_rank(name, c["display"]),
                              (c["display"] or "").lower()))
    return _resolve_envelope("employee", {"name": name}, cands)


@cena_bp.route("/sam/cena/resolve/vendor", methods=["GET"])
def cena_resolve_vendor():
    """OQ-5 / spec 8.3 — resolve a vendor name to vendor record(s).
    No vendors master table: returns distinct `vendor` strings from
    produce_price_snapshot and vendor_recent_orders, with a
    most-recent-activity hint for disambiguation."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate
    name = (request.args.get("name") or "").strip()
    if not name:
        return _resolve_missing("name")
    from sqlalchemy import text as _sa_text
    from app.db import SessionLocal as _SL
    pat = "%" + name.lower() + "%"
    db = _SL()
    try:
        merged = {}

        def _bump(vendor, src, dt):
            m = merged.setdefault(vendor, {"id": vendor, "display": vendor,
                                           "source": [], "last_seen": None})
            if src not in m["source"]:
                m["source"].append(src)
            dt = str(dt) if dt is not None else None
            if dt and (m["last_seen"] is None or dt > m["last_seen"]):
                m["last_seen"] = dt

        for r in db.execute(_sa_text(
                "SELECT vendor, max(snapshot_date) FROM produce_price_snapshot "
                "WHERE vendor IS NOT NULL AND lower(vendor) LIKE :pat "
                "GROUP BY vendor"), {"pat": pat}):
            _bump(r[0], "produce_price_snapshot", r[1])
        for r in db.execute(_sa_text(
                "SELECT vendor, max(placed_at) FROM vendor_recent_orders "
                "WHERE vendor IS NOT NULL AND lower(vendor) LIKE :pat "
                "GROUP BY vendor"), {"pat": pat}):
            _bump(r[0], "vendor_recent_orders", r[1])
    finally:
        db.close()
    cands = list(merged.values())
    cands.sort(key=lambda c: (_resolve_rank(name, c["display"]),
                              (c["display"] or "").lower()))
    return _resolve_envelope("vendor", {"name": name}, cands)


@cena_bp.route("/sam/cena/resolve/menu_item", methods=["GET"])
def cena_resolve_menu_item():
    """OQ-5 / spec 8.4 — resolve a menu-item phrase to item(s). No
    menu_items master table: matches order_items.raw_alias (with an
    order-frequency hint) and recipes.name."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate
    q = (request.args.get("q") or "").strip()
    if not q:
        return _resolve_missing("q")
    from sqlalchemy import text as _sa_text
    from app.db import SessionLocal as _SL
    pat = "%" + q.lower() + "%"
    db = _SL()
    try:
        cands = []
        for r in db.execute(_sa_text(
                "SELECT raw_alias, item_key, count(*) AS n FROM order_items "
                "WHERE raw_alias IS NOT NULL AND lower(raw_alias) LIKE :pat "
                "GROUP BY raw_alias, item_key"), {"pat": pat}):
            cands.append({"id": r[1], "display": r[0],
                          "source": "order_items", "order_count": r[2]})
        for r in db.execute(_sa_text(
                "SELECT id, name, category FROM recipes "
                "WHERE name IS NOT NULL AND lower(name) LIKE :pat"),
                {"pat": pat}):
            cands.append({"id": r[0], "display": r[1], "source": "recipes",
                          "category": r[2]})
    finally:
        db.close()
    cands.sort(key=lambda c: (_resolve_rank(q, c["display"]),
                              -(c.get("order_count") or 0),
                              (c["display"] or "").lower()))
    return _resolve_envelope("menu_item", {"q": q}, cands)


@cena_bp.route("/sam/cena/resolve/catering_order", methods=["GET"])
def cena_resolve_catering_order():
    """OQ-5 / spec 8.5 — resolve a delivery date to catering order(s).
    Matches orders.delivery_date (the fully-populated YYYY-MM-DD text
    column; deliver_at is a sparse time-of-day field, not used).
    Optional `client` substring and `location` filters."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate
    date = (request.args.get("date") or "").strip()
    if not date:
        return _resolve_missing("date")
    client = (request.args.get("client") or "").strip()
    location = (request.args.get("location") or "").strip()
    from sqlalchemy import text as _sa_text
    from app.db import SessionLocal as _SL
    sql = ("SELECT id, client, delivery_date, deliver_at, status, headcount, "
           "origin_store_id, total_amount, reported_store FROM orders "
           "WHERE delivery_date = :date")
    params = {"date": date}
    if client:
        sql += " AND client IS NOT NULL AND lower(client) LIKE :client"
        params["client"] = "%" + client.lower() + "%"
    if location:
        sql += (" AND reported_store IS NOT NULL "
                "AND lower(reported_store) LIKE :loc")
        params["loc"] = "%" + location.lower() + "%"
    db = _SL()
    try:
        cands = []
        for r in db.execute(_sa_text(sql), params):
            disp = "#%s - %s" % (r[0], r[1] or "(no client)")
            if r[3]:
                disp += " - " + str(r[3])
            cands.append({"id": r[0], "display": disp, "client": r[1],
                          "delivery_date": r[2], "deliver_at": r[3],
                          "status": r[4], "headcount": r[5],
                          "origin_store_id": r[6],
                          "total_amount": _jsonable(r[7]),
                          "reported_store": r[8]})
    finally:
        db.close()
    cands.sort(key=lambda c: ((c["client"] or "").lower(), c["id"]))
    return _resolve_envelope(
        "catering_order",
        {"date": date, "client": client or None, "location": location or None},
        cands)


@cena_bp.route("/sam/cena/resolve/manager_log", methods=["GET"])
def cena_resolve_manager_log():
    """OQ-5 / spec 8.6 — resolve a date (and optional topic) to manager
    daily-log entries. Matches manager_daily_log on entry_date, with an
    optional topic substring against title/body/subject/issue/module."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate
    date = (request.args.get("date") or "").strip()
    if not date:
        return _resolve_missing("date")
    topic = (request.args.get("topic") or "").strip()
    from sqlalchemy import text as _sa_text
    from app.db import SessionLocal as _SL
    sql = ("SELECT id, entry_date, title, body, subject, issue, module, "
           "priority, store_scope, created_at FROM manager_daily_log "
           "WHERE entry_date = :date")
    params = {"date": date}
    if topic:
        sql += (" AND (lower(coalesce(title,'')) LIKE :t "
                "OR lower(coalesce(body,'')) LIKE :t "
                "OR lower(coalesce(subject,'')) LIKE :t "
                "OR lower(coalesce(issue,'')) LIKE :t "
                "OR lower(coalesce(module,'')) LIKE :t)")
        params["t"] = "%" + topic.lower() + "%"
    db = _SL()
    try:
        cands = []
        for r in db.execute(_sa_text(sql), params):
            label = r[2] or r[4] or r[3] or "(entry)"
            cands.append({"id": r[0], "display": str(label)[:80],
                          "entry_date": _jsonable(r[1]), "title": r[2],
                          "subject": r[4], "issue": r[5], "module": r[6],
                          "priority": r[7], "store_scope": r[8],
                          "created_at": _jsonable(r[9])})
    finally:
        db.close()
    cands.sort(key=lambda c: c["id"])
    return _resolve_envelope("manager_log",
                             {"date": date, "topic": topic or None}, cands)


@cena_bp.route("/sam/cena/run-ingest-vendor-emails", methods=["POST"])
def cena_run_ingest_vendor_emails():
    """Kick a full vendor-email ingest pass — scan orders@ + ezcater@
    inboxes, LLM-parse each matched vendor email, upsert into
    vendor_recent_orders. Idempotent on (vendor, source_email_mid).

    Runs in a BACKGROUND THREAD: the IMAP scan + per-email LLM parse of
    the whole inbox exceeds the synchronous web-request window (the
    request was timing out at the worker → bare 500). The endpoint now
    returns immediately and the ingest finishes server-side; the result
    is logged and the vendor Recent Orders pages reflect the rows once
    the pass completes."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    import io
    import contextlib
    import threading
    import sys as _sys
    import pathlib as _pl

    repo_root = _pl.Path(current_app.root_path).parent
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))

    try:
        from scripts import ingest_vendor_emails as _ing
    except ImportError as e:
        return jsonify({"ok": False, "error": f"import failed: {e}"}), 500

    def _run_ingest():
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = _ing.main()
            logger.info("cena: vendor ingest finished rc=%s — %s",
                        rc, buf.getvalue()[:3000])
        except Exception:  # noqa: BLE001
            logger.exception("cena: vendor ingest (background) crashed — %s",
                             buf.getvalue()[:1000])

    threading.Thread(target=_run_ingest, name="vendor-ingest",
                     daemon=True).start()
    return jsonify({"ok": True, "started": True,
                    "note": "vendor email ingest running in the background; "
                            "rows upsert into vendor_recent_orders as it parses"})


@cena_bp.route("/sam/cena/run-scan-vendor-inbox", methods=["POST"])
def cena_run_scan_vendor_inbox():
    """One-shot scan of orders@cenaskitchen.com IMAP inbox for vendor
    emails. Per Sam /sam/chat #871 — the existing inbox already has
    real vendor emails, no need to wait on Sam to forward samples.
    Returns sender-domain counts + sample subjects/bodies per vendor."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    import io
    import contextlib
    import sys as _sys
    import pathlib as _pl

    repo_root = _pl.Path(current_app.root_path).parent
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))

    try:
        from scripts import scan_vendor_inbox as _scan
    except ImportError as e:
        return jsonify({"ok": False,
                        "error": f"import failed: {e}"}), 500

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _scan.main()
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: scan_vendor_inbox crashed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "stdout": buf.getvalue()}), 500

    return jsonify({
        "ok": rc == 0,
        "return_code": rc,
        "stdout": buf.getvalue(),
    })


@cena_bp.route("/sam/cena/run-probe-ezcater-order", methods=["POST"])
def cena_run_probe_ezcater_order():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    import io
    import contextlib
    import os as _os
    import sys as _sys
    import pathlib as _pl

    repo_root = _pl.Path(current_app.root_path).parent
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))

    body = request.get_json(silent=True) or {}
    order_id = (body.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"ok": False, "error": "order_id required in body"}), 400
    _os.environ["CENA_PROBE_ORDER_ID"] = order_id

    try:
        from scripts import probe_ezcater_order as _probe
    except ImportError as e:
        return jsonify({"ok": False,
                        "error": f"import failed: {e}"}), 500

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _probe.main()
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: probe_ezcater_order crashed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "stdout": buf.getvalue()}), 500

    return jsonify({
        "ok": rc == 0,
        "return_code": rc,
        "stdout": buf.getvalue(),
    })


@cena_bp.route("/sam/cena/run-list-upcoming-orders", methods=["POST"])
def cena_run_list_upcoming_orders():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    import io
    import contextlib
    import sys as _sys
    import pathlib as _pl

    repo_root = _pl.Path(current_app.root_path).parent
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))

    try:
        from scripts import list_upcoming_orders as _list
    except ImportError as e:
        return jsonify({"ok": False,
                        "error": f"import failed: {e}"}), 500

    import os as _os
    body = request.get_json(silent=True) or {}
    if body.get("include_recent"):
        _os.environ["CENA_INCLUDE_RECENT"] = "1"
    else:
        _os.environ.pop("CENA_INCLUDE_RECENT", None)

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _list.main()
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: list_upcoming_orders crashed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "stdout": buf.getvalue()}), 500

    return jsonify({
        "ok": rc == 0,
        "return_code": rc,
        "stdout": buf.getvalue(),
    })


@cena_bp.route("/sam/cena/run-wipe-ezcater-roster", methods=["POST"])
def cena_run_wipe_ezcater_roster():
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    import io
    import contextlib
    import sys as _sys
    import pathlib as _pl

    repo_root = _pl.Path(current_app.root_path).parent
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))

    try:
        from scripts import wipe_ezcater_known_driver as _wipe
    except ImportError as e:
        return jsonify({"ok": False,
                        "error": f"import failed: {e}"}), 500

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _wipe.main()
    except Exception as e:  # noqa: BLE001
        logger.exception("cena: wipe_ezcater_known_driver crashed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "stdout": buf.getvalue()}), 500

    return jsonify({
        "ok": rc == 0,
        "return_code": rc,
        "stdout": buf.getvalue(),
    })


@cena_bp.route("/sam/cena/run-recipes-bulk-insert", methods=["POST"])
def cena_run_recipes_bulk_insert():
    """Bulk-insert Recipe rows from external JSON (Sam #1246 ask: load
    the 33 recipes from the 14 PDFs Sam attached at /sam/chat msg ids
    25-39). Body: {"rows": [{category, name, prep_time, shelf_life,
    spanish_instructions, ingredients, batch_sizes, notes}, ...]}.
    Max 200 rows per request. Idempotent on (category, name) — re-runs
    upsert by name within a category."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    raw_rows = body.get("rows") or []
    if not isinstance(raw_rows, list) or not raw_rows:
        return jsonify({"ok": False, "error": "rows (non-empty list) required"}), 400
    if len(raw_rows) > 200:
        return jsonify({"ok": False, "error": "max 200 rows per request"}), 400

    import json as _json
    from app.db import SessionLocal as _SL
    from app.models import Recipe as _R

    db = _SL()
    inserted = 0
    updated = 0
    skipped = 0
    errors = []
    try:
        for r in raw_rows:
            try:
                name = (r.get("name") or "").strip()[:200]
                category = (r.get("category") or "").strip()[:40].lower()
                if not name or not category:
                    skipped += 1
                    continue
                existing = (db.query(_R)
                              .filter(_R.name == name, _R.category == category)
                              .first())
                ings = r.get("ingredients")
                bsizes = r.get("batch_sizes")
                fields = dict(
                    category=category,
                    name=name,
                    prep_time=(r.get("prep_time") or None),
                    shelf_life=(r.get("shelf_life") or None),
                    spanish_instructions=(r.get("spanish_instructions") or None),
                    ingredients_json=(_json.dumps(ings) if ings else None),
                    batch_sizes_json=(_json.dumps(bsizes) if bsizes else None),
                    notes=(r.get("notes") or None),
                )
                if existing is not None:
                    for k, v in fields.items():
                        if v is not None:
                            setattr(existing, k, v)
                    updated += 1
                else:
                    db.add(_R(**fields))
                    inserted += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {str(e)[:120]}")
                skipped += 1
        db.commit()
        return jsonify({
            "ok": True,
            "inserted": inserted, "updated": updated, "skipped": skipped,
            "errors": errors[:5],
        })
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("cena: run-recipes-bulk-insert crashed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        db.close()


@cena_bp.route("/sam/cena/run-vendor-orders-bulk-insert", methods=["POST"])
def cena_run_vendor_orders_bulk_insert():
    """Bulk-insert vendor_recent_orders rows from external JSON
    (samai parsed orders@cenaskitchen.com inbox externally; #2913 the
    in-process ingest crashes for unclear reasons, this is the bypass
    path). Body: {"rows": [{vendor, store_scope, order_number,
    customer_or_caterer, placed_at_iso, items_json, source_email_mid,
    subject, from_addr, raw_body, kind?}, ...]}. Capped at 500 rows.
    Idempotent on (vendor, source_email_mid) via UPSERT — re-running
    the same JSON dump doesn't duplicate."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    raw_rows = body.get("rows") or []
    if not isinstance(raw_rows, list) or not raw_rows:
        return jsonify({"ok": False, "error": "rows (non-empty list) required"}), 400
    if len(raw_rows) > 500:
        return jsonify({"ok": False, "error": "max 500 rows per request"}), 400

    from app.db import SessionLocal as _SL
    from app.models import VendorRecentOrder as _VRO
    from datetime import datetime as _dt

    db = _SL()
    inserted = 0
    updated = 0
    skipped = 0
    errors = []
    try:
        for r in raw_rows:
            try:
                vendor = (r.get("vendor") or "").strip()[:40]
                if not vendor:
                    skipped += 1
                    continue
                mid = (r.get("source_email_mid") or "").strip()[:80] or None
                # Idempotency lookup: vendor + source_email_mid
                existing = None
                if mid:
                    existing = (db.query(_VRO)
                                  .filter(_VRO.vendor == vendor,
                                          _VRO.source_email_mid == mid)
                                  .first())
                placed_at = None
                pa_str = (r.get("placed_at_iso") or r.get("date") or "").strip()
                if pa_str:
                    try:
                        placed_at = _dt.fromisoformat(pa_str.replace("Z", "+00:00"))
                    except Exception:
                        placed_at = None
                fields = dict(
                    vendor=vendor,
                    store_scope=(r.get("store_scope") or None),
                    order_number=(r.get("order_number") or None),
                    customer_or_caterer=(r.get("customer_or_caterer") or None),
                    placed_at=placed_at,
                    items_json=r.get("items_json") or r.get("items") or None,
                    source_email_mid=mid,
                    subject=(r.get("subject") or None),
                    from_addr=(r.get("from_addr") or None),
                    raw_body=(r.get("raw_body") or None),
                    parse_status=(r.get("kind") or r.get("parse_status") or "parsed"),
                )
                if existing is not None:
                    for k, v in fields.items():
                        if v is not None:
                            setattr(existing, k, v)
                    updated += 1
                else:
                    db.add(_VRO(**fields))
                    inserted += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {str(e)[:120]}")
                skipped += 1
        db.commit()
        return jsonify({
            "ok": True,
            "inserted": inserted, "updated": updated, "skipped": skipped,
            "errors": errors[:5],
        })
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("cena: run-vendor-orders-bulk-insert crashed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        db.close()


@cena_bp.route("/sam/cena/run-archive-and-wipe-dev-chat", methods=["POST"])
def cena_run_archive_and_wipe_dev_chat():
    """One-time bulk archive + wipe of developer_chat per Sam dev chat
    2026-05-19 4:07pm ("remember max 200msgs on this chage. the rest
    consistently archive"). Honors samai #2887 PASS-WITH-CONCERN-FLAG +
    samai #2980 spec: INSERT INTO archive SELECT * FROM live BEFORE the
    DELETE; archive count must equal pre-live count or transaction
    rolls back.

    Returns pre/post counts so the caller can verify the safety
    invariant (archived == pre_live) the way samai gated the
    7d8e390 EzcaterKnownDriver wipe at #2885.
    """
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    from app.models import DeveloperChatMessageArchive as _DCMA
    db = SessionLocal()
    try:
        pre_live = db.query(DeveloperChatMessage).count()
        pre_arch = db.query(_DCMA).count()

        for m in db.query(DeveloperChatMessage).order_by(DeveloperChatMessage.id.asc()).all():
            db.add(_DCMA(
                original_id=m.id,
                created_at=m.created_at,
                author=m.author,
                body=m.body,
            ))
        db.flush()

        post_arch = db.query(_DCMA).count()
        archived = post_arch - pre_arch
        if archived != pre_live:
            db.rollback()
            return jsonify({
                "ok": False,
                "error": f"archive count mismatch: pre_live={pre_live} archived={archived}",
                "pre_live": pre_live,
                "pre_archive": pre_arch,
                "post_archive": post_arch,
            }), 500

        deleted = db.query(DeveloperChatMessage).delete(synchronize_session=False)
        # Wiping every message orphans every attachment row — SQLite does
        # not fire the ON DELETE CASCADE on a bulk delete, so clear
        # attachments explicitly in the same transaction. (The gap behind
        # the 2026-05-19 phantom-image incident: orphaned attachment rows
        # collided onto new messages once message ids reset.)
        from app.models import DeveloperChatAttachment as _DCAtt
        att_deleted = db.query(_DCAtt).delete(synchronize_session=False)
        db.commit()

        post_live = db.query(DeveloperChatMessage).count()
        return jsonify({
            "ok": True,
            "pre_live": pre_live,
            "archived": archived,
            "deleted": deleted,
            "attachments_deleted": att_deleted,
            "post_live": post_live,
            "post_archive_total": post_arch,
        })
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("cena: run-archive-and-wipe-dev-chat crashed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        db.close()


@cena_bp.route("/sam/cena/run-cleanup-orphan-attachments", methods=["POST"])
def cena_run_cleanup_orphan_attachments():
    """Delete dev-chat attachment rows orphaned by the archive-and-wipe.
    The wipe deleted messages but SQLite did not fire the ON DELETE
    CASCADE on the bulk delete, so pre-wipe attachment rows survived.
    After the message-id reset they collide onto new messages by
    message_id (the 2026-05-19 phantom-image incident).

    Orphans = attachments created before the oldest surviving (post-
    wipe) message. That boundary is self-computed from live data — no
    hardcoded id or timestamp cutoff. Returns pre/post counts.
    """
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    from app.models import DeveloperChatAttachment as _DCAtt
    db = SessionLocal()
    try:
        oldest_live = db.query(func.min(DeveloperChatMessage.created_at)).scalar()
        if oldest_live is None:
            return jsonify({"ok": False,
                            "error": "no live messages — cannot compute cutoff"}), 400
        pre = db.query(_DCAtt).count()
        orphan_ids = [a.id for a in
                      db.query(_DCAtt).filter(_DCAtt.created_at < oldest_live).all()]
        deleted = 0
        if orphan_ids:
            deleted = db.query(_DCAtt).filter(
                _DCAtt.id.in_(orphan_ids)
            ).delete(synchronize_session=False)
        db.commit()
        post = db.query(_DCAtt).count()
        return jsonify({
            "ok": True,
            "cutoff": oldest_live.isoformat() if oldest_live else None,
            "pre": pre,
            "deleted": deleted,
            "post": post,
        })
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("cena: run-cleanup-orphan-attachments crashed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        db.close()


@cena_bp.route("/sam/cena/run-cleanup-dev-chat", methods=["POST"])
def cena_run_cleanup_dev_chat():
    """Delete developer_chat rows by explicit ID list. Sam #1047 + cena
    #1054/#1056 — sweep the agent auto-noise (samai LIGHT-GATE PASS posts,
    aick raw-push relays) from dev chat. IDs come from the caller (cena
    eyeballs the preview list first), capped at 200 per request."""
    gate = _require_gateway_token()
    if gate is not None:
        return gate

    body = request.get_json(silent=True) or {}
    raw_ids = body.get("ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"ok": False, "error": "ids (non-empty list) required"}), 400
    if len(raw_ids) > 200:
        return jsonify({"ok": False, "error": "max 200 ids per request"}), 400
    try:
        ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "ids must be integers"}), 400

    from sqlalchemy import delete as _sa_delete
    from app.db import SessionLocal as _SL
    from app.models import DeveloperChatMessage as _DCM
    db = _SL()
    try:
        stmt = _sa_delete(_DCM).where(_DCM.id.in_(ids))
        result = db.execute(stmt)
        db.commit()
        return jsonify({
            "ok": True,
            "deleted": result.rowcount,
            "requested": len(ids),
        })
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("cena: run-cleanup-dev-chat crashed")
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        db.close()


def install(app):
    """Register the cena blueprint."""
    app.register_blueprint(cena_bp)
