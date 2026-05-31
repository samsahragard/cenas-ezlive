"""PERMISSIONS admin page backend - PARTNER-ONLY. POSITION-based (Sam #2426/
#2435): the partner toggles each catalog permission ON/OFF for a (position,
store); a row in PositionPermission = ON, no row = OFF. Load/save are BY
(position, store) - there is NO per-user data on this page (Sam dropped the
employee dropdown). A person's effective perms at login = the UNION of their
positions' ON-perms at the active store (resolved in enforcement, not here).
ezCater driver perms are coded-locked + NOT managed here.

Endpoints (partner-only, JSON):
  GET  /partner/developer/permissions/position/<position_key>/<store_key>
       -> {ok, position_key, store_key, on_perms:[perm_key,...]}
  POST /partner/developer/permissions/position/save
       body {position_key, store_key, perms:[perm_key,...]}  (wholesale replace)
       -> {ok, saved:N}
"""
from __future__ import annotations

import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, g, abort

from app.db import SessionLocal
from app.models import PositionPermission, UserAuditLog
from app.services.permissions import requires_permission
from app.services.permission_catalog import ROLES, STORES, all_permission_keys

log = logging.getLogger(__name__)

permissions_admin = Blueprint("permissions_admin", __name__)

_VALID_PERM_KEYS = set(all_permission_keys())
_VALID_POSITION_KEYS = {r["key"] for r in ROLES}
_VALID_STORE_KEYS = {s["key"] for s in STORES}


def _require_partner():
    """Partner-ONLY hard gate (Sam #1694) - belt-and-suspenders on top of the
    developer.manage_permissions tag, so this surface is never reachable by
    corporate / anyone else."""
    u = getattr(g, "current_user", None)
    if not (u is not None and getattr(u, "permission_level", None) == "partner"):
        abort(403)


@permissions_admin.route(
    "/partner/developer/permissions/position/<position_key>/<store_key>",
    methods=["GET"])
@requires_permission("developer.manage_permissions")
def load_position(position_key, store_key):
    """The ON permission keys for a (position, store)."""
    _require_partner()
    if position_key not in _VALID_POSITION_KEYS:
        return jsonify({"ok": False, "error": "invalid position: %s" % position_key}), 400
    if store_key not in _VALID_STORE_KEYS:
        return jsonify({"ok": False, "error": "invalid store: %s" % store_key}), 400
    db = SessionLocal()
    try:
        rows = (db.query(PositionPermission.perm_key)
                .filter(PositionPermission.position_key == position_key,
                        PositionPermission.store_key == store_key).all())
        on_perms = sorted({r[0] for r in rows if r[0] in _VALID_PERM_KEYS})
    finally:
        db.close()
    return jsonify({"ok": True, "position_key": position_key,
                    "store_key": store_key, "on_perms": on_perms})


@permissions_admin.route("/partner/developer/permissions/position/save", methods=["POST"])
@requires_permission("developer.manage_permissions")
def save_position():
    """Wholesale-replace the ON permission set for a (position, store)."""
    _require_partner()
    data = request.get_json(silent=True) or {}
    position_key = data.get("position_key")
    store_key = data.get("store_key")
    perms = data.get("perms")
    if position_key not in _VALID_POSITION_KEYS:
        return jsonify({"ok": False, "error": "invalid position: %s" % position_key}), 400
    if store_key not in _VALID_STORE_KEYS:
        return jsonify({"ok": False, "error": "invalid store: %s" % store_key}), 400
    if not isinstance(perms, list):
        return jsonify({"ok": False, "error": "perms must be a list"}), 400
    clean = [p for p in perms if p in _VALID_PERM_KEYS]   # drop unknown keys silently
    saved = 0
    db = SessionLocal()
    try:
        # Wholesale replace: clear this (position, store)'s rows, insert the ON set.
        (db.query(PositionPermission)
           .filter(PositionPermission.position_key == position_key,
                   PositionPermission.store_key == store_key).delete())
        actor = getattr(g, "current_user", None)
        for pk in dict.fromkeys(clean):       # de-dupe, preserve order
            db.add(PositionPermission(
                position_key=position_key, store_key=store_key, perm_key=pk,
                created_at=datetime.utcnow(),
                created_by=(actor.id if actor else None)))
            saved += 1
        try:
            db.add(UserAuditLog(
                target_user_id=None,
                target_label="position:%s@%s" % (position_key, store_key),
                actor_user_id=(actor.id if actor else None),
                actor_label=(actor.full_name if actor else None),
                action="position_perms_save", before_value=None,
                after_value="%d perms ON" % saved, details="store=%s" % store_key,
                ip=(request.remote_addr or None) if request else None))
        except Exception:
            log.exception("position_perms_save audit row failed (non-fatal)")
        db.commit()
    except Exception:
        db.rollback()
        log.exception("position_perms_save failed")
        return jsonify({"ok": False, "error": "save failed"}), 500
    finally:
        db.close()
    return jsonify({"ok": True, "saved": saved})
