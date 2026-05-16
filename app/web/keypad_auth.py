"""Unified phone + 5-digit passcode keypad auth (Sam #1591 — 2026-05-15).

Endpoints:
  GET  /keypad-login          — renders the unified two-screen pad (phone → PIN)
  POST /keypad-login          — JSON: {"phone": "...", "pin": "..."} → routes to right dashboard
                                (Driver-table match first, User-table by phone second,
                                 User-table by passcode-only as legacy fallback for users
                                 without a phone set in the DB)
  GET  /change-passcode       — renders the change-passcode keypad (forced on first login)
  POST /change-passcode       — JSON: {"new": "12345"} → {"ok": true} or {"ok": false, "error": "..."}
  GET  /keypad-logout         — clears session, redirects to /keypad-login

Pre-Sam-#1591 history: /keypad-login was passcode-only against User.passcode_hash;
/driver/login was phone+pin against Driver.passcode_hash. Two forms, two
post-logout destinations, and a confusing UX where a driver who logged out
landed on the partner-keypad page (Sam, 2026-05-15: "it automatically goes
to the password screen for the Partners, not the passcode").

Now: ONE unified entry. Phone is the first factor (disambiguates which row
to bcrypt against, makes login O(1) instead of O(N) for the common path).
Driver-table lookup wins on phone collision (drivers are the higher-volume
login source per samai #1601 default). User-by-phone is the next-tier match;
user-by-passcode-only is the legacy fallback so partners/managers who
predate the Sam #1591 phone-required convention can still sign in.

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
    """Render the unified phone+PIN keypad. If already signed in (either
    role), jump straight to the dashboard.

    Sam #1591 (2026-05-15): the partner-keypad and driver-keypad pages
    are unified here — `driver_keypad_login.html` is the canonical login
    template for EVERYONE (phone screen 1 → PIN screen 2). /driver/login
    GET now redirects here too so there's a single entry point."""
    u = getattr(g, "current_user", None)
    if u is not None:
        nxt = request.args.get("next") or _landing_for_user(u)
        if not nxt.startswith("/"):
            nxt = "/"
        return redirect(nxt)
    # Driver session takes a driver to their portal, NOT this login page.
    # Prevents the post-driver-logout symptom where the partner-keypad
    # rendered over an active driver session.
    if session.get("driver_id"):
        return redirect("/driver/logs")
    return _no_store(render_template(
        "driver_keypad_login.html",
        next_url=request.args.get("next") or "/",
        passcode_len=PASSCODE_LEN,
        submit_url=url_for("keypad_auth.login_submit"),
        signup_url="/driver/signup",
        prefill_phone="",
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
    """Unified login (Sam #1591). Accept JSON {"phone": "...", "pin": "..."}.
    Driver-table by phone -> User-table by phone -> User-table by passcode-only
    (legacy fallback for users that predate the phone-required convention).

    Backward-compat: the field name `passcode` is still accepted as an alias
    for `pin` so old client code that POSTs {passcode: ...} doesn't break.
    Sets the appropriate session keys for whichever role matched and routes
    to that role's landing page.
    """
    from app.services.ezcater_known_drivers_seed import normalize_phone
    from app.models import Driver
    from werkzeug.security import check_password_hash as _check

    data = request.get_json(silent=True) or {}
    phone_raw = (data.get("phone") or "").strip()
    passcode = (data.get("pin") or data.get("passcode") or "").strip()
    if not _valid_passcode(passcode):
        return jsonify({"ok": False, "error": "Phone or passcode doesn't match."}), 401

    nxt = (data.get("next") or "/").strip()
    if not nxt.startswith("/"):
        nxt = "/"

    digits = normalize_phone(phone_raw) if phone_raw else ""
    now = datetime.utcnow()

    db = SessionLocal()
    try:
        # ===== Path 1: Driver lookup by phone (highest-volume login source) =====
        if digits:
            driver_match = None
            for d in (db.query(Driver)
                        .filter(Driver.active.is_(True))
                        .filter(Driver.phone.isnot(None))
                        .all()):
                if normalize_phone(d.phone) == digits:
                    driver_match = d
                    break
            if driver_match is not None:
                if driver_match.lockout_until and driver_match.lockout_until > now:
                    mins = max(1, int((driver_match.lockout_until - now)
                                      .total_seconds() // 60) + 1)
                    return jsonify({
                        "ok": False,
                        "error": f"Too many failed attempts. Try again in {mins} min.",
                    }), 429
                if (not driver_match.passcode_hash
                        or not _check(driver_match.passcode_hash, passcode)):
                    driver_match.failed_attempts = (
                        driver_match.failed_attempts or 0) + 1
                    if driver_match.failed_attempts >= MAX_FAILED_ATTEMPTS:
                        driver_match.lockout_until = (
                            now + timedelta(minutes=LOCKOUT_MINUTES))
                    db.commit()
                    return jsonify({
                        "ok": False,
                        "error": "Phone or passcode doesn't match.",
                    }), 401
                # Driver login success — set driver session keys.
                driver_match.failed_attempts = 0
                driver_match.lockout_until = None
                if hasattr(driver_match, "last_login_at"):
                    driver_match.last_login_at = now
                db.commit()
                # Clear any leftover user-keypad keys.
                for _k in ("user_id", "user_session_version",
                           "partner_auth_ok"):
                    session.pop(_k, None)
                session.permanent = True
                session["driver_id"] = driver_match.id
                session["driver_name"] = driver_match.name
                session["driver_location"] = driver_match.location
                session["driver_session_version"] = driver_match.session_version
                if not driver_match.first_login_done:
                    return jsonify({
                        "ok": True,
                        "next": url_for("driver.driver_change_passcode"),
                    })
                if nxt == "/":
                    nxt = "/driver/logs"
                return jsonify({"ok": True, "next": nxt})

        # ===== Path 2: User lookup by phone (managers/partners with phone set) =====
        # If the phone matches an active User, that User is the SOLE
        # candidate — wrong-passcode on the phone-matched user returns 401
        # with failed_attempts bumped (mirrors Path 1's Driver lockout
        # behavior). Locked-out match returns 429 with countdown. Does
        # NOT cascade to Path 3 — cascading would be the passcode-
        # collision-takeover surface samai flagged at FLAG-CRITICAL 1.
        user_match = None
        if digits:
            phone_matched_user = None
            for cand in (db.query(User)
                           .filter(User.active.is_(True))
                           .filter(User.phone.isnot(None))
                           .all()):
                if normalize_phone(cand.phone) == digits:
                    phone_matched_user = cand
                    break
            if phone_matched_user is not None:
                # Locked? Return countdown immediately (FLAG-MEDIUM 2 fix —
                # mirrors Path 1 driver lockout response).
                if (phone_matched_user.lockout_until
                        and phone_matched_user.lockout_until > now):
                    mins = max(1, int((phone_matched_user.lockout_until - now)
                                      .total_seconds() // 60) + 1)
                    return jsonify({
                        "ok": False,
                        "error": f"Too many failed attempts. Try again in {mins} min.",
                    }), 429
                # Passcode check + failed-attempts bump (FLAG-MEDIUM 3 fix —
                # mirrors Path 1 driver brute-force throttle).
                if (phone_matched_user.passcode_hash
                        and _check(phone_matched_user.passcode_hash, passcode)):
                    user_match = phone_matched_user
                else:
                    phone_matched_user.failed_attempts = (
                        phone_matched_user.failed_attempts or 0) + 1
                    if phone_matched_user.failed_attempts >= MAX_FAILED_ATTEMPTS:
                        phone_matched_user.lockout_until = (
                            now + timedelta(minutes=LOCKOUT_MINUTES))
                    db.commit()
                    return jsonify({
                        "ok": False,
                        "error": "Phone or passcode doesn't match.",
                    }), 401

        # ===== Path 3: Legacy fallback — User passcode-only scan =====
        # ONLY fires when no phone was typed at all (legacy passcode-only
        # entry, for users who predate the Sam #1591 phone-required
        # convention). FLAG-CRITICAL 1 fix: do NOT cascade here when
        # digits was non-empty — that path was the account-takeover
        # surface (typing phone-A, passcode-B matching user-B by collision,
        # logged in as user-B). If digits was typed and Path 1+2 didn't
        # match, return 401 instead.
        if user_match is None and not digits:
            user_match = _find_user_by_passcode(db, passcode)

        if user_match is None:
            return jsonify({
                "ok": False,
                "error": "Phone or passcode doesn't match.",
            }), 401

        # User login success.
        user_match.failed_attempts = 0
        user_match.lockout_until = None
        user_match.last_login_at = now
        user_match.last_login_ip = (
            request.headers.get("X-Forwarded-For")
            or request.remote_addr or "")[:64]
        db.commit()

        session.permanent = True
        # Clear any leftover driver-portal session keys (mirrors the
        # pre-unification login_submit cleanup; same race fix applies).
        for _k in ("driver_id", "driver_name", "driver_location",
                   "driver_session_version"):
            session.pop(_k, None)
        session["user_id"] = user_match.id
        session["user_session_version"] = user_match.session_version
        session["auth_ok"] = True
        if user_match.permission_level == "partner":
            session["partner_auth_ok"] = True
        else:
            session.pop("partner_auth_ok", None)

        if not user_match.first_login_done:
            return jsonify({
                "ok": True,
                "next": url_for("keypad_auth.change_passcode"),
            })
        if nxt == "/":
            nxt = _landing_for_user(user_match)
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

    @app.before_request
    def _validate_driver_session():
        """Mirror of load_current_user but for Driver sessions: if the
        driver_id in the cookie no longer matches the DB row's
        session_version, force-logout. Closes the open thread from the
        e1d929d migration — the column was added, admin reset bumps it
        (dd1d1c7), but until now nothing actually validated it on
        incoming requests. Phase 0 Block 2 (ck, 2026-05-13).

        Cost: one PK lookup per request that has a driver_id session.
        Drivers stream GPS every few seconds via /driver/track so this
        does add load — but the lookup is by primary key, ~1ms, and
        the alternative (admin reset that doesn't kick active sessions)
        is a security gap."""
        if not session.get("driver_id"):
            return
        from app.db import SessionLocal
        from app.models import Driver
        db = SessionLocal()
        try:
            d = db.get(Driver, session["driver_id"])
            stale = (
                d is None
                or not d.active
                or session.get("driver_session_version") is None
                or d.session_version != session.get("driver_session_version")
            )
            if stale:
                for _k in ("driver_id", "driver_name", "driver_location",
                           "driver_session_version"):
                    session.pop(_k, None)
        finally:
            db.close()

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

