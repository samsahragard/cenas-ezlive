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
from app.models import Employee, EmployeeSmsCode, EmployeeStoreAssignment, EmployeePosition, CenaToastLink, Shift, Schedule, Position
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
    {ok:true, linked:false} (the dashboard panel stays hidden).

    Serves from the CK-pushed sanitized perf caches only. Linked but not synced
    yet / cache read hiccup -> {syncing:true}. The old ToastEmployeeSnapshot
    fallback is intentionally not used here because snapshots can contain
    internal Toast GUIDs and sales-derived report fields. Scoped strictly to
    session['employee_id'] -- zero cross-employee data (the B2 guarantee)."""
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

        # Phase 3 (Sam #2938/#2941): prefer the CK-pushed SANITIZED perf cache when
        # present. STRICT WHITELIST -- only employee-visible numbers + service
        # metrics. NO toast_id/GUID, NO attribution internals, NO sync plumbing
        # (attribution_json is a column this builder never reads).
        from app.models import PerfPeriodCache, PerfShiftCache
        try:
            pc_rows = (db.query(PerfPeriodCache)
                         .filter(PerfPeriodCache.cena_employee_id == emp.id).all())
        except Exception:
            pc_rows = []   # fresh-table/read hiccup -> clean pending state, never raw fallback
        if pc_rows:
            _ord = {"today": 0, "week": 1, "month": 2, "last30": 3}
            perf_periods = sorted(([{
                "period": r.period,
                "period_start": r.period_start, "period_end": r.period_end,
                "total_hours": round(float(r.total_hours or 0), 2),
                "reg_hours": round(float(r.reg_hours or 0), 2),
                "ot_hours": round(float(r.ot_hours or 0), 2),
                "base_pay": round(float(r.base_pay or 0), 2),
                "tips": round(float(r.tips or 0), 2),
                "service": (r.service_json or {}),
            } for r in pc_rows]), key=lambda x: _ord.get(x["period"], 99))
            # per-shift detail (Sam #2938 / samai #2954) -- STRICT WHITELIST: only
            # employee-own fields; NO toast_id / attribution / GUID in the payload.
            try:
                ps_rows = (db.query(PerfShiftCache)
                             .filter(PerfShiftCache.cena_employee_id == emp.id)
                             .order_by(PerfShiftCache.clock_in.desc()).all())
            except Exception:
                ps_rows = []
            shifts = [{
                "business_date": s.business_date, "clock_in": s.clock_in, "clock_out": s.clock_out,
                "reg_hours": round(float(s.reg_hours or 0), 2),
                "ot_hours": round(float(s.ot_hours or 0), 2),
                "total_hours": round(float(s.total_hours or 0), 2),
                "base_pay": round(float(s.base_pay or 0), 2),
                "tips": round(float(s.tips or 0), 2),
                "tips_declared": bool(getattr(s, "tips_declared", True)),   # N4
                "needs_review": bool(getattr(s, "needs_review", False)),    # N5 -- visible warning marker
                "review_reason": getattr(s, "review_reason", None),
            } for s in ps_rows]
            # Phase 5.1 ranking (Sam #3009/#3014): the SANITIZED rank output -- own
            # ranks + per-cohort leaderboards (peers carry ONLY name+rank+allowed
            # metrics; min-cohort-gated). Sanitized at the CK source + sales-wall-
            # guarded at the receiver; this read returns it verbatim, scoped to
            # emp.id. Absent rank cache -> key simply omitted.
            ranking = None
            try:
                from app.models import PerfRankCache, sanitize_rank_json
                rk = (db.query(PerfRankCache)
                        .filter(PerfRankCache.cena_employee_id == emp.id).first())
                if rk and rk.rank_json:
                    # N-c read-path belt (Sam #3028): strip every leaderboard peer row to the
                    # field whitelist before serving (fail-safe even if a bad row were stored).
                    ranking = sanitize_rank_json(rk.rank_json)
            except Exception:
                ranking = None
            # FLAG 2 (aick #3143 / samai #3142 note 2; live re-audit FAIL fix samai #3163):
            # strict server-side tip omission for non-tipped on /my-performance too. FAIL-SAFE:
            # omit tips UNLESS the role is EXPLICITLY tipped (ranking is a dict with truthy
            # is_tipped). This matches /performance-center's `bool(ranking.get("is_tipped"))`
            # semantics so the BOTH-endpoints BOH-omission invariant holds for is_tipped =
            # None / absent / False / ranking-None too (common before GATE-3 sets classifications),
            # not only explicit False. Only an explicitly-tipped account keeps its tip keys.
            if not (isinstance(ranking, dict) and ranking.get("is_tipped")):
                for _p in perf_periods:
                    _p.pop("tips", None)
                for _s in shifts:
                    _s.pop("tips", None)
                    _s.pop("tips_declared", None)
            resp = {"ok": True, "linked": True, "perf_periods": perf_periods, "shifts": shifts}
            if ranking is not None:
                resp["ranking"] = ranking
            return jsonify(resp), 200

        # Linked, but no sanitized cache is available yet. Fail closed: no old
        # ToastEmployeeSnapshot fallback, because those snapshots may carry Toast
        # GUIDs and sales-derived report fields that are not employee-visible.
        return jsonify({"ok": True, "linked": True, "syncing": True}), 200
    finally:
        db.close()


@employee_auth.route("/employee/performance-center", methods=["GET"])
def performance_center():
    """Unified self-view payload for the performance DETAIL pages (T108). ONE
    endpoint feeds all 11 metric routes -- the detail template is data-driven by
    a metric_key, so this returns every period's money / rankings / daily /
    attendance in a single shape and the page picks the slice it needs.

    Scoped strictly to session['employee_id'] (the B2 isolation guarantee: zero
    cross-employee or partner data). Serves ONLY from the SANITIZED CK-pushed
    caches (PerfPeriodCache / PerfShiftCache / PerfRankCache) -- never a live
    Toast pull, and never sales / eligible_sales / cashSales / GUID (those never
    reach these tables; tip_pct is the allowed RATIO only).

    ROLE-AWARE (Sam #3077 / #3120): a non-tipped (BOH) employee's payload OMITS
    every tip key entirely -- no tips, tip_pct, tips_per_hour in money/daily and
    no tip_pct / tips_per_hour rankings. The UI reads is_tipped and renders a
    coherent BOH dashboard, not a tipped one with empty holes."""
    emp_id = session.get("employee_id")
    if not emp_id:
        return jsonify({"ok": False, "error": "not signed in"}), 401

    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == emp_id).first()
        if emp is None:
            return jsonify({"ok": False, "error": "unknown employee"}), 404
        links = (db.query(CenaToastLink)
                   .filter(CenaToastLink.cena_employee_id == emp.id).all())
        if not links:
            return jsonify({"ok": True, "linked": False}), 200

        from app.models import PerfPeriodCache, PerfShiftCache
        try:
            pc_rows = (db.query(PerfPeriodCache)
                         .filter(PerfPeriodCache.cena_employee_id == emp.id).all())
        except Exception:
            pc_rows = []
        if not pc_rows:
            # linked but the sanitized cache has not been pushed yet -> pending
            return jsonify({"ok": True, "linked": True, "syncing": True}), 200
        try:
            ps_rows = (db.query(PerfShiftCache)
                         .filter(PerfShiftCache.cena_employee_id == emp.id)
                         .order_by(PerfShiftCache.clock_in.desc()).all())
        except Exception:
            ps_rows = []

        # ATTENDANCE published-schedule source (Sam: late-vs-scheduled hybrid). The
        # published schedule is the Shift model (app DB). Pull THIS employee's shifts
        # ONCE (scoped strictly to emp.id -- never a request-supplied id, so no IDOR;
        # own-view only) and fold to {business_date 'YYYY-MM-DD' -> earliest scheduled
        # start_at datetime}. Shift.start_at is a real datetime column (B6 alarm key),
        # so the scheduled DATE is start_at.date() and we can subtract clock_in
        # directly. A day with multiple shifts keys off the EARLIEST start (you're
        # "late" against your first scheduled shift). A read hiccup -> empty map, which
        # degrades every day to the needs_review fallback, never a 500.
        sched_start_by_date: dict[str, datetime] = {}
        try:
            sh_rows = (db.query(Shift)
                         .filter(Shift.employee_id == emp.id,
                                 Shift.start_at.isnot(None)).all())
            for sh in sh_rows:
                st = sh.start_at
                if st is None:
                    continue
                dkey = st.date().isoformat()
                prev = sched_start_by_date.get(dkey)
                if prev is None or st < prev:
                    sched_start_by_date[dkey] = st
        except Exception:
            sched_start_by_date = {}

        # Sanitized ranking (own ranks + per-cohort leaderboards). The read-path
        # sanitizer strips peer rows to the field whitelist; structure (is_tipped,
        # ranks, leaderboards) is preserved. Absent rank cache -> treat as BOH-safe:
        # no rankings, no tip keys.
        ranking = {}
        raw_ranking = {}
        try:
            from app.models import PerfRankCache, sanitize_rank_json
            rk = (db.query(PerfRankCache)
                    .filter(PerfRankCache.cena_employee_id == emp.id).first())
            if rk and rk.rank_json:
                raw_ranking = rk.rank_json or {}
                ranking = sanitize_rank_json(rk.rank_json) or {}
        except Exception:
            ranking = {}
            raw_ranking = {}
        is_tipped = bool(ranking.get("is_tipped"))

        full_name = (emp.full_name or "").strip()
        first_name = full_name.split(" ")[0] if full_name else None

        pc_by_period = {r.period: r for r in pc_rows}
        rj_ranks = ranking.get("ranks") or {}
        raw_ranks = raw_ranking.get("ranks") or {}
        rj_lb = ranking.get("leaderboards") or {}
        try:
            own_position_rows = (
                db.query(EmployeePosition.store_key, EmployeePosition.position_id)
                  .filter(EmployeePosition.employee_id == emp.id,
                          EmployeePosition.store_key.isnot(None))
                  .distinct()
                  .all()
            )
            own_store_keys = {
                (sk or "").strip().lower()
                for sk, _pid in own_position_rows
                if (sk or "").strip()
            }
            own_store_role_pairs = {
                ((sk or "").strip().lower(), pid)
                for sk, pid in own_position_rows
                if (sk or "").strip() and pid is not None
            }
        except Exception:
            own_store_keys = set()
            own_store_role_pairs = set()
        # metric_key (UI) -> rank_json metric name
        RJ = {"effective_hourly": "effective_hourly",
              "tip_pct": "tip_percent", "tips_per_hour": "tips_per_hour",
              "combined": "combined"}

        def _shifts_in(r):
            lo, hi = r.period_start, r.period_end  # ISO 'YYYY-MM-DD' strings
            out = []
            for s in ps_rows:
                bd = s.business_date
                if bd and (not lo or bd >= lo) and (not hi or bd <= hi):
                    out.append(s)
            return out

        # LIVE 'on shift now' (Sam live-today-hours): a completed Toast entry carries
        # total_hours = clock_out - clock_in, so an OPEN entry (no clock_out) computes
        # to 0 and would show 0.00h while the person is mid-shift. For the row in this
        # period that is OPEN (clock_in present, clock_out empty, cached total_hours ~0
        # so we never double-count a finished row that merely lost its clock_out), add
        # the elapsed clock_in -> now. Returns (extra_hours, business_date, clock_in_iso)
        # for the FIRST such open row; (0.0, None, None) if none. Defensive: an
        # unparseable clock_in is ignored (never a 500), elapsed is clamped >= 0 and
        # capped at 24h so a stale/garbage timestamp can't inflate the total. This
        # surfaces live hours ONLY once CK actually pushes the open row into
        # PerfShiftCache; CK ingestion of the open entry is out of scope here.
        def _live_open_hours(r, now=None):
            now = now or datetime.utcnow()
            for s in _shifts_in(r):
                if getattr(s, "clock_out", None):
                    continue  # completed -> already in total_hours
                if float(getattr(s, "total_hours", 0) or 0) > 0.05:
                    continue  # has cached hours -> trust the cache, don't double-add
                ci_iso = getattr(s, "clock_in", None)
                ci = _parse_dt(ci_iso)
                if ci is None:
                    continue
                elapsed_h = (now - ci).total_seconds() / 3600.0
                if elapsed_h <= 0:
                    continue
                elapsed_h = min(elapsed_h, 24.0)
                return round(elapsed_h, 2), s.business_date, ci_iso
            return 0.0, None, None

        def _parse_dt(s):
            """Best-effort ISO -> naive datetime; None on anything unparseable.
            Defensive by contract: a bad cached timestamp must NEVER 500 the route
            (the caller treats None as 'no usable clock_in' -> needs_review fallback).
            Strips a trailing 'Z' and drops tz so the subtraction vs Shift.start_at
            (a naive datetime column) stays naive-vs-naive."""
            if not s:
                return None
            try:
                dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return dt

        def _live_today_tips():
            """LIVE credit-card tips for THIS employee TODAY, scoped strictly to
            their OWN confirmed Toast server guid(s) (CenaToastLink.toast_id) -- the
            same payment.tipAmount source the manager Server-Performance page shows,
            never another employee's row. Reads the shared 30-min orders cache
            (refresh=False) so it piggybacks the manager pull -- no extra Toast load.
            Returns {cc_tips, tip_pct, ...} or None on no mapping / any error, so
            the caller falls back to the finalized completed-shift cache. Central
            business_date (UTC-5) matches the Toast cache key + the manager page."""
            try:
                guids = {l.toast_id for l in links if getattr(l, "toast_id", None)}
                if not guids:
                    return None
                stores = {(l.store_key or "").strip().lower()
                          for l in links if (l.store_key or "").strip()}
                loc_filter = next(iter(stores)) if len(stores) == 1 else None
                bd = (datetime.utcnow() - timedelta(hours=5)).strftime("%Y%m%d")
                from app.services import toast_reports
                res = toast_reports.server_tips_for_guids(guids, loc_filter, bd)
                return res if isinstance(res, dict) else None
            except Exception:
                logging.getLogger(__name__).warning(
                    "employee perf: live today tips failed", exc_info=True)
                return None

        def _attendance_for(r):
            """Hybrid attendance for one period: join each PerfShiftCache row in the
            window to THIS employee's published Shift on the same business_date.

              - published shift with a scheduled start -> late_minutes =
                max(0, round((clock_in - start_at)/60)); 'on time' if <=5 else 'late'.
              - NO published shift (or clock_in unparseable) -> NEVER fabricate
                lateness; fall back to the needs_review punch flag (status
                'needs review') else skip the day ('no schedule').

            late = #'late' rows; missed = #needs_review rows. Rows newest-first.
            Scoped to emp.id via ps_rows + sched_start_by_date (both emp-only)."""
            rows = []
            late = 0
            missed = 0
            for s in _shifts_in(r):
                bd = s.business_date
                ci_raw = getattr(s, "clock_in", None)
                co_raw = getattr(s, "clock_out", None)
                reason = getattr(s, "review_reason", None)
                nr = bool(getattr(s, "needs_review", False))
                sched = sched_start_by_date.get(bd) if bd else None
                ci_dt = _parse_dt(ci_raw)
                if sched is not None and ci_dt is not None:
                    # published shift + a usable clock-in -> real late math
                    late_minutes = max(0, round((ci_dt - sched).total_seconds() / 60))
                    status = "on time" if late_minutes <= 5 else "late"
                    if status == "late":
                        late += 1
                    rows.append({"date": bd, "status": status,
                                 "late_minutes": late_minutes,
                                 "clock_in": ci_raw, "clock_out": co_raw,
                                 "note": reason})
                elif nr:
                    # no published shift (or bad timestamp) -> needs_review signal,
                    # never an invented late count.
                    missed += 1
                    rows.append({"date": bd, "status": "needs review",
                                 "late_minutes": 0,
                                 "clock_in": ci_raw, "clock_out": co_raw,
                                 "note": reason})
                # else: worked a day with no published shift + no review flag -> skip
            rows.sort(key=lambda x: (x.get("date") or ""), reverse=True)  # newest first
            return {"late": late, "missed": missed, "rows": rows}

        def _rank_obj(rank_json, period, rj):
            if not isinstance(rank_json, dict):
                return {}
            return (((rank_json.get("ranks") or {}).get(period) or {}).get(rj)
                    or {})

        def _rank_value(rank_json, period, rj):
            obj = _rank_obj(rank_json, period, rj)
            return obj.get("value") if isinstance(obj, dict) else None

        def _synth_peer_rows(period, mk):
            """Fallback for rank caches that carry own ranks but no leaderboard rows.

            Uses the server-only cohort_key to collect peers from the same rank cache
            cohort, then emits only the employee-approved comparison fields. The
            cohort key and employee ids never leave this function.
            """
            rj = RJ[mk]
            own = ((raw_ranks.get(period) or {}).get(rj) or {})
            if own.get("status") in ("not_eligible", "cohort_too_small"):
                return []
            cohort_key = own.get("cohort_key")
            if not cohort_key and not own_store_role_pairs:
                return []
            try:
                q = (
                    db.query(PerfRankCache, Employee.full_name,
                             EmployeePosition.store_key,
                             EmployeePosition.position_id)
                      .join(Employee, Employee.id == PerfRankCache.cena_employee_id)
                      .outerjoin(EmployeePosition,
                                 EmployeePosition.employee_id == Employee.id)
                      .filter(Employee.active.is_(True))
                )
                if not cohort_key and own_store_keys:
                    q = q.filter(EmployeePosition.store_key.in_(own_store_keys))
                rows = q.all()
            except Exception:
                return []
            out = []
            seen_peer_ids = set()
            for peer_cache, peer_name, peer_store, peer_position_id in rows:
                if peer_cache.cena_employee_id in seen_peer_ids:
                    continue
                peer_json = peer_cache.rank_json or {}
                if bool(peer_json.get("is_tipped")) != is_tipped:
                    continue
                peer_obj = _rank_obj(peer_json, period, rj)
                peer_cohort = peer_obj.get("cohort_key")
                peer_store_key = (peer_store or "").strip().lower()
                if (not isinstance(peer_obj, dict)
                        or not peer_obj.get("rank")
                        or peer_obj.get("status") in ("not_eligible", "cohort_too_small")):
                    continue
                if cohort_key:
                    if peer_cohort != cohort_key:
                        continue
                elif (peer_store_key, peer_position_id) not in own_store_role_pairs:
                    continue
                seen_peer_ids.add(peer_cache.cena_employee_id)
                row = {
                    "rank": peer_obj.get("rank"),
                    "name": (peer_name or "").strip() or "Team member",
                    "is_me": peer_cache.cena_employee_id == emp.id,
                }
                comparison_fields = ["effective_hourly"]
                if is_tipped:
                    comparison_fields += ["tip_percent", "tips_per_hour", "combined"]
                for key in comparison_fields:
                    val = _rank_value(peer_json, period, key)
                    if val is not None:
                        row[key] = val
                if is_tipped:
                    combined_rank = _rank_obj(peer_json, period, "combined").get("rank")
                    if combined_rank is not None:
                        row["combined_rank"] = combined_rank
                out.append(row)
            out.sort(key=lambda x: x["rank"] if x["rank"] is not None else 9999)
            try:
                cohort_size = int(own.get("cohort_size") or 0)
            except (TypeError, ValueError):
                cohort_size = 0
            if cohort_size > 0 and len(out) > cohort_size:
                out = out[:cohort_size]
            return out

        def _peer_rows(period, mk):
            rj = RJ[mk]
            lb = (rj_lb.get(period) or {}).get(rj) or {}
            rows = lb.get("rows") or []
            ranked = []
            for x in rows:
                if not x.get("rank"):
                    continue
                row = {
                    "rank": x.get("rank"),
                    "name": x.get("name"),
                    "is_me": bool(x.get("is_me")),
                }
                # Peer rows are already sanitized to RANK_PEER_FIELDS. Preserve the
                # allowed comparison columns so rank detail pages can explain who is
                # in the cohort and how they are doing without exposing private
                # identifiers or internal calculation details.
                for key in ("effective_hourly", "tip_percent", "tips_per_hour",
                            "combined", "combined_rank"):
                    if key in x:
                        row[key] = x.get(key)
                ranked.append(row)
            if not ranked:
                ranked = _synth_peer_rows(period, mk)
            ranked.sort(key=lambda x: x["rank"] if x["rank"] is not None else 9999)
            return ranked

        periods = {}
        for period in ("today", "week", "month", "last30"):
            r = pc_by_period.get(period)
            if r is None:
                periods[period] = {"money": {}, "rankings": {},
                                   "attendance": {"late": 0, "missed": 0, "rows": []},
                                   "live": {"on_shift": False, "since": None},
                                   "daily": []}
                continue
            hours = round(float(r.total_hours or 0), 2)
            # TODAY only: if an open (in-progress) shift row is present, add its
            # elapsed clock_in -> now so the page reflects the live shift instead of
            # 0.00h. live_bd / live_since drive the per-day row + 'on shift now' badge.
            live_extra, live_bd, live_since = (
                _live_open_hours(r) if period == "today" else (0.0, None, None))
            if live_extra > 0:
                hours = round(hours + live_extra, 2)
            base = round(float(r.base_pay or 0), 2)
            tips = round(float(r.tips or 0), 2) if is_tipped else 0.0
            # TODAY only, tipped + currently ON SHIFT (same open-shift signal as
            # live hours): replace the finalized-cache tips with the employee's
            # LIVE credit-card tips so far today, pulled scoped to their own Toast
            # server guid(s). Mirrors the manager Server-Performance page. Falls
            # back to the cache value if unmapped / Toast unreachable. Cash tips are
            # never in Toast, and pooled/shared tips settle at shift end, so this is
            # an estimate -> the UI labels it 'live, finalizes at clock-out'.
            live_tip_pct = None
            tips_live = False
            if period == "today" and is_tipped and live_extra > 0:
                _lt = _live_today_tips()
                if _lt is not None:
                    tips = round(float(_lt.get("cc_tips") or 0.0), 2)
                    live_tip_pct = _lt.get("tip_pct")
                    tips_live = True
            total = round(base + tips, 2)
            money = {"hours": hours, "base_pay": base, "total_pay": total,
                     "effective_hourly": (round(total / hours, 2) if hours > 0 else None),
                     "shifts": len(_shifts_in(r))}
            metrics = ["effective_hourly"]
            if is_tipped:
                # tip keys ONLY for tipped roles (sales-clean: tips $ + ratios)
                money["tips"] = tips
                money["tips_per_hour"] = (round(tips / hours, 4) if hours > 0 else None)
                if tips_live:
                    money["tip_pct"] = (round(float(live_tip_pct), 1)
                                        if live_tip_pct is not None else None)
                    money["tips_live"] = True
                else:
                    tp = ((rj_ranks.get(period) or {}).get("tip_percent") or {}).get("value")
                    money["tip_pct"] = (round(float(tp), 1) if tp is not None else None)
                metrics += ["tip_pct", "tips_per_hour", "combined"]

            rankings = {}
            for mk in metrics:
                rj = RJ[mk]
                rr = (rj_ranks.get(period) or {}).get(rj) or {}
                ranked = _peer_rows(period, mk)
                rankings[mk] = {
                    "rank": rr.get("rank"), "status": rr.get("status"),
                    "cohort_size": rr.get("cohort_size"), "value": rr.get("value"),
                    # held-days history not built yet -> clean empty state
                    "days_ranked": 0, "days_at_current_rank": 0, "history": [],
                    "leaders": ranked[:3],
                    "bottom": (ranked[-3:] if len(ranked) > 3 else []),
                    "peers": ranked,
                }
            score = (rj_ranks.get(period) or {}).get("score") or {}
            if score:
                peer_key = "combined" if is_tipped and "combined" in rankings else "effective_hourly"
                rankings["standing"] = {
                    "status": score.get("status"),
                    "standing_percentile": score.get("standing_percentile"),
                    "band": score.get("band"),
                    "peers": (rankings.get(peer_key) or {}).get("peers") or [],
                    "leaders": (rankings.get(peer_key) or {}).get("leaders") or [],
                    "bottom": (rankings.get(peer_key) or {}).get("bottom") or [],
                }

            # daily breakdown from per-shift cache (grouped by business_date)
            daily = []
            byd = {}
            order = []
            for s in _shifts_in(r):
                if s.business_date not in byd:
                    byd[s.business_date] = []
                    order.append(s.business_date)
                byd[s.business_date].append(s)
            for bd in order:
                ss = byd[bd]
                dh = round(sum(float(x.total_hours or 0) for x in ss), 2)
                # mirror the period-level live add onto the open shift's own day so the
                # daily breakdown still sums to the (bumped) total shown above.
                if live_extra > 0 and live_bd is not None and bd == live_bd:
                    dh = round(dh + live_extra, 2)
                dbase = round(sum(float(x.base_pay or 0) for x in ss), 2)
                dtips = round(sum(float(x.tips or 0) for x in ss), 2) if is_tipped else 0.0
                # mirror the live tip replacement onto the open shift's day so the
                # daily total_pay matches the (live) tips shown in the summary tile.
                if tips_live and live_bd is not None and bd == live_bd:
                    dtips = tips
                drow = {"date": bd, "hours": dh, "base_pay": dbase,
                        "total_pay": round(dbase + dtips, 2),
                        "effective_hourly": (round((dbase + dtips) / dh, 2) if dh > 0 else None),
                        "shifts": len(ss)}
                # per-day clock punches (Sam #3254 / aick #3255): own clock_in/clock_out
                # ONLY, as a per-day LIST so multi-shift days stay accurate (never a
                # misleading single in/out). No pay/tips/sales/ids ride along; identical
                # source to /my-performance shifts[] (PerfShiftCache, already sanitized).
                drow["punches"] = [{"clock_in": x.clock_in, "clock_out": x.clock_out} for x in ss]
                if is_tipped:
                    drow["tips"] = dtips
                    drow["tips_per_hour"] = (round(dtips / dh, 4) if dh > 0 else None)
                    drow["tip_pct"] = None  # per-day tip% needs eligible_sales (internal) -> omit
                daily.append(drow)

            periods[period] = {"money": money, "rankings": rankings,
                               # attendance hybrid: late-vs-published-schedule join,
                               # with a needs_review fallback (no fabricated lateness).
                               "attendance": _attendance_for(r),
                               # live 'on shift now' marker (today only); since = own
                               # clock_in (already employee-visible via daily punches).
                               "live": {"on_shift": bool(live_extra > 0),
                                        "since": live_since if live_extra > 0 else None},
                               "daily": daily}

        return jsonify({"ok": True, "linked": True,
                        "employee": {"first_name": first_name, "full_name": full_name or first_name},
                        "is_tipped": is_tipped,
                        "periods": periods}), 200
    finally:
        db.close()


# All 11 performance DETAIL pages share ONE parameterized route + ONE data-driven
# template (the page picks its slice by metric_key). Tip metrics render a clean,
# role-aware "applies to tipped roles" state for BOH (the page reads is_tipped).
_PERF_DETAIL_METRICS = {
    "total_pay", "tips", "base_pay", "effective_hourly", "tips_per_hour",
    "tip_pct", "shifts", "attendance",
    "rank_standing", "rank_effective_hourly", "rank_tip_pct",
    "rank_tips_per_hour", "rank_combined",
}


@employee_auth.route("/employee/performance/<metric>", methods=["GET"])
def performance_detail(metric):
    """One route serves all 11 performance detail pages. Requires an employee
    session (mirrors /employee/dashboard's no-session redirect). The template is
    data-driven: it fetches /employee/performance-center (scoped to this
    employee) and renders the slice for metric_key. Unknown metric -> 404."""
    if not session.get("employee_id"):
        return redirect("/employee/login")
    if metric not in _PERF_DETAIL_METRICS:
        abort(404)
    return render_template("employee_performance_detail.html", metric_key=metric)


@employee_auth.route("/employee/roster", methods=["GET"])
def employee_roster():
    """Read-only 'who's on' roster for the dashboard Roster tab (Sam #3245 item 3 /
    #3251 scope). Returns coworker NAME + position + store + their NEXT published
    shift (today/upcoming), scoped strictly to THIS employee's store(s). SANITIZED:
    no pay / tips / sales / performance metrics, and NO employee_id / GUID / hidden
    identifier in the payload; accepts no request id (session-scoped only -> no IDOR)."""
    emp_id = session.get("employee_id")
    if not emp_id:
        return jsonify({"ok": False, "error": "not signed in"}), 401
    stores = _employee_store_keys(emp_id)
    if not stores:
        return jsonify({"ok": True, "coworkers": []}), 200
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    horizon = start + timedelta(days=8)  # today + the next 7 days

    def _when(dt):
        d = dt.date()
        if d == today:
            return "Today"
        if d == today + timedelta(days=1):
            return "Tomorrow"
        return dt.strftime("%a, %b ") + str(d.day)

    def _tm(dt):
        return dt.strftime("%I:%M %p").lstrip("0")

    db = SessionLocal()
    try:
        rows = (db.query(Shift, Employee.full_name, Position.name, Schedule.store_key)
                  .join(Schedule, Shift.schedule_id == Schedule.id)
                  .join(Employee, Shift.employee_id == Employee.id)
                  .outerjoin(Position, Shift.position_id == Position.id)
                  .filter(Schedule.store_key.in_(stores),
                          Schedule.status == "published",
                          Shift.status == "assigned",
                          Shift.employee_id.isnot(None),
                          Shift.start_at >= start,
                          Shift.start_at < horizon)
                  .order_by(Shift.start_at.asc())
                  .all())
    finally:
        db.close()

    seen = set()
    coworkers = []
    for sh, full_name, pos_name, store_key in rows:
        if sh.employee_id in seen:    # one row per coworker -> their NEXT upcoming shift
            continue
        seen.add(sh.employee_id)
        name = (full_name or "").strip()
        if not name:
            continue
        coworkers.append({
            "name": name,
            "position": (pos_name or "").strip(),
            "store": _STORE_LABELS.get(store_key, (store_key or "").title()),
            "shift_when": _when(sh.start_at),
            "shift_label": _tm(sh.start_at) + " - " + _tm(sh.end_at),
        })
    return jsonify({"ok": True, "coworkers": coworkers}), 200


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
