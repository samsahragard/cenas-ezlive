"""Unified phone + 5-digit passcode keypad auth (Sam #1591 — 2026-05-15).

Endpoints:
  GET  /keypad-login          — renders the unified two-screen pad (phone → PIN)
  POST /keypad-login          — JSON: {"phone": "...", "pin": "..."} → routes to the
                                matching dashboard, or returns an account picker
                                when one phone+PIN legitimately unlocks more than
                                one profile (for example Employee + Driver)
  GET  /change-passcode       — renders the change-passcode keypad (forced on first login)
  POST /change-passcode       — JSON: {"new": "12345"} → {"ok": true} or {"ok": false, "error": "..."}
  GET  /keypad-logout         — clears session, redirects to /keypad-login

Pre-Sam-#1591 history: /keypad-login was passcode-only against User.passcode_hash;
/driver/login was phone+pin against Driver.passcode_hash. Two forms, two
post-logout destinations, and a confusing UX where a driver who logged out
landed on the partner-keypad page (Sam, 2026-05-15: "it automatically goes
to the password screen for the Partners, not the passcode").

Now: ONE unified entry. Phone is the first factor and each matching active
principal verifies its own passcode hash. If the same person has multiple
legitimate profiles on that phone+PIN, the client shows a Driver / store /
management picker and opens exactly one session after the choice. This avoids
the old driver-first collision where a newly created driver profile could
shadow the employee account. User-by-passcode-only is retained as the legacy
fallback so partners/managers who predate the Sam #1591 phone-required
convention can still sign in when no phone is supplied.

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
import secrets
import hashlib
import time
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
# Numeric-only keypad PINs. The visible unified login pad renders digits only,
# and Sam's 2026-06-03 direction keeps all login on numbers.
PASSCODE_RE = re.compile(rf"^\d{{{PASSCODE_LEN}}}$")
MAX_FAILED_ATTEMPTS = 6
LOCKOUT_MINUTES = 10
PENDING_LOGIN_TTL_SECONDS = 120
_PENDING_LOGIN_CHOICES: dict[str, dict] = {}


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


def _locked_minutes(lockout_until, now: datetime) -> int:
    return max(1, int((lockout_until - now).total_seconds() // 60) + 1)


def _passcode_marker(passcode_hash: str | None) -> str | None:
    if not passcode_hash:
        return None
    return hashlib.sha256(passcode_hash.encode("utf-8")).hexdigest()


def _clear_pending_login_choices() -> None:
    batch_id = session.pop("login_account_pick_id", None)
    if batch_id:
        _PENDING_LOGIN_CHOICES.pop(batch_id, None)
    session.pop("login_account_choices", None)  # older cookie-backed pending state


def _prune_pending_login_choices(now_ts: float | None = None) -> None:
    now_ts = now_ts if now_ts is not None else time.time()
    expired = [
        batch_id for batch_id, entry in _PENDING_LOGIN_CHOICES.items()
        if float(entry.get("expires_at") or 0) <= now_ts
    ]
    for batch_id in expired:
        _PENDING_LOGIN_CHOICES.pop(batch_id, None)


def _store_label(store_key: str | None) -> str:
    labels = {"tomball": "Tomball", "copperfield": "Copperfield", "__both__": "Both stores"}
    return labels.get(store_key, (store_key or "").title())


def _establish_driver_session(driver, next_url: str = "/") -> str:
    """Open a driver session and clear every other principal key."""
    _clear_pending_login_choices()
    for _k in ("user_id", "user_session_version", "partner_auth_ok",
               "employee_id", "employee_session_version", "active_store"):
        session.pop(_k, None)
    session.permanent = True
    session["driver_id"] = driver.id
    session["driver_name"] = driver.name
    session["driver_location"] = driver.location
    session["driver_session_version"] = driver.session_version
    session["auth_ok"] = True
    if not driver.first_login_done:
        return url_for("driver.driver_change_passcode")
    if next_url and next_url.startswith(("/driver", "/my-profile")):
        return next_url
    return "/my-profile"


def _establish_user_session(user, next_url: str = "/") -> str:
    """Open a management/user session and clear driver/employee keys."""
    _clear_pending_login_choices()
    for _k in ("driver_id", "driver_name", "driver_location",
               "driver_session_version", "employee_id",
               "employee_session_version", "active_store"):
        session.pop(_k, None)
    session.permanent = True
    session["user_id"] = user.id
    session["user_session_version"] = user.session_version
    session["auth_ok"] = True
    if user.permission_level == "partner":
        session["partner_auth_ok"] = True
    else:
        session.pop("partner_auth_ok", None)
    if not user.first_login_done:
        return url_for("keypad_auth.change_passcode")
    return next_url if next_url and next_url != "/" else _landing_for_user(user)


def _establish_employee_choice(employee, store_key: str | None = None) -> str:
    """Open an employee session, optionally setting the selected active store."""
    from app.web.employee_auth import _employee_store_keys, _establish_employee_session

    _clear_pending_login_choices()
    stores = _establish_employee_session(employee, include_linked_user=False)
    valid_stores = _employee_store_keys(employee.id)
    if store_key == "__both__":
        if len(valid_stores) >= 2:
            session["active_store"] = "__both__"
    elif store_key and store_key in valid_stores:
        session["active_store"] = store_key
    elif len(stores) > 1 and not session.get("active_store"):
        return "/employee/login?needpick=1"
    return "/employee/dashboard"


def _employee_choice_specs(employee) -> list[dict]:
    """Return employee portal choices split by store for the unified picker."""
    from app.web.employee_auth import _employee_store_keys

    stores = _employee_store_keys(employee.id)
    if not stores:
        return [{
            "kind": "employee",
            "id": employee.id,
            "session_version": getattr(employee, "session_version", None),
            "passcode_marker": _passcode_marker(getattr(employee, "passcode_hash", None)),
            "store_key": None,
            "label": "Employee",
            "sub": "Employee app",
        }]
    choices = [{
        "kind": "employee",
        "id": employee.id,
        "session_version": getattr(employee, "session_version", None),
        "passcode_marker": _passcode_marker(getattr(employee, "passcode_hash", None)),
        "store_key": store_key,
        "label": _store_label(store_key),
        "sub": "Employee app",
    } for store_key in stores]
    if len(stores) > 1:
        choices.append({
            "kind": "employee",
            "id": employee.id,
            "session_version": getattr(employee, "session_version", None),
            "passcode_marker": _passcode_marker(getattr(employee, "passcode_hash", None)),
            "store_key": "__both__",
            "label": "Both stores",
            "sub": "Employee app",
        })
    return choices


def _driver_choice_spec(driver) -> dict:
    return {
        "kind": "driver",
        "id": driver.id,
        "session_version": getattr(driver, "session_version", None),
        "passcode_marker": _passcode_marker(getattr(driver, "passcode_hash", None)),
        "label": "Driver",
        "sub": f"Driver portal · {_store_label(driver.location)}",
    }


def _user_choice_spec(user) -> dict:
    label_map = {
        "partner": "Partner",
        "corporate": "Corporate",
        "corporate_chef": "Corporate Chef",
        "gm": "GM",
        "manager": "Manager",
        "km": "Kitchen Manager",
        "assistant_km": "Assistant KM",
        "prep_manager": "Prep Manager",
        "foh_manager": "FOH Manager",
        "expo": "Expo",
    }
    return {
        "kind": "user",
        "id": user.id,
        "session_version": getattr(user, "session_version", None),
        "passcode_marker": _passcode_marker(getattr(user, "passcode_hash", None)),
        "label": label_map.get(user.permission_level, user.permission_level.title()),
        "sub": "Management app",
    }


def _store_pending_choices(choice_specs: list[dict], next_url: str) -> list[dict]:
    _clear_pending_login_choices()
    _prune_pending_login_choices()
    batch_id = secrets.token_urlsafe(16)
    private = []
    public = []
    for spec in choice_specs:
        token = secrets.token_urlsafe(12)
        row = {**spec, "token": token, "next": next_url}
        private.append(row)
        public.append({
            "token": token,
            "kind": spec["kind"],
            "label": spec["label"],
            "sub": spec.get("sub") or "",
        })
    _PENDING_LOGIN_CHOICES[batch_id] = {
        "expires_at": time.time() + PENDING_LOGIN_TTL_SECONDS,
        "choices": private,
    }
    session["login_account_pick_id"] = batch_id
    session.permanent = True
    return public


def _start_or_finish_login(db, choice_specs: list[dict], next_url: str):
    if len(choice_specs) > 1:
        choices = _store_pending_choices(choice_specs, next_url)
        return jsonify({"ok": True, "needs_account_pick": True,
                        "choices": choices}), 200
    return _finish_login_choice(db, choice_specs[0], next_url)


def _finish_login_choice(db, choice: dict, next_url: str = "/"):
    kind = choice.get("kind")
    if kind == "driver":
        from app.models import Driver
        driver = db.get(Driver, int(choice["id"]))
        if driver is None or not driver.active:
            _clear_pending_login_choices()
            return jsonify({"ok": False, "error": "That account is no longer active."}), 403
        if not _choice_still_current(driver, choice):
            _clear_pending_login_choices()
            return jsonify({"ok": False, "error": "Sign in again."}), 401
        driver.failed_attempts = 0
        driver.lockout_until = None
        if hasattr(driver, "last_login_at"):
            driver.last_login_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "next": _establish_driver_session(driver, next_url)}), 200
    if kind == "employee":
        from app.models import Employee
        employee = db.get(Employee, int(choice["id"]))
        if employee is None or not employee.active:
            _clear_pending_login_choices()
            return jsonify({"ok": False, "error": "That account is no longer active."}), 403
        if not _choice_still_current(employee, choice):
            _clear_pending_login_choices()
            return jsonify({"ok": False, "error": "Sign in again."}), 401
        employee.failed_attempts = 0
        employee.lockout_until = None
        db.commit()
        return jsonify({"ok": True, "next": _establish_employee_choice(
            employee, choice.get("store_key"))}), 200
    if kind == "user":
        user = db.get(User, int(choice["id"]))
        if user is None or not user.active:
            _clear_pending_login_choices()
            return jsonify({"ok": False, "error": "That account is no longer active."}), 403
        if not _choice_still_current(user, choice):
            _clear_pending_login_choices()
            return jsonify({"ok": False, "error": "Sign in again."}), 401
        user.failed_attempts = 0
        user.lockout_until = None
        user.last_login_at = datetime.utcnow()
        user.last_login_ip = (
            request.headers.get("X-Forwarded-For")
            or request.remote_addr or "")[:64]
        db.commit()
        return jsonify({"ok": True, "next": _establish_user_session(user, next_url)}), 200
    return jsonify({"ok": False, "error": "Unknown account choice."}), 400


def _choice_still_current(principal, choice: dict) -> bool:
    lockout_until = getattr(principal, "lockout_until", None)
    if lockout_until and lockout_until > datetime.utcnow():
        return False
    if choice.get("session_version") != getattr(principal, "session_version", None):
        return False
    return choice.get("passcode_marker") == _passcode_marker(
        getattr(principal, "passcode_hash", None))


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
    # Driver session takes a driver to their profile, NOT this login page.
    # Prevents the post-driver-logout symptom where the partner-keypad
    # rendered over an active driver session.
    if session.get("driver_id"):
        return redirect("/my-profile")
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
    Phone + passcode can now match more than one legitimate principal for the
    same person (for example a Tomball employee who also signs up as a driver).
    In that case, credentials are verified first and the response returns a
    server-side pending account picker. The picker opens exactly ONE session
    after the person chooses Driver / Tomball / Copperfield / Corporate.

    Backward-compat: the field name `passcode` is still accepted as an alias
    for `pin` so old client code that POSTs {passcode: ...} doesn't break.
    Sets the appropriate session keys for whichever role matched and routes
    to that role's landing page.
    """
    from app.services.ezcater_known_drivers_seed import normalize_phone
    from app.models import Driver, Employee
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
    _clear_pending_login_choices()

    db = SessionLocal()
    try:
        # Phone-present path: collect every active principal with that phone,
        # then only offer choices whose own hash accepted this passcode. This
        # fixes the old driver-first behavior where a driver profile shadowed
        # the employee account on the same phone.
        if digits:
            phone_principals: list[tuple[str, object]] = []
            for d in (db.query(Driver)
                        .filter(Driver.active.is_(True))
                        .filter(Driver.phone.isnot(None))
                        .all()):
                if normalize_phone(d.phone) == digits:
                    phone_principals.append(("driver", d))
            for cand in (db.query(User)
                           .filter(User.active.is_(True))
                           .filter(User.phone.isnot(None))
                           .all()):
                if normalize_phone(cand.phone) == digits:
                    phone_principals.append(("user", cand))
                    break
            for emp in db.query(Employee).filter(Employee.active.is_(True)).all():
                if normalize_phone(emp.phone or "") == digits:
                    phone_principals.append(("employee", emp))
                    break

            if not phone_principals:
                return jsonify({
                    "ok": False,
                    "error": "Phone or passcode doesn't match.",
                }), 401

            choices: list[dict] = []
            unlocked = []
            locked = []
            for kind, principal in phone_principals:
                lockout_until = getattr(principal, "lockout_until", None)
                if lockout_until and lockout_until > now:
                    locked.append(principal)
                    continue
                unlocked.append(principal)
                pass_hash = getattr(principal, "passcode_hash", None)
                if not pass_hash or not _check(pass_hash, passcode):
                    continue
                if kind == "driver":
                    choices.append(_driver_choice_spec(principal))
                elif kind == "user":
                    choices.append(_user_choice_spec(principal))
                elif kind == "employee":
                    choices.extend(_employee_choice_specs(principal))

            if choices:
                return _start_or_finish_login(db, choices, nxt)

            if not unlocked and locked:
                mins = min(_locked_minutes(p.lockout_until, now) for p in locked)
                return jsonify({
                    "ok": False,
                    "error": f"Too many failed attempts. Try again in {mins} min.",
                }), 429

            for principal in unlocked:
                principal.failed_attempts = (principal.failed_attempts or 0) + 1
                if principal.failed_attempts >= MAX_FAILED_ATTEMPTS:
                    principal.lockout_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                    if isinstance(principal, Employee):
                        principal.failed_attempts = 0
            db.commit()
            return jsonify({
                "ok": False,
                "error": "Phone or passcode doesn't match.",
            }), 401

        # Legacy fallback — User passcode-only scan.
        # ONLY fires when no phone was typed at all (legacy passcode-only
        # entry, for users who predate the Sam #1591 phone-required
        # convention). FLAG-CRITICAL 1 fix: do NOT cascade here when
        # digits was non-empty — that path was the account-takeover
        # surface (typing phone-A, passcode-B matching user-B by collision,
        # logged in as user-B). If digits was typed and Path 1+2 didn't
        # match, return 401 instead.
        user_match = _find_user_by_passcode(db, passcode)
        if user_match is None:
            return jsonify({
                "ok": False,
                "error": "Phone or passcode doesn't match.",
            }), 401
        return _finish_login_choice(db, _user_choice_spec(user_match), nxt)
    finally:
        db.close()


@keypad_auth.route("/keypad-login/select-account", methods=["POST"])
def select_login_account():
    """Complete a pending multi-account login choice.

    The pending choices were created only after phone+PIN succeeded. The
    browser receives only opaque tokens; account ids/types live in a short
    server-side cache and are revalidated before any session opens.
    """
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    _prune_pending_login_choices()
    batch_id = session.get("login_account_pick_id")
    pending = _PENDING_LOGIN_CHOICES.get(batch_id or "")
    if not pending:
        _clear_pending_login_choices()
        return jsonify({"ok": False, "error": "Sign in again."}), 401
    choices = pending.get("choices") or []
    choice = next((c for c in choices if c.get("token") == token), None)
    if not choice:
        _clear_pending_login_choices()
        return jsonify({"ok": False, "error": "Sign in again."}), 401
    next_url = (choice.get("next") or "/").strip()
    if not next_url.startswith("/"):
        next_url = "/"
    db = SessionLocal()
    try:
        return _finish_login_choice(db, choice, next_url)
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

