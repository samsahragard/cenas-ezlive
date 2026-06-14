"""Schedules V2 - Block 7: the employee time-off endpoints (ckai).

  POST   /employee/time-off/request   {start_date, end_date, reason?}
  GET    /employee/time-off/list                           -> own requests, newest first (JSON)
  DELETE /employee/time-off/<id>                           -> cancel own PENDING request

(GET /employee/time-off itself is ck's HTML PAGE; the JSON list lives at the /list
child so the page + data don't collide on one GET path - the B5/B6 split pattern.)

Like the B5/B6 employee endpoints this ATTACHES to the existing employee_auth
blueprint (decorator side effect; imported before ezempauth.install in
app/__init__.py) so all /employee/* routes share one blueprint + namespace;
employee_auth.py stays untouched.

AUTH / ISOLATION: every endpoint self-guards session['employee_id'] (401 JSON
with no employee session) and every read/write is scoped to that employee. The
/employee/time-off prefix is in auth.py EXEMPT_PREFIXES so a session-less hit
gets this JSON 401 instead of the staff-keypad redirect; isolation is enforced
by the employee_id scope on every query, not by the site gate.

Only an APPROVED request blocks shift-create (scheduling_timeoff.conflict); a
cancel is a soft status flip to 'cancelled' (kept as history).
"""
from __future__ import annotations

from datetime import date as _date, datetime

from flask import jsonify, request, session

from app.db import SessionLocal
from app.models import TimeOffRequest
from app.web.employee_auth import employee_auth

_OPEN_STATUSES = ("pending", "approved")  # block a new overlapping request against these


def _require_emp():
    """(employee_id, None) for a logged-in employee, else (None, (json, 401))."""
    eid = session.get("employee_id")
    if not eid:
        return None, (jsonify({"ok": False, "error": "login required"}), 401)
    return eid, None


def _serialize(r):
    return {
        "id": r.id,
        "start_date": r.start_date.isoformat() if r.start_date else None,
        "end_date": r.end_date.isoformat() if r.end_date else None,
        "reason": r.reason,
        "status": r.status,
        "manager_notes": r.manager_notes,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _parse_date(v):
    """(date, None) or (None, errmsg)."""
    if not v or not str(v).strip():
        return None, "required"
    try:
        return _date.fromisoformat(str(v).strip()), None
    except ValueError:
        return None, "must be YYYY-MM-DD"


@employee_auth.route("/employee/time-off/request", methods=["POST"])
def emp_time_off_request():
    """Submit a pending time-off request for a date range."""
    emp_id, err = _require_emp()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    start, e1 = _parse_date(data.get("start_date"))
    if e1:
        return jsonify({"ok": False, "error": "start_date " + e1}), 400
    end, e2 = _parse_date(data.get("end_date"))
    if e2:
        return jsonify({"ok": False, "error": "end_date " + e2}), 400
    if end < start:
        return jsonify({"ok": False, "error": "end_date must be on or after start_date"}), 400
    if start < datetime.utcnow().date():
        return jsonify({"ok": False, "error": "cannot request time off for a past date"}), 400
    reason = (data.get("reason") or "").strip() or None

    now = datetime.utcnow()
    db = SessionLocal()
    try:
        # Advance-notice CUTOFF (Sam 2026-06-13): if the manager turned it on,
        # the requested START must be >= today + cutoff_days. Enforced server-side
        # (fail-closed) so it holds even if the client date-min is bypassed; the
        # employee's effective policy is the most restrictive across their stores.
        from app.services import timeoff_policy
        policy = timeoff_policy.effective_for_employee(db, emp_id)
        earliest = timeoff_policy.earliest_allowed_start(policy)  # store-local base
        if earliest is not None and start < earliest:
            return jsonify({"ok": False,
                            "error": "Time off must be requested at least %d days in advance — "
                                     "the earliest date you can request off is %s."
                                     % (policy["cutoff_days"], earliest.isoformat())}), 400

        # reject overlap with an existing own pending/approved request (two ranges
        # overlap iff start <= other.end AND end >= other.start)
        clash = (db.query(TimeOffRequest)
                   .filter(TimeOffRequest.employee_id == emp_id,
                           TimeOffRequest.status.in_(_OPEN_STATUSES),
                           TimeOffRequest.start_date <= end,
                           TimeOffRequest.end_date >= start)
                   .first())
        if clash is not None:
            return jsonify({"ok": False,
                            "error": "overlaps an existing %s request (%s to %s)"
                                     % (clash.status, clash.start_date.isoformat(),
                                        clash.end_date.isoformat())}), 409
        # Approval policy: auto-approve when the manager doesn't require review.
        new_status = "pending" if policy["require_approval"] else "approved"
        r = TimeOffRequest(employee_id=emp_id, start_date=start, end_date=end,
                           reason=reason, status=new_status,
                           created_at=now, updated_at=now)
        db.add(r)
        db.commit()
        return jsonify({"ok": True, "request": _serialize(r)}), 201
    finally:
        db.close()


@employee_auth.route("/employee/time-off/list", methods=["GET"])
def emp_time_off_list():
    """The employee's own time-off requests, newest first."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        rows = (db.query(TimeOffRequest)
                  .filter(TimeOffRequest.employee_id == emp_id)
                  .order_by(TimeOffRequest.created_at.desc(), TimeOffRequest.id.desc())
                  .all())
        return jsonify({"ok": True, "requests": [_serialize(r) for r in rows]}), 200
    finally:
        db.close()


@employee_auth.route("/employee/time-off/<int:req_id>", methods=["DELETE"])
def emp_time_off_cancel(req_id):
    """Cancel one of the employee's own PENDING requests (soft -> 'cancelled').
    Foreign -> 403; an already approved/denied/cancelled request -> 409."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        r = db.query(TimeOffRequest).filter_by(id=req_id).first()
        if r is None:
            return jsonify({"ok": False, "error": "request not found"}), 404
        if r.employee_id != emp_id:
            return jsonify({"ok": False, "error": "not your request"}), 403
        if r.status != "pending":
            return jsonify({"ok": False,
                            "error": "only a pending request can be cancelled (this is %s)"
                                     % r.status}), 409
        r.status = "cancelled"
        r.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "request": _serialize(r)}), 200
    finally:
        db.close()
