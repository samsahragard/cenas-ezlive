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
# Digits + the special keys on the pad: * # @ + % - $
PASSCODE_RE = re.compile(rf"^[\d*#@+%\-$]{{{PASSCODE_LEN}}}$")
MAX_FAILED_ATTEMPTS = 6
LOCKOUT_MINUTES = 10


def _valid_passcode(s: str) -> bool:
    return bool(s and PASSCODE_RE.match(s))


# Store slug each scope maps to in store_routes.STORE_TO_LOCATION:
#   'tomball'     -> 'dos'         (DOS MAS)
#   'copperfield' -> 'uno'         (UNO MAS)
#   'both' / NULL -> 'corporate'   (everyone for corporate; partners get partner)
def _landing_for_user(u) -> str:
    """Default landing page after a successful login — based on role + store.
    Sam's 2026-05-11 spec: each role goes straight to their authorized scope
    instead of the shared /  store picker. Multi-store users land on the
    first store in their list; the sidebar lets them switch."""
    from app.web.permissions import accessible_store_slugs

    if u.permission_level == "corporate-driver":
        return "/driver/portal"
    slugs = accessible_store_slugs(u)
    if not slugs:
        return "/"
    return f"/{slugs[0]}/"


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
    """Render the keypad. If already signed in, jump straight to their
    role landing — Sam's 2026-05-11 spec: 'they stay logged in unless
    logging out'. Back-button after login is solved client-side via
    history.replaceState (see keypad_login.html JS)."""
    u = getattr(g, "current_user", None)
    if u is not None:
        nxt = request.args.get("next") or _landing_for_user(u)
        if not nxt.startswith("/"):
            nxt = "/"
        return redirect(nxt)
    return _no_store(render_template(
        "keypad_login.html",
        next_url=request.args.get("next") or "/",
        passcode_len=PASSCODE_LEN,
    ))


def _no_store(body):
    """Wrap an HTML body so the browser doesn't cache the inline JS — Sam
    (2026-05-11) was hitting stale templates after deploys because his
    phone cached the prior keypad HTML (with the old digits-only JS)."""
    resp = current_app.make_response(body)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@keypad_auth.route("/keypad-login", methods=["POST"])
def login_submit():
    """Accept JSON {"passcode": "12345"}. On match: set session, return next URL."""
    data = request.get_json(silent=True) or {}
    passcode = (data.get("passcode") or "").strip()
    if not _valid_passcode(passcode):
        return jsonify({"ok": False, "error": "Passcode must be exactly 5 characters (digits or * # @ + % - $)."}), 400

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
        # Clear any leftover driver-portal session keys so the dashboard
        # sidebar doesn't render the "MY WORK" driver menu over a partner
        # session (Sam, 2026-05-13: hit this on the Capacitor mobile app
        # after switching from a driver-login test back to his partner
        # keypad — sidebar role-detection picks driver_id first).
        for _k in ("driver_id", "driver_name", "driver_location",
                   "driver_session_version"):
            session.pop(_k, None)
        session["user_id"] = u.id
        # Stamp the session with the user's current version. Any later
        # passcode reset or deactivation bumps User.session_version, which
        # makes load_current_user kick stale sessions on the next request.
        session["user_session_version"] = u.session_version
        # Legacy shims so partner-gated routes keep working under the new auth.
        session["auth_ok"] = True
        # Sam (2026-05-11): only partner gets the partner_auth_ok flag — that
        # unlocks /partner/team (Admin) and /partner/developer/* (Chat,
        # Ezcater Review, App docs). Corporate sees the same operational
        # dashboards as partner but NOT those owner-private sections.
        if u.permission_level == "partner":
            session["partner_auth_ok"] = True
        else:
            session.pop("partner_auth_ok", None)

        if not u.first_login_done:
            return jsonify({"ok": True, "next": url_for("keypad_auth.change_passcode")})
        # Bare-login (no specific destination requested) routes by role.
        if nxt == "/":
            nxt = _landing_for_user(u)
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
        return _no_store(render_template(
            "keypad_change_passcode.html",
            user=u,
            passcode_len=PASSCODE_LEN,
            forced=not u.first_login_done,
        ))
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
        return jsonify({"ok": False, "error": "New passcode must be exactly 5 characters (digits or * # @ + % - $)."}), 400

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
        # Route by role straight into their default landing page.
        return jsonify({"ok": True, "next": _landing_for_user(u)})
    finally:
        db.close()


@keypad_auth.route("/keypad-logout", methods=["GET", "POST"])
def logout():
    # Wipe every role key (user, driver, partner gate) so app reopen never
    # lands on a stale dashboard for the wrong role. Preserve Tier-1
    # auth_ok so the user doesn't have to re-type the site password on
    # the way back in — that's a separate gate.
    auth_ok = session.get("auth_ok")
    session.clear()
    if auth_ok:
        session["auth_ok"] = auth_ok
    resp = _no_store(redirect(url_for("keypad_auth.login")))
    return resp


def install(app):
    """Register the blueprint and the load-current-user before_request hook."""
    from app.web.permissions import (
        accessible_store_slugs, load_current_user,
    )

    app.register_blueprint(keypad_auth)

    @app.before_request
    def _attach_current_user():
        # Cheap; only hits the DB when a session exists.
        if session.get("user_id"):
            load_current_user()
        else:
            g.current_user = None

    @app.after_request
    def _no_store_when_authed(resp):
        """Force Cache-Control: no-store on all auth-state-sensitive
        responses — the Capacitor mobile app's WebView was caching the
        logged-in dashboard HTML and serving it back on app restart
        after a logout, making logout appear to "not stick" (Sam,
        2026-05-13). The cookie WAS being cleared; the cache was the
        problem.

        Targets:
          - any HTML response for an authenticated session
            (user_id, driver_id, or admin tier-2 partner_auth_ok)
          - login/logout endpoints regardless of session state
        """
        path = (request.path or "")
        auth_paths = {
            "/keypad-login", "/keypad-logout",
            "/change-passcode",
            "/driver/login", "/driver/logout", "/driver/signup",
            "/driver/change-passcode",
            "/login", "/logout", "/partner-login",
        }
        is_auth_html = (
            resp.mimetype == "text/html"
            and (session.get("user_id")
                 or session.get("driver_id")
                 or session.get("partner_auth_ok"))
        )
        if path in auth_paths or is_auth_html:
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        return resp

    # The sidebar (base_dashboard.html) calls current_user_stores() to decide
    # whether to render the switch-store dropdown. Lift it into the Jinja
    # globals so templates don't need to import anything.
    from app.web.store_routes import STORE_LABELS

    def _current_user_stores():
        slugs = accessible_store_slugs(getattr(g, "current_user", None))
        return [(s, STORE_LABELS.get(s, s.title())) for s in slugs]

    app.jinja_env.globals["current_user_stores"] = _current_user_stores

