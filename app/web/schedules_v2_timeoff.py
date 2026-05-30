"""Schedules V2 - Block 7: the MANAGER time-off review endpoints (ckai).

  GET  /<store>/schedules-v2/time-off/list [?status=...]  -> this store's requests (JSON; the
       PAGE at the bare /<store>/schedules-v2/time-off is ck's HTML - /list avoids the collision)
  POST /<store>/schedules-v2/time-off/<id>/approve         {manager_notes?}
  POST /<store>/schedules-v2/time-off/<id>/deny            {manager_notes?}

These ride the EXISTING store_bp blueprint (/<store_slug>/ prefix) exactly like
the B4 manager endpoints (schedules_v2.py), so they inherit store_bp's gates:
_pull_store (404 on bad slug) + _per_store_gate (a Tomball gm hitting /uno/... is
403/redirected BEFORE the view, zero rows touched) + the partner second factor.
On top, @require_level('foh_manager') gates to managers (expo/driver 403,
employees redirected to login).

STORE SCOPING: time-off is per-EMPLOYEE (an employee is off regardless of store),
but the manager view + approve/deny are scoped to the employees ASSIGNED to this
store - the request's employee must have an employee_store_assignments row for
g.current_location (the location, matching _store() in schedules_v2.py), else the
request is invisible here (404 on approve/deny - never reveal another store's
rows). Only an APPROVED request blocks shift-create (scheduling_timeoff.conflict).
"""
from __future__ import annotations

from datetime import datetime

from flask import g, jsonify, request

from app.db import SessionLocal
from app.models import Employee, EmployeeStoreAssignment, TimeOffRequest
from app.web.permissions import current_user_id, require_level
from app.web.store_routes import store_bp

_MGR = "foh_manager"  # mirror schedules_v2.py's manager gate


def _store() -> str | None:
    """LOCATION ('tomball'/'copperfield') - joins employee_store_assignments.store_key
    (B2 contract). Mirror of schedules_v2.py._store()."""
    return getattr(g, "current_location", None)


def _serialize(r, employee_name=None):
    return {
        "id": r.id,
        "employee_id": r.employee_id,
        "employee_name": employee_name,
        "start_date": r.start_date.isoformat() if r.start_date else None,
        "end_date": r.end_date.isoformat() if r.end_date else None,
        "reason": r.reason,
        "status": r.status,
        "manager_notes": r.manager_notes,
        "reviewed_by": r.reviewed_by,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _request_in_store(db, req_id):
    """The TimeOffRequest IFF its employee is assigned to the current store, else
    None (so a cross-store id is a 404, never revealed/mutated)."""
    return (db.query(TimeOffRequest)
              .join(EmployeeStoreAssignment,
                    EmployeeStoreAssignment.employee_id == TimeOffRequest.employee_id)
              .filter(TimeOffRequest.id == req_id,
                      EmployeeStoreAssignment.store_key == _store())
              .first())


@store_bp.route("/schedules-v2/time-off/list", methods=["GET"])
@require_level(_MGR)
def sv2_time_off_list():
    """This store's time-off requests (employees assigned here). ?status filters."""
    status = (request.args.get("status") or "").strip()
    db = SessionLocal()
    try:
        q = (db.query(TimeOffRequest, Employee.full_name)
               .join(EmployeeStoreAssignment,
                     EmployeeStoreAssignment.employee_id == TimeOffRequest.employee_id)
               .join(Employee, Employee.id == TimeOffRequest.employee_id)
               .filter(EmployeeStoreAssignment.store_key == _store()))
        if status:
            q = q.filter(TimeOffRequest.status == status)
        rows = q.order_by(TimeOffRequest.start_date.asc(), TimeOffRequest.id.asc()).all()
        return jsonify({"ok": True,
                        "requests": [_serialize(r, name) for (r, name) in rows]}), 200
    finally:
        db.close()


def _review(req_id, new_status, allowed_from):
    """Shared approve/deny body. allowed_from = statuses this transition is valid
    from. Same status -> 200 no-op (idempotent); a terminal/foreign state -> 409;
    not-in-this-store -> 404."""
    data = request.get_json(silent=True) or {}
    notes = (data.get("manager_notes") or "").strip() or None
    db = SessionLocal()
    try:
        r = _request_in_store(db, req_id)
        if r is None:
            return jsonify({"ok": False, "error": "request not found in this store"}), 404
        if r.status == new_status:
            return jsonify({"ok": True, "request": _serialize(r)}), 200  # idempotent
        if r.status not in allowed_from:
            return jsonify({"ok": False,
                            "error": "cannot %s a %s request" % (new_status, r.status)}), 409
        r.status = new_status
        if notes is not None:
            r.manager_notes = notes
        r.reviewed_by = current_user_id()
        r.reviewed_at = datetime.utcnow()
        r.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "request": _serialize(r)}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/time-off/<int:req_id>/approve", methods=["POST"])
@require_level(_MGR)
def sv2_time_off_approve(req_id):
    """Approve a pending (or previously-denied) request. An approved request then
    blocks conflicting shift-create. Cancelled -> 409."""
    return _review(req_id, "approved", allowed_from=("pending", "denied"))


@store_bp.route("/schedules-v2/time-off/<int:req_id>/deny", methods=["POST"])
@require_level(_MGR)
def sv2_time_off_deny(req_id):
    """Deny a pending (or reverse a previously-approved) request. Cancelled -> 409."""
    return _review(req_id, "denied", allowed_from=("pending", "approved"))
