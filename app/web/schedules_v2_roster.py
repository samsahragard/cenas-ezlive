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

from datetime import datetime

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import (CANONICAL_POSITIONS, CenaToastLink, Employee,
                        EmployeeAvailability, EmployeePosition,
                        EmployeeStoreAssignment, EmployeeUnavailabilityBlock,
                        Position, User)
from app.web.permissions import current_user_id, require_level
from app.web.schedules_v2 import (_MGR, _store, _highest_section_role,
                                 apply_section_placement_to_user)
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
    invite = send_setup_invite(emp_id)
    setup_code = (invite or {}).get("code")
    # Dual-channel (Sam 2026-06-07): the SAME single-use token backs BOTH the
    # emailed link AND this manager-displayed code. Whichever the employee uses
    # FIRST sets the PIN; the other stops working. Surface the code so the manager
    # can read it out; omit it gracefully if no invite (e.g. no email on file).
    resp = {"ok": True,
            "message": "We emailed a link AND generated a code. Give the code to the "
                       "employee, or they can use the link. Whichever they use first "
                       "sets the PIN; the other stops working."}
    if setup_code:
        resp["setup_code"] = setup_code
    else:
        resp["message"] = ("A fresh setup link has been emailed and any existing "
                           "session was signed out.")
    return jsonify(resp), 200


@store_bp.route("/schedules-v2/employees/<int:emp_id>/deactivate", methods=["POST"])
@require_level(_MGR)
def sv2_employee_deactivate(emp_id):
    """DEACTIVATE (Sam #2626): soft-remove a team member -- set active=False so they
    drop off the roster, and bump session_version to kill any live session (they can no
    longer sign in). Reversible (re-add / reactivate) -- NOT a hard delete, so history
    and FKs are preserved. Manager-gated; keyed off the URL emp_id; never touches
    session['partner_auth_ok']. -> 200 {ok}; 404 unknown employee."""
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=emp_id).first()
        if emp is None:
            return jsonify({"ok": False, "error": "employee not found"}), 404
        emp.active = False
        emp.session_version = (emp.session_version or 0) + 1  # kill any live session
        db.commit()
        return jsonify({"ok": True, "message": "Removed from the team."}), 200
    finally:
        db.close()


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
        # S5 SECTION-SCOPED assign: the FE may send an explicit 'section'
        # ('management'|'hourly'|'driver'); if absent we derive it from the ADD
        # positions. A mixed ADD (management + hourly in one call) is rejected.
        req_section = (data.get("section") or "").strip().lower() or None
        if req_section is not None and req_section not in ("management", "hourly", "driver"):
            return jsonify({"ok": False, "error": "Unknown section: %s." % req_section}), 400

        add_pids = list(dict.fromkeys([p for (p, _s) in add_pairs]))
        valid_pids = set()
        if add_pids:
            from app.services.permission_catalog import addable_roles, position_role
            from app.services.role_buckets import section_for_position
            _canon_lc = {c.lower() for c in CANONICAL_POSITIONS}
            found = {p.id: p for p in
                     db.query(Position).filter(Position.id.in_(add_pids)).all()}
            for pid, p in found.items():
                if (p.name or "").strip().lower() in _canon_lc:
                    valid_pids.add(pid)
            if [pid for pid in add_pids if pid not in valid_pids]:
                return jsonify({"ok": False, "error": "Unknown or non-canonical position(s)."}), 400
            _valid_names = [p.name for pid, p in found.items() if pid in valid_pids]
            # Section-consistency: all ADD positions in ONE section (skip tier-
            # above / no-section positions: partner/corporate/Expo).
            _pos_sections = {section_for_position(nm) for nm in _valid_names}
            _pos_sections.discard(None)
            if len(_pos_sections) > 1:
                return jsonify({"ok": False,
                                "error": "One assign is one section - don't mix %s."
                                         % " + ".join(sorted(_pos_sections))}), 400
            derived_section = next(iter(_pos_sections), None)
            if req_section is not None and derived_section is not None and req_section != derived_section:
                return jsonify({"ok": False,
                                "error": "Positions are %s, not %s." % (derived_section, req_section)}), 400
            section = req_section or derived_section
            # Rank-gate (strictly-below-rank) tightened by section: each ADD
            # position's role must be addable AND -- when a section is in play --
            # within that section tier (role_buckets).
            _allowed = addable_roles(getattr(db.get(User, current_user_id()), "permission_level", None))
            _over = sorted({nm for nm in _valid_names
                            if position_role(nm) and position_role(nm) not in _allowed})
            if _over:
                return jsonify({"ok": False,
                                "error": "Your role can only assign positions below your own - not: %s."
                                         % ", ".join(_over)}), 403
            if section is not None:
                _wrong = sorted({nm for nm in _valid_names
                                 if section_for_position(nm) not in (None, section)})
                if _wrong:
                    return jsonify({"ok": False,
                                    "error": "Positions outside the %s section: %s."
                                             % (section, ", ".join(_wrong))}), 400

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

        # S5 PLACEMENT->PERMISSION: flush the new EmployeePosition/Assignment rows
        # so the derivation reads the post-assign state, then push the derived
        # permission_level + store_scope onto the linked User (no-op if there is
        # none -- a pure scheduling employee). The tier GUARDS run inside
        # apply_section_placement_to_user; a violation -> clean 4xx, not a 500.
        from app.services import tier_invariants as ti
        try:
            db.flush()
            apply_section_placement_to_user(db, emp, db.get(User, current_user_id()))
            db.commit()
        except ti.TierInvariantError as e:
            db.rollback()
            return jsonify({"ok": False, "error": str(e)}), 409
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


# ==========================================================================
# Availability endpoints (D3, 2026-05-31): a MANAGER edits an existing
# scheduling employee's B8 availability from the Team roster -- the recurring
# weekly windows (EmployeeAvailability: when they CAN work) + the date-specific
# unavailability blocks (EmployeeUnavailabilityBlock: one-off spans they CANNOT
# work). Manager-controlled (NOT employee-self-set), keyed off the URL emp_id,
# gated @require_level(_MGR) ONLY -- availability.manage is a reserved catalog/
# display toggle (NOT in ROLE_PERMISSIONS), so a @requires_permission gate on it
# would deny managers (lockout); _MGR is the right gate, same as /update+/assign.
# (Per ck #3769.)
# Ride store_bp (inheriting _pull_store 404 + _per_store_gate cross-store) and
# NEVER touch session['partner_auth_ok']. Times are exchanged as "HH:MM"
# (stored as minutes-since-midnight) / ISO datetimes ("YYYY-MM-DDTHH:MM").
# ==========================================================================
def _min_to_hhmm(m: int) -> str:
    """Minutes-since-midnight -> 'HH:MM' (e.g. 540 -> '09:00')."""
    return "%02d:%02d" % (divmod(int(m), 60))


def _hhmm_to_min(s: str) -> int:
    """'HH:MM' -> minutes-since-midnight. Raises ValueError on a bad shape /
    out-of-range value (so the POST validator can turn it into a 400)."""
    parts = (s or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError("time must be 'HH:MM'")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("time out of range")
    return hh * 60 + mm


def _availability_payload(db, emp_id):
    """The shared GET/POST response body: current recurring windows (sorted by
    weekday, then start) + unavailability blocks for this employee."""
    recurring = sorted(
        db.query(EmployeeAvailability).filter_by(employee_id=emp_id).all(),
        key=lambda a: (a.day_of_week, a.start_minute))
    blocks = sorted(
        db.query(EmployeeUnavailabilityBlock).filter_by(employee_id=emp_id).all(),
        key=lambda b: b.start_at)
    return {
        "ok": True,
        "recurring": [{"day_of_week": a.day_of_week,
                       "start": _min_to_hhmm(a.start_minute),
                       "end": _min_to_hhmm(a.end_minute)} for a in recurring],
        "blocks": [{"id": b.id,
                    "start_at": b.start_at.isoformat(timespec="minutes"),
                    "end_at": b.end_at.isoformat(timespec="minutes"),
                    "reason": b.reason or ""} for b in blocks],
    }


@store_bp.route("/schedules-v2/employees/<int:emp_id>/availability", methods=["GET"])
@require_level(_MGR)
def sv2_employee_availability_get(emp_id):
    """LOAD AVAILABILITY: return an existing employee's B8 recurring weekly
    windows + date-specific unavailability blocks. -> 200 {ok, recurring:
    [{day_of_week, start:'HH:MM', end:'HH:MM'} ...sorted by day], blocks:
    [{id, start_at:'YYYY-MM-DDTHH:MM', end_at, reason}]}; 404 unknown employee.
    """
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=emp_id).first()
        if emp is None:
            return jsonify({"ok": False, "error": "employee not found"}), 404
        return jsonify(_availability_payload(db, emp.id)), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/employees/<int:emp_id>/availability", methods=["POST"])
@require_level(_MGR)
def sv2_employee_availability_set(emp_id):
    """WHOLESALE-REPLACE AVAILABILITY: replace ALL of an existing employee's B8
    recurring windows + unavailability blocks with the posted sets. Body:
        {recurring:[{day_of_week:0-6, start:'HH:MM', end:'HH:MM'} ...],
         blocks:[{start_at:'YYYY-MM-DDTHH:MM', end_at:'...', reason:'...'} ...]}
    Both keys optional (default []). Every item is validated BEFORE any write --
    on a single bad item we commit NOTHING and return 400. day_of_week int 0-6;
    start/end parse as 'HH:MM' with end>start; start_at/end_at parse as ISO
    datetime with end_at>start_at. -> 200 same shape as GET; 400 bad item;
    404 unknown employee; 409 commit conflict.
    """
    data = request.get_json(silent=True) or {}
    recurring_in = data.get("recurring") or []
    blocks_in = data.get("blocks") or []
    if not isinstance(recurring_in, list) or not isinstance(blocks_in, list):
        return jsonify({"ok": False, "error": "recurring/blocks must be lists."}), 400

    # Validate EVERYTHING first (parse into the new rows) so a bad item aborts
    # before we delete or insert anything -- commit nothing on a 400.
    new_recurring = []
    for r in recurring_in:
        if not isinstance(r, dict):
            return jsonify({"ok": False,
                            "error": "each recurring item must be an object."}), 400
        try:
            dow = int(r.get("day_of_week"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "day_of_week must be an int 0-6."}), 400
        if not (0 <= dow <= 6):
            return jsonify({"ok": False, "error": "day_of_week must be 0-6."}), 400
        try:
            start_minute = _hhmm_to_min(r.get("start"))
            end_minute = _hhmm_to_min(r.get("end"))
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": "recurring start/end must be 'HH:MM'."}), 400
        if end_minute <= start_minute:
            return jsonify({"ok": False,
                            "error": "recurring end must be after start."}), 400
        new_recurring.append((dow, start_minute, end_minute))

    new_blocks = []
    for b in blocks_in:
        if not isinstance(b, dict):
            return jsonify({"ok": False,
                            "error": "each block must be an object."}), 400
        try:
            start_at = datetime.fromisoformat((b.get("start_at") or "").strip())
            end_at = datetime.fromisoformat((b.get("end_at") or "").strip())
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": "block start_at/end_at must be ISO datetimes."}), 400
        if end_at <= start_at:
            return jsonify({"ok": False,
                            "error": "block end_at must be after start_at."}), 400
        reason = (b.get("reason") or "").strip() or None
        new_blocks.append((start_at, end_at, reason))

    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=emp_id).first()
        if emp is None:
            return jsonify({"ok": False, "error": "employee not found"}), 404

        # Wholesale replace: drop the old sets, insert the validated new ones.
        (db.query(EmployeeAvailability)
           .filter_by(employee_id=emp.id).delete(synchronize_session=False))
        (db.query(EmployeeUnavailabilityBlock)
           .filter_by(employee_id=emp.id).delete(synchronize_session=False))
        for (dow, sm, em) in new_recurring:
            db.add(EmployeeAvailability(employee_id=emp.id, day_of_week=dow,
                                        start_minute=sm, end_minute=em))
        for (sa, ea, reason) in new_blocks:
            db.add(EmployeeUnavailabilityBlock(employee_id=emp.id, start_at=sa,
                                               end_at=ea, reason=reason))

        try:
            db.commit()
        except Exception:
            db.rollback()
            return jsonify({"ok": False,
                            "error": "Could not save availability (data conflict)."}), 409
        return jsonify(_availability_payload(db, emp.id)), 200
    finally:
        db.close()


# ==========================================================================
# Cena<->Toast Link tab (Sam #2629): persist a manager-CONFIRMED match between
# a Cena employee and a Toast employee, scoped to THIS store (_store() = the
# location). ckbro's GET .../toast/match-suggestions proposes matches; a manager
# verifies one and we store it here (CenaToastLink, UNIQUE(cena_employee_id,
# store_key)) so the Link tab can mark which suggestions are confirmed + later
# load that person's Toast data by toast_id. Manager-gated @require_level(_MGR),
# same as the roster writes above; confirmed_by = current_user_id().
# ==========================================================================
@store_bp.route("/schedules-v2/toast/link", methods=["POST"])
@require_level("partner")   # Sam #2675: only the partner (owner) confirms a Toast match
def sv2_toast_link():
    """UPSERT a confirmed Cena<->Toast link for (cena_emp_id, _store()). Body:
        {cena_emp_id: int (required), toast_id: str (required), toast_name?: str}
    If a link already exists for (cena_employee_id, store_key) update its
    toast_id/toast_name/confirmed_by/confirmed_at; else insert a new row.
    -> 200 {ok, link:{cena_emp_id, toast_id, toast_name, store_key}};
    400 if cena_emp_id or toast_id is missing."""
    data = request.get_json(silent=True) or {}
    try:
        cena_emp_id = int(data.get("cena_emp_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "cena_emp_id required"}), 400
    toast_id = str(data.get("toast_id") or "").strip()
    if not toast_id:
        return jsonify({"ok": False, "error": "toast_id required"}), 400
    toast_name = (data.get("toast_name") or "").strip() or None
    store = _store()
    if not store:
        # defensive: store_bp's _pull_store should always set this on a valid slug.
        return jsonify({"ok": False, "error": "store not resolved"}), 400
    db = SessionLocal()
    try:
        row = (db.query(CenaToastLink)
                 .filter_by(cena_employee_id=cena_emp_id, store_key=store).first())
        now = datetime.utcnow()
        uid = current_user_id()
        if row is None:
            row = CenaToastLink(cena_employee_id=cena_emp_id, store_key=store,
                                toast_id=toast_id, toast_name=toast_name,
                                confirmed_by=uid, confirmed_at=now)
            db.add(row)
        else:
            row.toast_id = toast_id
            row.toast_name = toast_name
            row.confirmed_by = uid
            row.confirmed_at = now
        db.commit()
        return jsonify({"ok": True, "link": {"cena_emp_id": cena_emp_id,
                                             "toast_id": toast_id,
                                             "toast_name": toast_name,
                                             "store_key": store}}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/toast/unlink", methods=["POST"])
@require_level("partner")   # Sam #2675: partner-only (paired with link)
def sv2_toast_unlink():
    """Delete the confirmed Cena<->Toast link for (cena_emp_id, _store()).
    Body: {cena_emp_id: int (required)}. Idempotent: 200 {ok} even if no link
    existed. 400 if cena_emp_id is missing."""
    data = request.get_json(silent=True) or {}
    try:
        cena_emp_id = int(data.get("cena_emp_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "cena_emp_id required"}), 400
    store = _store()
    if not store:
        return jsonify({"ok": False, "error": "store not resolved"}), 400
    db = SessionLocal()
    try:
        (db.query(CenaToastLink)
           .filter_by(cena_employee_id=cena_emp_id, store_key=store)
           .delete(synchronize_session=False))
        db.commit()
        return jsonify({"ok": True}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/toast/links", methods=["GET"])
@require_level(_MGR)
def sv2_toast_links():
    """List all confirmed Cena<->Toast links for THIS store (_store()) so the FE
    can mark which suggestions are already confirmed.
    -> {ok, links:[{cena_emp_id, toast_id, toast_name}]}."""
    store = _store()
    if not store:
        return jsonify({"ok": False, "error": "store not resolved"}), 400
    db = SessionLocal()
    try:
        rows = (db.query(CenaToastLink)
                  .filter_by(store_key=store)
                  .order_by(CenaToastLink.cena_employee_id).all())
        links = [{"cena_emp_id": r.cena_employee_id,
                  "toast_id": r.toast_id,
                  "toast_name": r.toast_name} for r in rows]
        return jsonify({"ok": True, "links": links}), 200
    finally:
        db.close()
