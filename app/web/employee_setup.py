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

from flask import has_request_context, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import SessionLocal
from app.models import Employee, EmployeeSetupToken
from app.web.employee_auth import (_establish_employee_session,
                                   _find_employee_by_phone, employee_auth)

log = logging.getLogger(__name__)

PASSCODE_LEN = 5                # 5-digit numeric PIN (matches keypad_auth)
SETUP_TOKEN_TTL_HOURS = 72      # invite link lifetime
MAX_LOGIN_ATTEMPTS = 5          # passcode-login lockout threshold
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


def send_setup_invite(employee_id, *, base_url: str | None = None) -> str | None:
    """Create a one-time setup token for the employee + email the setup link via
    the orders@ SMTP. Returns the raw token (for logging/testing) or None if the
    employee/email is missing. Called by the admin-add flow (aick). Never raises
    into the caller - a send failure is logged (with the link, for testing) so the
    admin-add still succeeds; a re-invite just issues a fresh token."""
    emp_email = emp_name = None
    raw = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=employee_id).first()
        if emp is None or not (emp.email or "").strip():
            log.warning("[employee-setup] no employee/email for id=%s; invite skipped", employee_id)
            return None
        db.add(EmployeeSetupToken(employee_id=emp.id, token_hash=_sha(raw),
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
    except Exception as e:  # noqa: BLE001 - never break admin-add; log the link for testing
        log.warning("[employee-setup] invite email NOT sent (employee %s): %s -- setup link: %s",
                    employee_id, e, link)
    return raw


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@employee_auth.route("/employee/login/passcode", methods=["POST"])
def login_passcode():
    """Email-or-phone + 5-digit passcode -> isolated employee session. 5-attempt
    lockout (15 min). Generic failure messages (anti-enumeration)."""
    data = request.get_json(silent=True) or {}
    ident = (data.get("identifier") or "").strip()
    passcode = (data.get("passcode") or "").strip()
    if not ident or not passcode:
        return jsonify({"ok": False, "error": "Enter your email or phone and your passcode."}), 400
    db = SessionLocal()
    try:
        emp = _find_employee_by_identifier(db, ident)
        if emp is None or not emp.passcode_hash:
            return jsonify({"ok": False, "error": "Login failed - check your details."}), 401
        now = datetime.utcnow()
        if emp.lockout_until and emp.lockout_until > now:
            return jsonify({"ok": False, "error": "Too many attempts. Try again shortly."}), 429
        if not check_password_hash(emp.passcode_hash, passcode):
            emp.failed_attempts = (emp.failed_attempts or 0) + 1
            if emp.failed_attempts >= MAX_LOGIN_ATTEMPTS:
                emp.lockout_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                emp.failed_attempts = 0
            db.commit()
            return jsonify({"ok": False, "error": "Login failed - check your details."}), 401
        emp.failed_attempts = 0
        emp.lockout_until = None
        db.commit()
        _establish_employee_session(emp)
        return jsonify({"ok": True, "next": "/employee/dashboard"}), 200
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
                        "employee": {"full_name": emp.full_name, "email": emp.email}}), 200
    finally:
        db.close()


@employee_auth.route("/employee/setup/<token>/complete", methods=["POST"])
def setup_complete(token):
    """The employee sets their 5-digit passcode + completes their profile. Token-
    scoped (sets ONLY the token's own employee -> no IDOR) + single-use (consumed
    here). On success, logs them straight in."""
    data = request.get_json(silent=True) or {}
    passcode = (data.get("passcode") or "").strip()
    if not _valid_passcode(passcode):
        return jsonify({"ok": False, "error": "Passcode must be exactly 5 digits."}), 400
    db = SessionLocal()
    try:
        emp, row = _resolve_setup_token(db, token)
        if emp is None:
            return jsonify({"ok": False, "error": "This setup link is invalid or has expired."}), 410
        emp.passcode_hash = generate_password_hash(passcode)
        full_name = (data.get("full_name") or "").strip()
        if full_name:
            emp.full_name = full_name
        phone = (data.get("phone") or "").strip()
        if phone:
            from app.services.ezcater_known_drivers_seed import normalize_phone
            emp.phone = normalize_phone(phone) or phone
        emp.failed_attempts = 0
        emp.lockout_until = None
        emp.session_version = (emp.session_version or 0) + 1  # bump -> invalidate any stale session (guardrail #4)
        emp.updated_at = datetime.utcnow()
        row.used = True   # consume the single-use token
        try:
            db.commit()
        except Exception:  # e.g. a duplicate phone (Employee.phone is UNIQUE)
            db.rollback()
            return jsonify({"ok": False,
                            "error": "Could not save - that phone may already be on file."}), 409
        _establish_employee_session(emp)
        return jsonify({"ok": True, "next": "/employee/dashboard"}), 200
    finally:
        db.close()
