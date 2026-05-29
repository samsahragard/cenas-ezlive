"""PERMISSIONS admin page backend (Sam #1676) - PARTNER-ONLY (Sam #1694).

Roster / load / save for per-user, per-store permission overrides. The catalog
(app/services/permission_catalog.py) + the roster drive ck's frontend.
ezCater driver perms are coded-locked + NOT managed here.

Endpoints (all partner-only, JSON, no CSRF layer in this app):
  GET  /partner/developer/permissions/roster          -> {users:[{user_id,name,role,stores[]}]}
  GET  /partner/developer/permissions/user/<user_id>  -> {user_id,name,role,stores[],overrides:{store:{key:mode}}}
  POST /partner/developer/permissions/save  body {user_id,role,stores[],overrides:{store:{key:allow|deny|inherit}}}
"""
from __future__ import annotations

import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, g, abort

from app.db import SessionLocal
from app.models import User, UserPermissionOverride, UserAuditLog
from app.services.permissions import requires_permission
from app.services.permission_catalog import ROLES, STORES, all_permission_keys

log = logging.getLogger(__name__)

permissions_admin = Blueprint("permissions_admin", __name__)

_VALID_PERM_KEYS = set(all_permission_keys())
_VALID_ROLE_KEYS = {r["key"] for r in ROLES}
_VALID_STORE_KEYS = {s["key"] for s in STORES}


def _require_partner():
    """Partner-ONLY hard gate (Sam #1694) - belt-and-suspenders on top of the
    perms.* tag gate, so this surface is never reachable by corporate/anyone."""
    u = getattr(g, "current_user", None)
    if not (u is not None and getattr(u, "permission_level", None) == "partner"):
        abort(403)


def _scope_to_stores(scope):
    if scope in (None, "both"):
        return ["copperfield", "tomball"]
    return [scope] if scope in _VALID_STORE_KEYS else []


def _stores_to_scope(stores):
    s = set(stores or [])
    if {"copperfield", "tomball"} <= s:
        return "both"
    if "copperfield" in s:
        return "copperfield"
    if "tomball" in s:
        return "tomball"
    return None


@permissions_admin.route("/partner/developer/permissions/roster", methods=["GET"])
@requires_permission("developer.manage_permissions")
def roster():
    _require_partner()
    db = SessionLocal()
    try:
        users = (db.query(User)
                 .filter(User.active.is_(True))
                 .order_by(User.full_name)
                 .all())
        out = [{"user_id": u.id, "name": u.full_name, "role": u.permission_level,
                "stores": _scope_to_stores(u.store_scope)} for u in users]
    finally:
        db.close()
    return jsonify({"users": out})


@permissions_admin.route("/partner/developer/permissions/user/<int:user_id>", methods=["GET"])
@requires_permission("developer.manage_permissions")
def load_user(user_id):
    _require_partner()
    db = SessionLocal()
    try:
        u = db.get(User, user_id)
        if u is None:
            return jsonify({"ok": False, "error": "user not found"}), 404
        overrides: dict = {}
        rows = (db.query(UserPermissionOverride)
                .filter(UserPermissionOverride.user_id == user_id).all())
        for r in rows:
            overrides.setdefault(r.store_key, {})[r.perm_key] = r.mode
        resp = {"user_id": u.id, "name": u.full_name, "role": u.permission_level,
                "stores": _scope_to_stores(u.store_scope), "overrides": overrides}
    finally:
        db.close()
    return jsonify(resp)


@permissions_admin.route("/partner/developer/permissions/save", methods=["POST"])
@requires_permission("developer.manage_permissions")
def save():
    _require_partner()
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    role = data.get("role")
    stores = data.get("stores") or []
    overrides = data.get("overrides") or {}

    if not isinstance(user_id, int):
        return jsonify({"ok": False, "error": "user_id (int) required"}), 400
    if role not in _VALID_ROLE_KEYS:
        return jsonify({"ok": False, "error": "invalid role: %s" % role}), 400
    for sk in stores:
        if sk not in _VALID_STORE_KEYS:
            return jsonify({"ok": False, "error": "invalid store: %s" % sk}), 400

    saved = 0
    db = SessionLocal()
    try:
        u = db.get(User, user_id)
        if u is None:
            return jsonify({"ok": False, "error": "user not found"}), 404
        before = "%s|%s" % (u.permission_level, u.store_scope)
        u.permission_level = role
        u.store_scope = _stores_to_scope(stores)
        # Replace this user's overrides wholesale (inherit == absent row).
        (db.query(UserPermissionOverride)
           .filter(UserPermissionOverride.user_id == user_id).delete())
        actor = getattr(g, "current_user", None)
        for store_key, perms in (overrides or {}).items():
            if store_key not in _VALID_STORE_KEYS:
                continue
            for perm_key, mode in (perms or {}).items():
                if perm_key not in _VALID_PERM_KEYS or mode not in ("allow", "deny"):
                    continue  # unknown key or 'inherit' -> no row
                db.add(UserPermissionOverride(
                    user_id=user_id, store_key=store_key, perm_key=perm_key,
                    mode=mode, created_at=datetime.utcnow(),
                    created_by=(actor.id if actor else None),
                ))
                saved += 1
        after = "%s|%s" % (u.permission_level, u.store_scope)
        try:
            db.add(UserAuditLog(
                target_user_id=u.id, target_label=u.full_name,
                actor_user_id=(actor.id if actor else None),
                actor_label=(actor.full_name if actor else None),
                action="permissions_save", before_value=before, after_value=after,
                details="overrides=%d" % saved,
                ip=(request.remote_addr or None) if request else None,
            ))
        except Exception:
            log.exception("permissions_save audit row failed (non-fatal)")
        db.commit()
    except Exception:
        db.rollback()
        log.exception("permissions_save failed")
        return jsonify({"ok": False, "error": "save failed"}), 500
    finally:
        db.close()
    return jsonify({"ok": True, "saved": saved})
