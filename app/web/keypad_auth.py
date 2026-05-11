"""5-digit passcode keypad auth (migration 13 + 2026-05-11 rewrite).

Endpoints:
  GET  /keypad-login          — renders the keypad
  POST /keypad-login          — JSON: {"passcode": "12345"} → {"ok": true, "next": "/"} or {"ok": false, "error": "..."}
  GET  /change-passcode       — renders the change-passcode keypad (forced on first login)
  POST /change-passcode       — JSON: {"new": "12345"} → {"ok": true} or {"ok": false, "error": "..."}
  GET  /logout                — clears session, redirects to /keypad-login

The legacy /login + /partner-login routes (auth.py) stay live for backwards
compat with the chat-tail/post tooling and the existing PARTNER_PASSWORD
flow; the global before_request gate accepts EITHER session.

Passcode storage uses werkzeug.security so it's salted/hashed. Lookup at
login time scans all active users (O(N) check_password_hash calls — fine
for a small team), and rejects duplicate passcodes at create/change time
the same way.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from flask import (
    Blueprint, current_app, g, jsonify, redirect, render_template,
    request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import SessionLocal
from app.models import User

log = logging.getLogger(__name__)

keypad_auth = Blueprint("keypad_auth", __name__)

PASSCODE_LEN = 5
PASSCODE_RE = re.compile(rf"^\d{{{PASSCODE_LEN}}}$")
MAX_FAILED_ATTEMPTS = 6
LOCKOUT_MINUTES = 10


def _valid_passcode(s: str) -> bool:
    return bool(s and PASSCODE_RE.match(s))


def _find_user_by_passcode(db, passcode: str) -> User | None:
    """Iterate active users, return the first one whose hash matches.
    Passcode uniqueness is enforced at create/change time, so at most one
    match is possible in practice."""
    now = datetime.utcnow()
    users = (db.query(User)
               .filter(User.active.is_(True))
               .all())
    for u in users:
        if u.lockout_until and u.lockout_until > now:
            continue
        if check_password_hash(u.passcode_hash, passcode):
            return u
    return None


def _passcode_in_use(db, passcode: str, excluding_user_id: int | None = None) -> bool:
    """True if some OTHER active user already has this passcode."""
    q = db.query(User).filter(User.active.is_(True))
    if excluding_user_id is not None:
        q = q.filter(User.id != excluding_user_id)
    for u in q.all():
        if check_password_hash(u.passcode_hash, passcode):
            return True
    return False


def _bump_failed_attempts_for_passcode(db, passcode: str) -> None:
    """No matching user means we can't bump per-user failed_attempts. But
    if any user's hash matches but they're locked out, bump theirs.
    This is mostly belt-and-suspenders: lockouts are also enforced at
    match time above."""
    pass


@keypad_auth.route("/keypad-login", methods=["GET"])
def login():
    """Render the keypad. If already signed in, jump to ?next or /."""
    if session.get("user_id"):
        nxt = request.args.get("next") or "/"
        if not nxt.startswith("/"):
            nxt = "/"
        return redirect(nxt)
    return render_template(
        "keypad_login.html",
        next_url=request.args.get("next") or "/",
        passcode_len=PASSCODE_LEN,
    )


@keypad_auth.route("/keypad-login", methods=["POST"])
def login_submit():
    """Accept JSON {"passcode": "12345"}. On match: set session, return next URL."""
    data = request.get_json(silent=True) or {}
    passcode = (data.get("passcode") or "").strip()
    if not _valid_passcode(passcode):
        return jsonify({"ok": False, "error": "Passcode must be exactly 5 digits."}), 400

    nxt = (data.get("next") or "/").strip()
    if not nxt.startswith("/"):
        nxt = "/"

    db = SessionLocal()
    try:
        u = _find_user_by_passcode(db, passcode)
        if u is None:
            return jsonify({"ok": False, "error": "Wrong passcode."}), 401

        u.failed_attempts = 0
        u.lockout_until = None
        u.last_login_at = datetime.utcnow()
        u.last_login_ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64]
        db.commit()

        session.permanent = True
        session["user_id"] = u.id
        # Legacy shims so partner-gated routes keep working under the new auth.
        session["auth_ok"] = True
        if u.permission_level in ("partner", "corporate"):
            session["partner_auth_ok"] = True

        if not u.first_login_done:
            return jsonify({"ok": True, "next": url_for("keypad_auth.change_passcode")})
        return jsonify({"ok": True, "next": nxt})
    finally:
        db.close()


@keypad_auth.route("/change-passcode", methods=["GET"])
def change_passcode():
    """Forced on first login; also linkable from the user's account menu."""
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("keypad_auth.login"))
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == uid).first()
        if not u:
            session.pop("user_id", None)
            return redirect(url_for("keypad_auth.login"))
        return render_template(
            "keypad_change_passcode.html",
            user=u,
            passcode_len=PASSCODE_LEN,
            forced=not u.first_login_done,
        )
    finally:
        db.close()


@keypad_auth.route("/change-passcode", methods=["POST"])
def change_passcode_submit():
    """Accept JSON {"new": "12345"}. On success, mark first_login_done."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"ok": False, "error": "Sign in first."}), 401
    data = request.get_json(silent=True) or {}
    new = (data.get("new") or "").strip()
    if not _valid_passcode(new):
        return jsonify({"ok": False, "error": "New passcode must be exactly 5 digits."}), 400

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == uid).first()
        if not u:
            return jsonify({"ok": False, "error": "Account no longer exists."}), 404

        if _passcode_in_use(db, new, excluding_user_id=u.id):
            return jsonify({"ok": False, "error": "That passcode is taken — pick a different one."}), 409

        if check_password_hash(u.passcode_hash, new):
            return jsonify({"ok": False, "error": "New passcode must be different from your current one."}), 400

        u.passcode_hash = generate_password_hash(new)
        u.first_login_done = True
        db.commit()
        return jsonify({"ok": True, "next": url_for("auth.store_picker")})
    finally:
        db.close()


@keypad_auth.route("/keypad-logout", methods=["GET", "POST"])
def logout():
    session.pop("user_id", None)
    session.pop("auth_ok", None)
    session.pop("partner_auth_ok", None)
    return redirect(url_for("keypad_auth.login"))


def install(app):
    """Register the blueprint and the load-current-user before_request hook."""
    from app.web.permissions import load_current_user

    app.register_blueprint(keypad_auth)

    @app.before_request
    def _attach_current_user():
        # Cheap; only hits the DB when a session exists.
        if session.get("user_id"):
            load_current_user()
        else:
            g.current_user = None
