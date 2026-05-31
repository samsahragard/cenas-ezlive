"""Schedules V2 — manager roster-assignment write route (ckai, email-pivot follow-up).

Closes the gap samai verified (Q3): there is NO employee.store / employee.position
attribute — schedulability comes from an EmployeeStoreAssignment row (read in
schedules_v2.py sv2_board: roster = employees that have an assignment for the
store_key). That row was created in exactly ONE place — the B3 Sling migration
(scripts/sling_migrate.py) — no web route ever made one. So an admin-added hire
({full_name, email}) got an Employee row but NO assignment -> invisible to every
store roster -> a manager could never give them a shift -> they logged in to a
permanently-empty schedule with no path in. This route is the missing manager action.

Manager-purview, matching samai's privilege split (store + position(s) are
manager-set, NEVER employee-self-set — position drives B9 eligibility, store drives
B4 assignment). Rides store_bp, so it inherits _pull_store (404 on unknown slug) +
_per_store_gate (403-BEFORE-mutation on cross-store) for free, and @require_level(_MGR)
gates to managers (employees / expo / drivers can't reach it). The store is the URL
location (_store() = g.current_location, NOT the slug) — exactly the key the board
reads the roster by — so a Tomball manager can only roster INTO Tomball.

ck builds the manager control (on the board/roster) against POST
/<store>/schedules-v2/roster. aick gates. Does NOT block the auth/login/setup merge —
it's the last link in the onboarding -> schedulable chain (samai's end-to-end).
"""
from __future__ import annotations

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import (CANONICAL_POSITIONS, Employee, EmployeePosition,
                        EmployeeStoreAssignment, Position, User)
from app.web.permissions import current_user_id, require_level
from app.web.schedules_v2 import _MGR, _store
from app.web.store_routes import store_bp


@store_bp.route("/schedules-v2/roster", methods=["POST"])
@require_level(_MGR)
def sv2_roster_add():
    """Add an employee to THIS store's roster (the schedulability gate) + optionally
    assign positions. Idempotent: re-adding an already-rostered employee returns 200
    (not an error), respecting uq_emp_store / uq_emp_position. Body:
        {employee_id: int (required), position_ids: [int] (optional)}
    -> 201 {ok, created:true, ...} on first add; 200 {ok, created:false, ...} if the
    employee was already on the roster; 400 bad input / unknown-or-foreign position;
    404 unknown employee.
    """
    data = request.get_json(silent=True) or {}
    emp_id = data.get("employee_id")
    if not emp_id:
        return jsonify({"ok": False, "error": "employee_id required"}), 400
    store = _store()
    if not store:
        # defensive: store_bp's _pull_store should always set this on a valid slug.
        return jsonify({"ok": False, "error": "store not resolved"}), 400
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=emp_id).first()
        if emp is None:
            return jsonify({"ok": False, "error": "employee not found"}), 404

        # 1) store assignment = the roster gate (idempotent on uq_emp_store)
        existing = (db.query(EmployeeStoreAssignment)
                      .filter_by(employee_id=emp.id, store_key=store).first())
        created = existing is None
        if created:
            db.add(EmployeeStoreAssignment(employee_id=emp.id, store_key=store))

        # 2) optional positions — every id must EXIST and be in-scope for this store
        #    (Position.store_key null = all-store; else must match). No silent skip:
        #    a bad/foreign id is a 400 (nothing committed). Idempotent on uq_emp_position.
        want_pos = []
        for p in (data.get("position_ids") or []):
            try:
                want_pos.append(int(p))
            except (TypeError, ValueError):
                db.rollback()
                return jsonify({"ok": False, "error": f"invalid position id: {p!r}"}), 400
        if want_pos:
            found = {p.id: p for p in
                     db.query(Position).filter(Position.id.in_(want_pos)).all()}
            missing = sorted(set(want_pos) - set(found))
            if missing:
                db.rollback()
                return jsonify({"ok": False, "error": f"unknown position id(s): {missing}"}), 400
            foreign = sorted(pid for pid, p in found.items()
                             if p.store_key is not None and p.store_key != store)
            if foreign:
                db.rollback()
                return jsonify({"ok": False,
                                "error": f"position(s) not available at this store: {foreign}"}), 400
            have = {ep.position_id for ep in
                    db.query(EmployeePosition).filter_by(employee_id=emp.id).all()}
            for pid in want_pos:
                if pid not in have:
                    db.add(EmployeePosition(employee_id=emp.id, position_id=pid))

        try:
            db.commit()
        except IntegrityError:
            # a concurrent manager raced us on uq_emp_store / uq_emp_position; the row
            # exists either way -> benign. Re-read actual state below.
            db.rollback()

        # echo back the resulting roster entry + positions so ck's control reflects state
        pos_ids = [ep.position_id for ep in
                   db.query(EmployeePosition).filter_by(employee_id=emp.id).all()]
        positions = ([{"id": p.id, "name": p.name} for p in
                      db.query(Position).filter(Position.id.in_(pos_ids))
                        .order_by(Position.name).all()]
                     if pos_ids else [])
        return jsonify({
            "ok": True,
            "created": created,
            "store_key": store,
            "employee": {"id": emp.id, "full_name": emp.full_name, "active": emp.active},
            "positions": positions,
        }), (201 if created else 200)
    finally:
        db.close()


@store_bp.route("/schedules-v2/team-roster", methods=["GET"])
@require_level(_MGR)
def sv2_team_roster():
    """Unified Team-tab roster (Project 1 unify, Sam #2261): wraps aick's team_roster()
    read -> jsonify, the exact shape ck's FE binds to (counts{all,boh,foh} + stats +
    stores[] with per-member multi-position pills + domain + access_role). Query params:
      location=all|tomball|copperfield (default all = the ONE team list; dropdown narrows)
      position=all|<name>, flt=all|boh|foh, include_inactive=0/1
    Manager-gated (require_level _MGR) + rides store_bp so the per-store audience gate is
    inherited. NB: location='all' shows BOTH stores (the unified team list) regardless of
    the manager's store_scope - flagged to aick/Sam; trivial to scope to store_scope if
    they prefer a gm see only their store(s)."""
    from app.services.team_roster import team_roster
    location = (request.args.get("location") or "all").strip()
    position = (request.args.get("position") or "all").strip()
    flt = (request.args.get("flt") or "all").strip()
    include_inactive = (request.args.get("include_inactive") or "").strip().lower() in ("1", "true", "yes", "on")
    db = SessionLocal()
    try:
        return jsonify(team_roster(db, location=location, position=position,
                                   include_inactive=include_inactive, flt=flt)), 200
    finally:
        db.close()


# ==========================================================================
# Roster EDIT endpoints (2026-05-31, roster-edit branch): a manager edits an
# EXISTING scheduling employee from the Team roster -- contact, PIN reset, and
# per-store (position, store) assignment. All three are MANAGER actions
# (@require_level(_MGR)), keyed off the URL employee_id, and NEVER touch
# session['partner_auth_ok'] (ckai security invariant): editing a team member
# is not a partner-auth event. They ride store_bp, inheriting _pull_store (404
# on unknown slug) + _per_store_gate (403-before-mutation cross-store).
# ==========================================================================
def _emp_email_valid(email: str) -> bool:
    """Same shape-check sv2_employee_add uses (schedules_v2.py:126-128)."""
    parts = (email or "").split("@")
    return len(parts) == 2 and bool(parts[0]) and "." in parts[1] and not parts[1].endswith(".")


@store_bp.route("/schedules-v2/employees/<int:emp_id>/update", methods=["POST"])
@require_level(_MGR)
def sv2_employee_update(emp_id):
    """EDIT CONTACT: update an existing employee's phone / email / address from
    the Team roster. Body {phone, email, address} -- each optional; only the keys
    PRESENT in the body are written (so the FE can patch one field). Email is the
    login identity, so it is validated (shape) + uniqueness-checked (case-
    insensitive, excluding this employee) exactly like sv2_employee_add; phone is
    the SMS-identity UNIQUE so a duplicate is rejected too. address is free text.
    -> 200 {ok, employee:{id,full_name,phone,email,address}}; 400 bad email;
    404 unknown employee; 409 duplicate email/phone.
    """
    data = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=emp_id).first()
        if emp is None:
            return jsonify({"ok": False, "error": "employee not found"}), 404

        # Only mutate fields the caller actually sent (presence, not truthiness:
        # address="" clears it; an absent key is left untouched).
        if "email" in data:
            email = (data.get("email") or "").strip()
            if not _emp_email_valid(email):
                return jsonify({"ok": False, "error": "A valid email is required."}), 400
            # Uniqueness: case-insensitive, excluding self (mirrors sv2_employee_add).
            if any((e.email or "").lower() == email.lower()
                   for e in db.query(Employee).all() if e.id != emp.id):
                return jsonify({"ok": False,
                                "error": "An employee with that email already exists."}), 409
            emp.email = email

        if "phone" in data:
            phone = (data.get("phone") or "").strip() or None
            if phone and any((e.phone or "") == phone
                             for e in db.query(Employee).all() if e.id != emp.id):
                return jsonify({"ok": False,
                                "error": "An employee with that phone already exists."}), 409
            emp.phone = phone

        if "address" in data:
            emp.address = (data.get("address") or "").strip() or None

        try:
            db.commit()
        except Exception:
            db.rollback()
            return jsonify({"ok": False,
                            "error": "Could not update (duplicate phone or data conflict)."}), 409
        return jsonify({"ok": True, "employee": {
            "id": emp.id, "full_name": emp.full_name,
            "phone": emp.phone, "email": emp.email, "address": emp.address,
        }}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/employees/<int:emp_id>/reset-pin", methods=["POST"])
@require_level(_MGR)
def sv2_employee_reset_pin(emp_id):
    """PIN RESET: mint a FRESH single-use email setup link (send_setup_invite ->
    a new /employee/setup/<token>, the same path the employee used originally to
    set their 5-digit passcode) AND bump emp.session_version. The version bump is
    CRITICAL: the employee before_request invalidates any session whose stored
    version != Employee.session_version (samai guardrail #4), so this immediately
    kills any stale logged-in session -- the employee must re-setup. Keyed off the
    URL emp_id; NEVER touches session['partner_auth_ok']. -> 200 {ok, message};
    404 unknown employee. The invite email just logs the link on missing SMTP
    (send_setup_invite never raises), which is fine.
    """
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=emp_id).first()
        if emp is None:
            return jsonify({"ok": False, "error": "employee not found"}), 404
        # Bump session_version FIRST so any stale session is dead even if the
        # invite send is slow. before_request keys off this exact column.
        emp.session_version = (emp.session_version or 0) + 1
        db.commit()
    finally:
        db.close()

    # Fire the invite AFTER our session closes (send_setup_invite opens its own
    # and never raises into us -- logs the link on SMTP failure). Same ordering
    # as sv2_employee_add.
    from app.web.employee_setup import send_setup_invite
    send_setup_invite(emp_id)
    return jsonify({"ok": True,
                    "message": "A fresh setup link has been emailed and any existing "
                               "session was signed out."}), 200


@store_bp.route("/schedules-v2/employees/<int:emp_id>/assign", methods=["POST"])
@require_level(_MGR)
def sv2_employee_assign(emp_id):
    """ASSIGN (existing): set per-store (position, store) assignments for an
    EXISTING employee. Body:
        {assignments:[{position_id, store_key}], remove?:[{position_id, store_key}]}
    ADD: writes one EmployeePosition per (position_id, store_key) pair WITH
    store_key (reusing sv2_employee_add's logic -- canonical-position validation +
    the rank-gate) and one EmployeeStoreAssignment per distinct store (the
    schedulability gate). REMOVE: deletes exactly those EmployeePosition rows
    (store assignments are left intact -- removing one role at a store should not
    un-schedule the person there). Idempotent on uq_emp_position_store / uq_emp_store.
    Rank-gate: a manager may assign only positions whose permission-role is
    STRICTLY BELOW their own rank (addable_roles/position_role) -- an over-rank
    position is 403 with NOTHING committed. Keyed off the URL emp_id; NEVER
    touches session['partner_auth_ok']. -> 200 {ok, positions:[{id,name,store_key}]};
    400 bad input / unknown-or-non-canonical / unknown store; 403 over-rank;
    404 unknown employee.
    """
    data = request.get_json(silent=True) or {}

    def _pairs(key):
        out = []
        v = data.get(key) or []
        if not isinstance(v, list):
            return None  # signal malformed
        for a in v:
            if not isinstance(a, dict):
                return None
            try:
                pid = int(a.get("position_id"))
            except (TypeError, ValueError):
                return None
            sk = str(a.get("store_key") or "").strip().lower()
            if not sk:
                return None
            out.append((pid, sk))
        return out

    add_pairs = _pairs("assignments")
    rem_pairs = _pairs("remove")
    if add_pairs is None or rem_pairs is None:
        return jsonify({"ok": False,
                        "error": "assignments/remove must be [{position_id, store_key}]."}), 400

    # Store-key allow-list (mirrors sv2_employee_add:129-131).
    bad_stores = sorted({s for (_p, s) in (add_pairs + rem_pairs)
                         if s not in ("tomball", "copperfield")})
    if bad_stores:
        return jsonify({"ok": False, "error": "Unknown store(s): %s." % ", ".join(bad_stores)}), 400

    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=emp_id).first()
        if emp is None:
            return jsonify({"ok": False, "error": "employee not found"}), 404

        # Validate + rank-gate the ADD positions against the CANONICAL catalog --
        # identical to sv2_employee_add (schedules_v2.py:147-172). REMOVE is not
        # rank-gated: a manager clearing a stale assignment shouldn't be blocked
        # by their own rank, and remove only deletes existing rows.
        add_pids = list(dict.fromkeys([p for (p, _s) in add_pairs]))
        valid_pids = set()
        if add_pids:
            from app.services.permission_catalog import addable_roles, position_role
            _canon_lc = {c.lower() for c in CANONICAL_POSITIONS}
            found = {p.id: p for p in
                     db.query(Position).filter(Position.id.in_(add_pids)).all()}
            for pid, p in found.items():
                if (p.name or "").strip().lower() in _canon_lc:
                    valid_pids.add(pid)
            if [pid for pid in add_pids if pid not in valid_pids]:
                return jsonify({"ok": False, "error": "Unknown or non-canonical position(s)."}), 400
            _allowed = addable_roles(getattr(db.get(User, current_user_id()), "permission_level", None))
            _over = sorted({p.name for pid, p in found.items()
                            if pid in valid_pids
                            and position_role(p.name) and position_role(p.name) not in _allowed})
            if _over:
                return jsonify({"ok": False,
                                "error": "Your role can only assign positions below your own - not: %s."
                                         % ", ".join(_over)}), 403

        # REMOVE first, then ADD (so a same-pair remove+add nets to present).
        for (pid, sk) in dict.fromkeys(rem_pairs):
            (db.query(EmployeePosition)
               .filter_by(employee_id=emp.id, position_id=pid, store_key=sk)
               .delete(synchronize_session=False))

        # ADD: EmployeePosition per (position, store) WITH store_key + one
        # EmployeeStoreAssignment per distinct store (the schedulability gate).
        have_pos = {(ep.position_id, ep.store_key) for ep in
                    db.query(EmployeePosition).filter_by(employee_id=emp.id).all()}
        have_stores = {sa.store_key for sa in
                       db.query(EmployeeStoreAssignment).filter_by(employee_id=emp.id).all()}
        for (pid, sk) in dict.fromkeys([(p, s) for (p, s) in add_pairs if p in valid_pids]):
            if (pid, sk) not in have_pos:
                db.add(EmployeePosition(employee_id=emp.id, position_id=pid, store_key=sk))
                have_pos.add((pid, sk))
        for sk in dict.fromkeys([s for (_p, s) in add_pairs]):
            if sk not in have_stores:
                db.add(EmployeeStoreAssignment(employee_id=emp.id, store_key=sk))
                have_stores.add(sk)

        try:
            db.commit()
        except IntegrityError:
            # concurrent manager raced us on a unique constraint -> row exists
            # either way; re-read the resulting state below.
            db.rollback()

        # Echo the resulting per-store positions (id+name+store_key) so the FE
        # reflects the actual committed state.
        rows = db.query(EmployeePosition).filter_by(employee_id=emp.id).all()
        names = {p.id: p.name for p in
                 db.query(Position).filter(
                     Position.id.in_([r.position_id for r in rows] or [-1])).all()}
        positions = sorted(
            [{"id": r.position_id, "name": names.get(r.position_id), "store_key": r.store_key}
             for r in rows],
            key=lambda d: ((d["name"] or ""), (d["store_key"] or "")))
        return jsonify({"ok": True, "positions": positions}), 200
    finally:
        db.close()
