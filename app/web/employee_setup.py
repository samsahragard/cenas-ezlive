"""Schedules V2 email-pivot - employee passcode auth + email self-setup (ckai).

Replaces the retired SMS-OTP login. Flow (Sam pivot 2026-05-30):
  admin-add (name+email, aick) -> send_setup_invite() emails a one-time tokenized
  link -> employee opens GET /employee/setup/<token> (ck page) -> sets a 5-digit
  passcode + completes profile (POST .../complete) -> logs in thereafter with
  email-or-phone + passcode (POST /employee/login/passcode). All set the SAME
  session["employee_id"] the OTP flow did, so B5-B9 self-service is UNCHANGED.

ATTACHES to the employee_auth blueprint (decorator side effect; imported before
ezempauth.install in app/__init__.py), reusing _establish_employee_session +
_find_employee_by_phone from employee_auth. Security (samai guardrails):
  - setup token: high-entropy (token_urlsafe), stored SHA-256 (lookupable, not
    reversible), single-use (used flips on consume), expiring (72h).
  - setup is TOKEN-SCOPED: the token resolves to ITS employee, who can set only
    their own passcode/profile -> no IDOR to another employee_id.
  - passcode login: 5-attempt lockout (15 min) - keypad_auth's pattern.
  - login + setup are anti-enumerating (generic failure messages).
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta

from flask import (has_request_context, jsonify, render_template, request,
                   session)
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import SessionLocal
from app.models import Employee, EmployeeSetupToken
from app.web.employee_auth import (_establish_employee_session,
                                   _find_employee_by_phone, _post_login_response,
                                   employee_auth)

log = logging.getLogger(__name__)

PASSCODE_LEN = 5                # 5-digit numeric PIN (matches keypad_auth)
SETUP_CODE_LEN = 5              # short MANAGER-DISPLAYED reset code (dual-channel; matches the 5-digit PIN, Sam 2026-06-07)
SETUP_TOKEN_TTL_HOURS = 72      # invite link lifetime
MAX_LOGIN_ATTEMPTS = 5          # passcode-login lockout threshold (also caps per-token code guesses)
LOCKOUT_MINUTES = 15            # lockout duration after MAX_LOGIN_ATTEMPTS


def _valid_passcode(pc: str) -> bool:
    return bool(pc) and pc.isdigit() and len(pc) == PASSCODE_LEN


def _sha(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _base_url() -> str:
    if has_request_context():
        return request.url_root.rstrip("/")
    return os.getenv("APP_BASE_URL", "https://app.cenaskitchen.com").rstrip("/")


def _find_employee_by_identifier(db, ident: str):
    """Active employee by email (if the identifier has '@') or phone, else None.
    Scans (small table) + case-insensitive email, mirroring _find_employee_by_phone."""
    ident = (ident or "").strip()
    if not ident:
        return None
    if "@" in ident:
        il = ident.lower()
        for e in db.query(Employee).filter(Employee.active.is_(True)).all():
            if (e.email or "").lower() == il:
                return e
        return None
    from app.services.ezcater_known_drivers_seed import normalize_phone
    return _find_employee_by_phone(db, normalize_phone(ident))


def _resolve_setup_token(db, token: str):
    """(employee, token_row) for a VALID setup token (unused + unexpired), else
    (None, None). Lookup by sha256(token)."""
    if not token:
        return None, None
    row = (db.query(EmployeeSetupToken)
             .filter(EmployeeSetupToken.token_hash == _sha(token),
                     EmployeeSetupToken.used.is_(False),
                     EmployeeSetupToken.expires_at > datetime.utcnow())
             .first())
    if row is None:
        return None, None
    emp = db.query(Employee).filter_by(id=row.employee_id).first()
    return (emp, row) if emp is not None else (None, None)


def _resolve_setup_by_code(db, identifier: str, code: str):
    """(employee, token_row) for a VALID setup token whose code matches, scoped to
    the identifier's ACTIVE employee, else (None, None).

    SECURITY:
      * IDENTIFIER-SCOPED: resolve the employee from the identifier (email/phone)
        FIRST, then look for THAT employee's valid token whose code_hash == sha(code).
        Employee A's code can NEVER set employee B's passcode (no cross-employee use).
      * VALID = used=False AND expires_at>now (so a consumed link -> code dead, and
        an expired/superseded token never matches).
      * BRUTE-FORCE CAP: a 6-digit code is guessable, so each wrong code increments
        the row's code_attempts; once >= MAX_LOGIN_ATTEMPTS the token is rejected
        (returns (None,None)) even for the right code -- the manager must re-issue.
        The increment is committed so the cap survives across requests.
    Anti-enumeration: callers surface a single generic failure for every miss."""
    if not (identifier or "").strip() or not (code or "").strip():
        return None, None
    emp = _find_employee_by_identifier(db, identifier)
    if emp is None:
        return None, None
    # The newest valid (unused, unexpired) token for THIS employee. send_setup_invite
    # invalidates prior unused tokens, so at most one is live; order by id desc for safety.
    row = (db.query(EmployeeSetupToken)
             .filter(EmployeeSetupToken.employee_id == emp.id,
                     EmployeeSetupToken.used.is_(False),
                     EmployeeSetupToken.expires_at > datetime.utcnow())
             .order_by(EmployeeSetupToken.id.desc())
             .first())
    if row is None or not row.code_hash:
        return None, None
    # Hard lockout once the per-token guess cap is hit (reject even a correct code).
    if (row.code_attempts or 0) >= MAX_LOGIN_ATTEMPTS:
        return None, None
    if not secrets.compare_digest(row.code_hash, _sha(code)):
        row.code_attempts = (row.code_attempts or 0) + 1
        try:
            db.commit()
        except Exception:  # noqa: BLE001 - never let a counter write mask the auth failure
            db.rollback()
        return None, None
    return emp, row


def _gen_setup_code() -> str:
    """A zero-padded SETUP_CODE_LEN-digit numeric code (cryptographically random).
    secrets.randbelow gives a uniform draw over 0..10**n-1, then zero-pad so every
    code is exactly n digits (incl. leading zeros)."""
    return str(secrets.randbelow(10 ** SETUP_CODE_LEN)).zfill(SETUP_CODE_LEN)


def send_setup_invite(employee_id, *, base_url: str | None = None) -> dict | None:
    """Create a one-time setup token for the employee that backs BOTH reset
    channels -- the emailed link AND a short MANAGER-DISPLAYED code -- and email
    the link via the orders@ SMTP.

    Dual-channel (Sam 2026-06-07): the link token and the 6-digit code live on the
    SAME single-use EmployeeSetupToken row, so whichever the employee uses FIRST
    consumes the row (used=True) and the OTHER stops working. Before inserting the
    new row, all prior UNUSED tokens for this employee are invalidated (used=True)
    so only the newest reset is live (an old link + old code both die).

    Returns {"token": raw, "code": code} (raw values, for the manager response /
    testing) or None if the employee/email is missing. The raw code is returned
    ONLY to the manager via the reset/add response; it is NEVER stored in plaintext
    or logged. Called by the admin-add + reset-PIN flows. Never raises into the
    caller - a send failure is logged (with the link, NOT the code) so the action
    still succeeds; a re-invite just issues a fresh token + code."""
    emp_email = emp_name = None
    raw = secrets.token_urlsafe(32)
    code = _gen_setup_code()
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=employee_id).first()
        if emp is None or not (emp.email or "").strip():
            log.warning("[employee-setup] no employee/email for id=%s; invite skipped", employee_id)
            return None
        # Invalidate any prior UNUSED tokens for this employee so only the NEWEST
        # reset is live (old link + old code both become dead). Single-use rows are
        # consumed via used=True, so flipping unused ones to used kills them too.
        (db.query(EmployeeSetupToken)
           .filter(EmployeeSetupToken.employee_id == emp.id,
                   EmployeeSetupToken.used.is_(False))
           .update({EmployeeSetupToken.used: True}, synchronize_session=False))
        db.add(EmployeeSetupToken(employee_id=emp.id, token_hash=_sha(raw),
                                  code_hash=_sha(code), code_attempts=0,
                                  expires_at=now + timedelta(hours=SETUP_TOKEN_TTL_HOURS),
                                  used=False, created_at=now))
        db.commit()
        emp_email, emp_name = emp.email, emp.full_name
    finally:
        db.close()

    link = "%s/employee/setup/%s" % (_base_url() if base_url is None else base_url.rstrip("/"), raw)
    body = ("Hi %s,\n\nYou've been added to Cenas Kitchen scheduling. Set up your "
            "account at the link below (it expires in %d hours):\n\n%s\n\nYou'll set "
            "a 5-digit passcode and confirm your details, then you can view your "
            "schedule.\n\n- Cenas Kitchen"
            % (emp_name or "there", SETUP_TOKEN_TTL_HOURS, link))
    try:
        from app.services import brief_email
        brief_email._smtp_send(emp_email, "Set up your Cenas Kitchen account", body)
        log.info("[employee-setup] invite emailed to employee %s", employee_id)
    except Exception as e:  # noqa: BLE001 - never break admin-add; log the link (NOT the code) for testing
        log.warning("[employee-setup] invite email NOT sent (employee %s): %s -- setup link: %s",
                    employee_id, e, link)
    return {"token": raw, "code": code}


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@employee_auth.route("/employee/login/passcode", methods=["POST"])
def login_passcode():
    """Email-or-phone + 5-digit passcode -> isolated employee session. 5-attempt
    lockout (15 min). Generic failure messages (anti-enumeration).

    Sam 2026-06-07: a manager-issued RESET CODE also logs the employee in here --
    they enter their email/phone + the code (instead of a PIN) and are signed in;
    the code BECOMES their passcode (changeable later in their profile). This is
    why the reset gives a code: the employee logs in with it directly (no separate
    page needed). It consumes the shared single-use setup token, so the emailed
    setup link stops working -- whichever they use FIRST wins."""
    data = request.get_json(silent=True) or {}
    ident = (data.get("identifier") or "").strip()
    passcode = (data.get("passcode") or "").strip()
    if not ident or not passcode:
        return jsonify({"ok": False, "error": "Enter your email or phone and your passcode."}), 400
    db = SessionLocal()
    try:
        emp = _find_employee_by_identifier(db, ident)
        if emp is None:
            return jsonify({"ok": False, "error": "Login failed - check your details."}), 401
        now = datetime.utcnow()
        if emp.lockout_until and emp.lockout_until > now:
            return jsonify({"ok": False, "error": "Too many attempts. Try again shortly."}), 429
        # 1) Normal passcode login.
        if emp.passcode_hash and check_password_hash(emp.passcode_hash, passcode):
            emp.failed_attempts = 0
            emp.lockout_until = None
            db.commit()
            stores = _establish_employee_session(emp)
            return _post_login_response(stores)   # Lane B: both-store -> picker, else dashboard
        # 2) RESET-CODE login: a valid manager-issued code (identifier-scoped +
        #    brute-force-capped in _resolve_setup_by_code) signs them in and becomes
        #    their passcode; consumes the shared token so the emailed link is dead.
        emp_c, row = _resolve_setup_by_code(db, ident, passcode)
        if emp_c is not None:
            emp_c.passcode_hash = generate_password_hash(passcode)
            emp_c.failed_attempts = 0
            emp_c.lockout_until = None
            emp_c.session_version = (emp_c.session_version or 0) + 1
            row.used = True   # consume the shared single-use token -> emailed link now dead
            db.commit()
            stores = _establish_employee_session(emp_c)
            return _post_login_response(stores)
        # 3) Neither a matching passcode nor a valid code -> count the failure.
        emp.failed_attempts = (emp.failed_attempts or 0) + 1
        if emp.failed_attempts >= MAX_LOGIN_ATTEMPTS:
            emp.lockout_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            emp.failed_attempts = 0
        db.commit()
        return jsonify({"ok": False, "error": "Login failed - check your details."}), 401
    finally:
        db.close()


@employee_auth.route("/employee/setup/<token>/info", methods=["GET"])
def setup_info(token):
    """Validate a setup token + return the prefill (name/email). valid:false (200)
    when the token is bad/expired/used so the page can show 'link expired'."""
    db = SessionLocal()
    try:
        emp, _row = _resolve_setup_token(db, token)
        if emp is None:
            return jsonify({"ok": True, "valid": False}), 200
        return jsonify({"ok": True, "valid": True,
                        "employee": {"full_name": emp.full_name, "email": emp.email,
                                     "phone": emp.phone}}), 200
    finally:
        db.close()


def _apply_passcode(db, emp, row, *, passcode, phone, full_name):
    """Shared single-use-token consume + passcode set for BOTH reset channels
    (the link and the code). Validates+sets the 5-digit passcode, requires+
    normalizes the phone (Sam #2606), bumps session_version (kills stale sessions,
    guardrail #4), consumes the SAME row (row.used=True -> the other channel for
    this reset stops working), commits (handling the duplicate-phone 409), and
    establishes the employee session.

    Returns a Flask response: the post-login response on success, or an error
    tuple ((json, status)). Centralizing this guarantees the link path and the code
    path consume the IDENTICAL single-use row -> first-wins is structural, not
    duplicated per endpoint."""
    passcode = (passcode or "").strip()
    if not _valid_passcode(passcode):
        return jsonify({"ok": False, "error": "Passcode must be exactly 5 digits."}), 400
    # Phone is REQUIRED at setup (Sam #2606): the normal /keypad-login is phone+PIN, so
    # an employee needs a phone on file to sign in there. Validate + normalize up front.
    from app.services.ezcater_known_drivers_seed import normalize_phone
    norm_phone = normalize_phone((phone or "").strip())
    if not norm_phone or len(norm_phone) < 10:
        return jsonify({"ok": False, "error": "A valid phone number is required."}), 400
    emp.passcode_hash = generate_password_hash(passcode)
    full_name = (full_name or "").strip()
    if full_name:
        emp.full_name = full_name
    emp.phone = norm_phone   # required + normalized above (Sam #2606)
    emp.failed_attempts = 0
    emp.lockout_until = None
    emp.session_version = (emp.session_version or 0) + 1  # bump -> invalidate any stale session (guardrail #4)
    emp.updated_at = datetime.utcnow()
    # If this employee is a linked manager (User), keep their keypad login in sync:
    # set the SAME passcode on the User + bump its session_version, so a manager who
    # uses the link / code-page changes how they sign in at /keypad-login (managers
    # authenticate against User.passcode, not Employee.passcode).
    _uid = getattr(emp, "user_id", None)
    if _uid:
        from app.models import User as _User
        _u = db.query(_User).filter_by(id=_uid).first()
        if _u is not None:
            _u.passcode_hash = emp.passcode_hash
            if hasattr(_u, "failed_attempts"):
                _u.failed_attempts = 0
            if hasattr(_u, "lockout_until"):
                _u.lockout_until = None
            _u.session_version = (_u.session_version or 0) + 1
    row.used = True   # consume the single-use token (the OTHER channel for this reset now dies)
    try:
        db.commit()
    except Exception:  # e.g. a duplicate phone (Employee.phone is UNIQUE)
        db.rollback()
        return jsonify({"ok": False,
                        "error": "Could not save - that phone may already be on file."}), 409
    stores = _establish_employee_session(emp)
    return _post_login_response(stores)   # Lane B: both-store -> picker, else dashboard


@employee_auth.route("/employee/setup/<token>/complete", methods=["POST"])
def setup_complete(token):
    """The employee sets their 5-digit passcode + completes their profile via the
    emailed LINK. Token-scoped (sets ONLY the token's own employee -> no IDOR) +
    single-use (consumed via _apply_passcode -> the matching CODE for this reset
    stops working). On success, logs them straight in."""
    data = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        emp, row = _resolve_setup_token(db, token)
        if emp is None:
            return jsonify({"ok": False, "error": "This setup link is invalid or has expired."}), 410
        return _apply_passcode(db, emp, row,
                               passcode=(data.get("passcode") or "").strip(),
                               phone=(data.get("phone") or "").strip(),
                               full_name=(data.get("full_name") or "").strip())
    finally:
        db.close()


@employee_auth.route("/employee/setup/code/complete", methods=["POST"])
def setup_code_complete():
    """The employee sets their 5-digit passcode via the short MANAGER-DISPLAYED
    CODE (the second reset channel). JSON: {identifier, code, passcode, phone,
    full_name}. Resolves the code SCOPED to the identifier's employee, then runs
    _apply_passcode -> consumes the SAME single-use row, so the email link for this
    SAME reset immediately stops working (first-wins).

    Anti-enumeration: a bad identifier/code is a single generic 410 (same as a
    used/expired token); the per-token guess cap returns 429 once tripped."""
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or "").strip()
    code = (data.get("code") or "").strip()
    if not identifier or not code:
        return jsonify({"ok": False, "error": "Enter your email or phone and the code."}), 400
    db = SessionLocal()
    try:
        # Distinguish a hard lockout (429) from a generic miss (410) WITHOUT leaking
        # whether the identifier exists: only an identifier that resolves to an
        # employee with a live, attempt-capped token yields 429.
        emp_probe = _find_employee_by_identifier(db, identifier)
        if emp_probe is not None:
            live = (db.query(EmployeeSetupToken)
                      .filter(EmployeeSetupToken.employee_id == emp_probe.id,
                              EmployeeSetupToken.used.is_(False),
                              EmployeeSetupToken.expires_at > datetime.utcnow())
                      .order_by(EmployeeSetupToken.id.desc())
                      .first())
            if live is not None and (live.code_attempts or 0) >= MAX_LOGIN_ATTEMPTS:
                return jsonify({"ok": False,
                                "error": "Too many attempts. Ask your manager for a new code."}), 429
        emp, row = _resolve_setup_by_code(db, identifier, code)
        if emp is None:
            return jsonify({"ok": False, "error": "That code is invalid or has expired."}), 410
        return _apply_passcode(db, emp, row,
                               passcode=(data.get("passcode") or "").strip(),
                               phone=(data.get("phone") or "").strip(),
                               full_name=(data.get("full_name") or "").strip())
    finally:
        db.close()


@employee_auth.route("/employee/setup-code", methods=["GET"])
def employee_setup_code_page():
    """Render the mobile page where an employee enters their email/phone + the
    short MANAGER-DISPLAYED code to set their passcode (the code-channel companion
    to GET /employee/setup/<token>). Anonymous-reachable (the code is the
    credential; no employee session yet -- covered by the /employee/setup EXEMPT
    prefix). The UI phase refines the template; this route just renders the shell."""
    return render_template("employee_setup_code.html")
