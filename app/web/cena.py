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
    AccessRequest,
    CenaActionLog,
    Driver,
    SamChatMessage,
    SamChatSession,
    User,
    UserAuditLog,
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


def install(app):
    """Register the cena blueprint."""
    app.register_blueprint(cena_bp)
