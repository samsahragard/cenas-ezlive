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
from types import SimpleNamespace

from flask import (Blueprint, abort, jsonify, redirect, render_template,
                   request, session)
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import SessionLocal
from app.models import Employee, EmployeeSmsCode, EmployeeStoreAssignment, EmployeePosition, CenaToastLink
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


def _establish_employee_session(emp) -> list[str]:
    """Open an ISOLATED employee session. Clears any other principal's keys
    first (mirrors keypad_auth's login cleanup at :227 / :325) so a shared
    device can't carry a stale higher-privilege session, then sets the
    employee keys + auth_ok (site gate). NEVER sets partner_auth_ok.

    Returns the employee's assigned store_keys (Lane B, Sam #3573 cross-store):
    with exactly ONE, session['active_store'] is auto-set (the store aick's
    per-request g.effective_perms resolves the position-union against); with 2+
    it's left UNSET and the caller routes to the 'Uno Mas / Dos Mas' picker
    (POST /employee/select-store), which sets it."""
    for k in ("user_id", "user_session_version",
              "driver_id", "driver_name", "driver_location",
              "driver_session_version", "partner_auth_ok", "active_store"):
        session.pop(k, None)
    session.permanent = True
    session["employee_id"] = emp.id
    session["employee_session_version"] = emp.session_version  # stale-session gate (guardrail #4)
    session["auth_ok"] = True   # passes auth.py:_gate; does NOT grant partner.
    # UNIFY login-fold (Project 1, Sam #2261; seam ckai-locked #2295): a team member
    # who is ALSO a manager/partner is LINKED via Employee.user_id. ONE login (this
    # passcode flow) then establishes their MANAGER keypad session too, so the same
    # login grants both self-service (employee_id) AND management gates (require_level
    # reads user_id). A PURE employee (user_id NULL) stays fully isolated - no user_id,
    # no /partner. partner_auth_ok is NOT folded - the /partner/* second-factor stays
    # a deliberate separate gate (owner-only), so a linked partner still second-factors.
    uid = getattr(emp, "user_id", None)
    if uid:
        from app.db import SessionLocal as _SL
        from app.models import User as _User
        _udb = _SL()
        try:
            u = _udb.query(_User).filter_by(id=uid).first()
            if u is not None and u.active:
                session["user_id"] = u.id
                session["user_session_version"] = getattr(u, "session_version", None)
        finally:
            _udb.close()
    # CROSS-STORE login resolution (Lane B, Sam #3573/#3582 + ckbro #3583): the
    # store(s) a person may act at = the stores where they HOLD A POSITION (per-store
    # EmployeePosition.store_key - the (A)-model source, Sam #2457). ONE store ->
    # auto-scope now; 2+ -> leave active_store unset, the caller pops the picker and
    # /employee/select-store sets the chosen store. Perms then follow that store via
    # aick's per-request g.effective_perms (Lane A); store scoping is Lane B's.
    stores = _employee_store_keys(emp.id)
    if len(stores) == 1:
        session["active_store"] = stores[0]
    return stores


def _employee_store_keys(emp_id) -> list[str]:
    """Distinct stores where the employee HOLDS A POSITION (per-store
    EmployeePosition.store_key) - the (A)-model 'their stores' source (Sam #2457):
    a person can act at a store only where they hold a position there (= where they
    have perms), so the login picker, the active-store auto-scope, and the
    select-store membership check all key off THIS, not EmployeeStoreAssignment.
    NULL-store rows (store-less employees, pre-backfill) are excluded (stable order)."""
    db = SessionLocal()
    try:
        rows = (db.query(EmployeePosition.store_key)
                  .filter(EmployeePosition.employee_id == emp_id,
                          EmployeePosition.store_key.isnot(None))
                  .distinct().all())
    finally:
        db.close()
    seen: set[str] = set()
    out: list[str] = []
    for (sk,) in rows:
        sk = (sk or "").strip().lower()
        if sk and sk not in seen:
            seen.add(sk)
            out.append(sk)
    return out


def _post_login_response(stores):
    """Lane B: shape the login JSON. A both-store person (2+ assigned stores,
    active_store not yet set by _establish_employee_session) is routed to the
    store picker; everyone else goes straight to the dashboard."""
    if len(stores) > 1 and not session.get("active_store"):
        return jsonify({
            "ok": True,
            "needs_store_pick": True,
            "stores": [{"key": s, "label": _STORE_LABELS.get(s, (s or "").title())}
                       for s in stores],
            "next": "/employee/select-store",
        }), 200
    return jsonify({"ok": True, "next": "/employee/dashboard"}), 200


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
# --- RETIRED 2026-05-30 (email pivot): the SMS-OTP login endpoints
# /employee/login/request-code + /employee/login/verify-code are REMOVED. Login is
# now email-or-phone + a 5-digit passcode via POST /employee/login/passcode
# (app/web/employee_setup.py); onboarding is the emailed setup link. No dangling
# OTP route can bypass the new passcode gate (samai guardrail #5). _send_sms_code,
# _generate_code, the RATE_*/CODE_*/MAX_VERIFY constants + the EmployeeSmsCode
# import are now unused here - left in place (harmless) for a tidy-up pass. ---


@employee_auth.route("/employee/logout", methods=["POST"])
def logout():
    """Clear the employee session fully. Pops auth_ok (employee-login set it; an
    employee shouldn't retain site-gate access after logout), active_store (Lane B),
    AND the folded manager keys (user_id / user_session_version) the UNIFY login-fold
    sets for a LINKED team member - else a linked manager logging out of the employee
    app would keep their management session. Other principals were cleared on login."""
    for k in ("employee_id", "employee_session_version", "auth_ok",
              "active_store", "user_id", "user_session_version"):
        session.pop(k, None)
    return jsonify({"ok": True}), 200


@employee_auth.route("/employee/select-store", methods=["POST"])
def select_store():
    """Lane B (cross-store, Sam #3582): a person assigned to BOTH stores picks
    their active store after login. Sets session['active_store'] - the store
    aick's per-request g.effective_perms resolves the position-union against -
    but ONLY after verifying the logged-in employee is actually assigned there
    (you can't scope yourself into a store you don't belong to). Idempotent."""
    emp_id = session.get("employee_id")
    if not emp_id:
        return jsonify({"ok": False, "error": "Not logged in."}), 401
    data = request.get_json(silent=True) or {}
    store_key = (data.get("store_key") or "").strip().lower()
    member = _employee_store_keys(emp_id)
    # Sam #2606: "__both__" = the "Both stores" pick -> only valid for someone actually
    # assigned to 2+ stores; perms then union across stores (see _effective_perms).
    if store_key == "__both__":
        if len(member) < 2:
            return jsonify({"ok": False, "error": "You aren't assigned to both stores."}), 403
    elif store_key not in member:
        return jsonify({"ok": False, "error": "You aren't assigned to that store."}), 403
    session["active_store"] = store_key
    return jsonify({"ok": True, "next": "/employee/dashboard"}), 200


# --------------------------------------------------------------------------
# Frontend pages (ck) — GET /employee/login + GET /employee/dashboard.
# The JSON auth endpoints above (ckai) are the API these two pages call.
# Per this file's header: ckai owns the auth endpoints, ck owns these pages.
# --------------------------------------------------------------------------
_STORE_LABELS = {"tomball": "Tomball", "copperfield": "Copperfield"}


@employee_auth.route("/employee/login", methods=["GET"])
def login_page():
    """Passcode login screen (mobile, branded). Anonymous-reachable: the site
    gate exempts /employee/login (auth.py). The page POSTs {identifier,
    passcode} to submit_url (ckai's /employee/login/passcode); on success its
    JS redirects to dashboard_url. passcode_len mirrors the 5-digit PIN the
    employee set at /employee/setup. (B11 email-onboarding swap, 2026-05-30 --
    replaced the retired phone -> SMS-code flow; ckai owns the passcode
    endpoint, ck owns this page route + template.)"""
    # Sam #2606: a both-store employee who signed in at the keypad is bounced here
    # (?needpick=1) with an employee session but no active_store -> hand the template
    # their stores so it pops the "Which store today?" picker on load. Else (single-
    # store or anonymous) autopick is empty and the normal sign-in form shows.
    autopick = []
    eid = session.get("employee_id")
    if eid and not session.get("active_store"):
        skeys = _employee_store_keys(eid)
        if len(skeys) > 1:
            autopick = [{"key": s, "label": _STORE_LABELS.get(s, (s or "").title())}
                        for s in skeys]
    return render_template(
        "employee_login.html",
        submit_url="/employee/login/passcode",
        dashboard_url="/employee/dashboard",
        login_url="/employee/login",
        passcode_len=5,
        prefill_identifier="",
        autopick=autopick,
    )


@employee_auth.route("/employee/dashboard", methods=["GET"])
def dashboard_page():
    """Landing after login. Requires an employee session; with none we send
    them to /employee/login (NOT the staff keypad) — auth.py exempts
    /employee/dashboard from the site gate so this route owns the no-session
    redirect target. Every query is scoped to session['employee_id']: zero
    cross-employee or partner data (the frontend half of the B2 isolation
    guarantee)."""
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect("/employee/login")

    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == emp_id).first()
        if emp is None:
            # Stale/cleared employee — drop the session keys + bounce to login.
            for k in ("employee_id", "employee_session_version", "auth_ok"):
                session.pop(k, None)
            return redirect("/employee/login")

        stores = (db.query(EmployeeStoreAssignment)
                    .filter(EmployeeStoreAssignment.employee_id == emp.id)
                    .all())
        store_name = ", ".join(
            _STORE_LABELS.get(s.store_key, (s.store_key or "").title())
            for s in stores
        ) or None
        full_name = (emp.full_name or "").strip()
        first_name = full_name.split(" ")[0] if full_name else None

        view = SimpleNamespace(
            first_name=first_name,
            full_name=full_name or None,
            store_name=store_name,
        )
        return render_template(
            "employee_dashboard.html",
            employee=view,
            logout_url="/employee/logout",
            login_url="/employee/login",
        )
    finally:
        db.close()


@employee_auth.route("/employee/my-performance", methods=["GET"])
def my_performance():
    """Employee self-view of their Toast labor + performance + (est.) pay -- the
    personalized-app payload (Sam #2829). Surfaces ONLY for the logged-in
    employee, and ONLY where a manager has CONFIRMED their Cena<->Toast link (a
    cena_toast_link row, which is partner-verified). No confirmed link ->
    {ok:true, linked:false} (the dashboard panel then stays hidden). Reuses the
    SAME toast_employee_summary() the manager Link tab uses, so the employee
    sees identical numbers. Scoped strictly to session['employee_id'] -- zero
    cross-employee data (the B2 isolation guarantee)."""
    emp_id = session.get("employee_id")
    if not emp_id:
        return jsonify({"ok": False, "error": "not signed in"}), 401

    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == emp_id).first()
        if emp is None:
            return jsonify({"ok": False, "error": "unknown employee"}), 404
        links = (db.query(CenaToastLink)
                   .filter(CenaToastLink.cena_employee_id == emp.id)
                   .all())
        if not links:
            return jsonify({"ok": True, "linked": False}), 200

        # Aggregate across the employee's confirmed links (usually one store).
        from app.web.toast_link_routes import toast_employee_summary
        total_hours = 0.0
        timecards: list[dict] = []
        performance: dict = {"available": False}
        gross_pay = 0.0
        pay_available = False
        stores_seen: list[str] = []
        any_ok = False
        last_err = None
        for ln in links:
            payload, _status = toast_employee_summary(ln.store_key, ln.toast_id)
            if not payload.get("ok"):
                last_err = payload.get("error")
                continue
            any_ok = True
            stores_seen.append(ln.store_key)
            total_hours += float(payload.get("hours") or 0)
            timecards.extend(payload.get("timecards") or [])
            perf = payload.get("performance") or {}
            if perf.get("available") and not performance.get("available"):
                performance = perf
            pay = payload.get("payroll") or {}
            if pay.get("available"):
                pay_available = True
                gross_pay += float(pay.get("gross_pay") or 0)

        if not any_ok:
            # Linked, but every Toast pull failed (creds blank / Toast down).
            # Report linked:true so the panel shows a "pending sync" state
            # instead of vanishing -- and never 500.
            return jsonify({"ok": False, "linked": True,
                            "error": last_err or "Toast data unavailable"}), 502

        timecards.sort(key=lambda t: (t.get("in") or ""), reverse=True)  # newest first
        payroll = ({"available": True, "estimated": True,
                    "gross_pay": round(gross_pay, 2), "hours": round(total_hours, 2)}
                   if pay_available else
                   {"available": False,
                    "note": "Pay appears once Toast Payroll creds are wired."})
        return jsonify({
            "ok": True,
            "linked": True,
            "stores": stores_seen,
            "hours": round(total_hours, 2),
            "timecards": timecards,
            "performance": performance,
            "payroll": payroll,
        }), 200
    finally:
        db.close()


@employee_auth.route("/partner/schedules-v2/migration/run", methods=["POST"])
def migration_run_placeholder():
    """B2 placeholder — B3 (aick) fills in the real Sling migration trigger.
    Partner-gated (mirrors legal_routes:104 / ezcater_import:23 checks). The
    global firewall + this check both keep employees out.

    TODO(B3): confirm the partner+operator gate + wire scripts/sling_migrate.py."""
    import os
    import base64
    import csv as _csv
    import io
    from sqlalchemy import text as _text

    raw = (request.headers.get("Authorization", "") or "").strip()
    tok = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    expected = (os.getenv("INGEST_TOKEN") or "").strip()
    if not session.get("partner_auth_ok") and not (expected and tok == expected):
        abort(403)

    data = request.get_json(silent=True) or {}
    b64 = data.get("csv_b64")
    if not b64:
        return jsonify({"ok": False, "error": "csv_b64 (base64 of the CSV) required"}), 400
    try:
        text_csv = base64.b64decode(b64).decode("utf-8-sig")
        rows = list(_csv.DictReader(io.StringIO(text_csv)))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": "bad csv_b64: %s" % e}), 400
    if not rows:
        return jsonify({"ok": False, "error": "CSV parsed to 0 rows"}), 400

    from app import models as _m
    from scripts.sling_migrate import run_migration, MODEL_NAMES

    probe = SessionLocal()
    try:
        empty = probe.query(Employee).count() == 0
        eng = probe.get_bind()
    finally:
        probe.close()

    recreated = False
    if empty:
        # GUARDED: only when employees is EMPTY -> apply the B3 nullable-phone
        # schema. SQLite can't ALTER DROP NOT NULL, so drop + recreate the empty
        # V2 tables (FK order). Never runs against populated data.
        with eng.begin() as conn:
            for t in ("employee_sms_codes", "employee_positions",
                      "employee_store_assignments", "employee_phones", "employees"):
                conn.execute(_text("DROP TABLE IF EXISTS %s" % t))
        _m.Base.metadata.create_all(eng)
        recreated = True

    db = SessionLocal()
    try:
        models = {n: getattr(_m, n) for n in MODEL_NAMES}
        rep, flags, info = run_migration(rows, db, models, commit=True, log=lambda *a: None)
        return jsonify({"ok": True, "recreated_schema": recreated,
                        "report": rep, "flags": flags,
                        "merged": len(info["merged_sling_ids"]),
                        "csv_rows": info["csv_rows"]}), 200
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.exception("B3 migration_run failed")
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500
    finally:
        db.close()


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

    @app.before_request
    def _employee_session_version_gate():
        """Invalidate a stale employee session at a single chokepoint (samai
        guardrail #4): if the logged-in employee is gone, deactivated, or their
        session_version was bumped (e.g. a passcode reset), drop the employee
        session keys so the route's own employee_id guard then 401s/redirects.
        Real sessions always carry employee_session_version (set on login/setup)."""
        eid = session.get("employee_id")
        if not eid:
            return None
        db = SessionLocal()
        try:
            emp = db.query(Employee).filter_by(id=eid).first()
        finally:
            db.close()
        sv = session.get("employee_session_version")
        if emp is None or not emp.active or (sv is not None and sv != emp.session_version):
            for k in ("employee_id", "employee_session_version", "auth_ok"):
                session.pop(k, None)
        return None
