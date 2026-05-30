"""Employee phone + SMS-code auth for Schedules V2 — Block 2.

ckai, 2026-05-29. Per the B2-backend split (aick #2927):
  - aick owns the app/models.py tables (employees, employee_sms_codes, ...);
    schema contract = aick #2932.
  - ckai (this file) owns the auth ENDPOINTS.
  - ck owns the frontend pages (GET /employee/login, GET /employee/dashboard).

ENDPOINTS (all JSON in/out, mirroring keypad_auth.login_submit):
  POST /employee/login/request-code  {"phone": "..."}
        -> finds the employee, stores a hashed 10-min OTP, "sends" it
           (MOCK log until Twilio), rate-limited (5+ rapid in a window -> 429).
           Anti-enumeration: always 200 {ok:true} whether or not the phone
           matches, so the endpoint can't be used to discover employees.
  POST /employee/login/verify-code   {"phone": "...", "code": "..."}
        -> latest unused non-expired code for that phone's employee,
           check_password_hash, single-use, 5-attempt lock -> employee session.
  POST /employee/logout              -> clears the employee session.

SESSION MODEL (mirrors keypad_auth's driver/user pattern exactly):
  session["employee_id"]    -> the logged-in employee
  session["auth_ok"] = True -> passes the global before_request gate
                               (auth.py:_gate, which accepts user_id OR auth_ok),
                               EXACTLY like driver login (:241) + user login (:330).
                               Does NOT grant partner access.

ISOLATION (samai gate-3 probes: employee session -> /partner/* -> 302/403, NEVER 200):
  An employee session sets auth_ok (to clear the site gate) but NEVER
  partner_auth_ok. /partner/* routes each require partner_auth_ok, so an
  employee already bounces. install() ALSO registers a single global firewall
  (_employee_partner_firewall) that 403s any employee session on /partner/* —
  a deterministic chokepoint so the guarantee does not depend on every partner
  route remembering its own check.

MIGRATION PLACEHOLDER (B2 checklist): POST /partner/schedules-v2/migration/run
  exists + partner-gated, returns 501 until B3 (aick) wires scripts/sling_migrate.py.

NOTE: model class names (Employee, EmployeeSmsCode) follow house convention for
the contract's table names; verify against aick's app/models.py on the
schedules-v2-b2 branch when it lands.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, abort, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import SessionLocal
from app.models import Employee, EmployeeSmsCode
from app.services.ezcater_known_drivers_seed import normalize_phone

log = logging.getLogger(__name__)

employee_auth = Blueprint("employee_auth", __name__)

# --- OTP + lockout policy (B2 spec) ---
CODE_LEN = 6                       # 6-digit numeric OTP
CODE_TTL_MINUTES = 10              # employee_sms_codes.expires_at = created + 10 min
MAX_VERIFY_ATTEMPTS = 5            # "5-attempt lock" on a single code row
RATE_WINDOW_SECONDS = 60          # request-code rate-limit window
RATE_MAX_PER_WINDOW = 4           # 5th+ rapid request in the window -> 429 (B2 spec)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _generate_code() -> str:
    """Cryptographically-random zero-padded numeric OTP."""
    return f"{secrets.randbelow(10 ** CODE_LEN):0{CODE_LEN}d}"


def _find_employee_by_phone(db, digits: str):
    """Active employee whose phone matches (digit-normalized), else None.

    employees.phone is UNIQUE; we normalize both sides so formatting
    (spaces / dashes / +1) never causes a miss. Mirrors keypad_auth's phone
    match. ~111 employees -> a scan is fine; if aick stores phone normalized
    at insert this becomes an indexed .filter(Employee.phone == digits)."""
    if not digits:
        return None
    for e in db.query(Employee).filter(Employee.active.is_(True)).all():
        if normalize_phone(e.phone or "") == digits:
            return e
    return None


def _send_sms_code(emp, code: str) -> None:
    """Deliver the OTP by SMS. MOCK until Twilio creds are confirmed (Sam
    dependency, B1 pre-flight).

    TODO(B2/Twilio): replace the mock-log with a Twilio REST send to
    emp.phone once TWILIO_* env vars are confirmed. Until then the code is
    logged server-side so Sam's gate-3 ('alarm at +2 min' / login test) can
    read it. Logged at WARNING with a [MOCK SMS] marker so it's obviously
    a dev-only path."""
    log.warning("[MOCK SMS -> employee %s] Schedules V2 login code: %s",
                getattr(emp, "id", "?"), code)


def _establish_employee_session(emp) -> None:
    """Open an ISOLATED employee session. Clears any other principal's keys
    first (mirrors keypad_auth's login cleanup at :227 / :325) so a shared
    device can't carry a stale higher-privilege session, then sets the
    employee keys + auth_ok (site gate). NEVER sets partner_auth_ok."""
    for k in ("user_id", "user_session_version",
              "driver_id", "driver_name", "driver_location",
              "driver_session_version", "partner_auth_ok"):
        session.pop(k, None)
    session.permanent = True
    session["employee_id"] = emp.id
    session["auth_ok"] = True   # passes auth.py:_gate; does NOT grant partner.


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@employee_auth.route("/employee/login/request-code", methods=["POST"])
def request_code():
    data = request.get_json(silent=True) or {}
    digits = normalize_phone((data.get("phone") or "").strip())
    if not digits:
        return jsonify({"ok": False, "error": "Enter your phone number."}), 400

    db = SessionLocal()
    try:
        emp = _find_employee_by_phone(db, digits)
        # Anti-enumeration: respond OK regardless of match; only send on a hit.
        if emp is None:
            log.info("employee request-code: no active employee for that phone")
            return jsonify({"ok": True}), 200

        # Rate-limit: too many codes for this employee in the window -> 429.
        window_start = datetime.utcnow() - timedelta(seconds=RATE_WINDOW_SECONDS)
        recent = (db.query(EmployeeSmsCode)
                    .filter(EmployeeSmsCode.employee_id == emp.id)
                    .filter(EmployeeSmsCode.created_at >= window_start)
                    .count())
        if recent >= RATE_MAX_PER_WINDOW:
            return jsonify({"ok": False,
                            "error": "Too many requests. Wait a minute and try again."}), 429

        now = datetime.utcnow()
        code = _generate_code()
        db.add(EmployeeSmsCode(
            employee_id=emp.id,
            code_hash=generate_password_hash(code),
            created_at=now,
            expires_at=now + timedelta(minutes=CODE_TTL_MINUTES),
            used=False,
            attempts=0,
        ))
        db.commit()
        _send_sms_code(emp, code)
        return jsonify({"ok": True}), 200
    finally:
        db.close()


@employee_auth.route("/employee/login/verify-code", methods=["POST"])
def verify_code():
    data = request.get_json(silent=True) or {}
    digits = normalize_phone((data.get("phone") or "").strip())
    code = (data.get("code") or "").strip()
    if not digits or not code:
        return jsonify({"ok": False, "error": "Enter your phone and the code."}), 400

    db = SessionLocal()
    try:
        emp = _find_employee_by_phone(db, digits)
        if emp is None:
            return jsonify({"ok": False, "error": "Phone or code doesn't match."}), 401

        now = datetime.utcnow()
        row = (db.query(EmployeeSmsCode)
                 .filter(EmployeeSmsCode.employee_id == emp.id)
                 .filter(EmployeeSmsCode.used.is_(False))
                 .filter(EmployeeSmsCode.expires_at > now)
                 .order_by(EmployeeSmsCode.created_at.desc())
                 .first())
        if row is None:
            return jsonify({"ok": False, "error": "Code expired. Request a new one."}), 401

        # 5-attempt lock: burn the code once it's hit the cap.
        if (row.attempts or 0) >= MAX_VERIFY_ATTEMPTS:
            row.used = True
            db.commit()
            return jsonify({"ok": False,
                            "error": "Too many attempts. Request a new code."}), 429

        if not check_password_hash(row.code_hash, code):
            row.attempts = (row.attempts or 0) + 1
            if row.attempts >= MAX_VERIFY_ATTEMPTS:
                row.used = True   # lock: no more tries on this code
            db.commit()
            return jsonify({"ok": False, "error": "Phone or code doesn't match."}), 401

        # Success — single-use: burn the code, open an isolated employee session.
        row.used = True
        db.commit()
        _establish_employee_session(emp)
        return jsonify({"ok": True, "next": "/employee/dashboard"}), 200
    finally:
        db.close()


@employee_auth.route("/employee/logout", methods=["POST"])
def logout():
    """Clear the employee session fully. Pops auth_ok too (employee-login set
    it; an employee shouldn't retain site-gate access after logging out).
    Leaves any unrelated principal keys untouched — employee-login cleared
    those on the way in, so a clean employee session has none."""
    for k in ("employee_id", "employee_session_version", "auth_ok"):
        session.pop(k, None)
    return jsonify({"ok": True}), 200


@employee_auth.route("/partner/schedules-v2/migration/run", methods=["POST"])
def migration_run_placeholder():
    """B2 placeholder — B3 (aick) fills in the real Sling migration trigger.
    Partner-gated (mirrors legal_routes:104 / ezcater_import:23 checks). The
    global firewall + this check both keep employees out.

    TODO(B3): confirm the partner+operator gate + wire scripts/sling_migrate.py."""
    if not session.get("partner_auth_ok"):
        abort(403)
    return jsonify({"ok": False,
                    "error": "Migration runner not yet implemented (B3).",
                    "block": "B3"}), 501


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
def install(app):
    """Register the blueprint + the employee->/partner firewall.

    Call from app/__init__.py:  from app.web import employee_auth
                                employee_auth.install(app)
    """
    app.register_blueprint(employee_auth)

    @app.before_request
    def _employee_partner_firewall():
        """Hard guarantee for the B2 isolation gate: an employee session may
        NEVER reach /partner/*. Employees never set partner_auth_ok and every
        partner route checks it, but this single chokepoint makes the
        guarantee independent of each route remembering its check. samai
        probes employee-session -> /partner/* -> expects 302/403, never 200."""
        if session.get("employee_id") and not session.get("partner_auth_ok"):
            path = request.path or "/"
            if path == "/partner" or path.startswith("/partner/"):
                abort(403)
