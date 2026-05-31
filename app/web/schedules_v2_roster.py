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
from app.models import (Employee, EmployeePosition, EmployeeStoreAssignment,
                        Position)
from app.web.permissions import require_level
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
