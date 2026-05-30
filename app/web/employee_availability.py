"""Schedules V2 - Block 8: the employee availability endpoints (ckai).

  POST   /employee/availability/recurring   {day_of_week 0-6, start_time HH:MM, end_time HH:MM}
  DELETE /employee/availability/recurring/<id>
  POST   /employee/availability/block        {start_at, end_at, reason?}   (date-specific unavailable span)
  DELETE /employee/availability/block/<id>
  GET    /employee/availability/list         -> {recurring:[...], blocks:[...]} (JSON, own)

URL-split (the B5/B6/B7 pattern, applied up front): the JSON list lives at the
/list child so ck's HTML PAGE can own the bare GET /employee/availability without
a route collision. POST/DELETE sub-paths don't collide with the page.

Attaches to the employee_auth blueprint (imported before ezempauth.install);
employee-session-scoped (_require_emp -> 401); /employee/availability is in
auth.py EXEMPT_PREFIXES so a session-less hit gets a clean 401 JSON. Availability
drives only a SOFT warning at shift-create (scheduling_availability.warning), it
never blocks - so there is no manager approval here (contrast B7 time-off).
"""
from __future__ import annotations

from datetime import datetime

from flask import jsonify, request, session

from app.db import SessionLocal
from app.models import EmployeeAvailability, EmployeeUnavailabilityBlock
from app.web.employee_auth import employee_auth


def _require_emp():
    eid = session.get("employee_id")
    if not eid:
        return None, (jsonify({"ok": False, "error": "login required"}), 401)
    return eid, None


def _fmt_hhmm(m) -> str:
    h, mi = divmod(int(m), 60)
    return "%02d:%02d" % (h, mi)


def _parse_hhmm(v):
    """(minutes, None) or (None, errmsg)."""
    if v is None or not str(v).strip():
        return None, "is required"
    parts = str(v).strip().split(":")
    if len(parts) != 2:
        return None, "must be HH:MM"
    try:
        h, mi = int(parts[0]), int(parts[1])
    except ValueError:
        return None, "must be HH:MM"
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None, "must be a valid 24-hour HH:MM"
    return h * 60 + mi, None


def _parse_dt(v):
    if not v or not str(v).strip():
        return None, "is required"
    try:
        return datetime.fromisoformat(str(v).strip()), None
    except ValueError:
        return None, "must be ISO datetime (YYYY-MM-DDTHH:MM)"


def _ser_recurring(r):
    return {"id": r.id, "day_of_week": r.day_of_week,
            "start_time": _fmt_hhmm(r.start_minute), "end_time": _fmt_hhmm(r.end_minute)}


def _ser_block(b):
    return {"id": b.id,
            "start_at": b.start_at.isoformat() if b.start_at else None,
            "end_at": b.end_at.isoformat() if b.end_at else None,
            "reason": b.reason}


@employee_auth.route("/employee/availability/recurring", methods=["POST"])
def emp_avail_recurring_add():
    """Add a recurring weekly available window."""
    emp_id, err = _require_emp()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        dow = int(data.get("day_of_week"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "day_of_week must be an integer 0-6 (0=Mon)"}), 400
    if not (0 <= dow <= 6):
        return jsonify({"ok": False, "error": "day_of_week must be 0-6 (0=Mon..6=Sun)"}), 400
    sm, e1 = _parse_hhmm(data.get("start_time"))
    if e1:
        return jsonify({"ok": False, "error": "start_time " + e1}), 400
    em, e2 = _parse_hhmm(data.get("end_time"))
    if e2:
        return jsonify({"ok": False, "error": "end_time " + e2}), 400
    if em <= sm:
        return jsonify({"ok": False, "error": "end_time must be after start_time"}), 400
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        dup = (db.query(EmployeeAvailability)
                 .filter_by(employee_id=emp_id, day_of_week=dow,
                            start_minute=sm, end_minute=em).first())
        if dup is not None:
            return jsonify({"ok": False, "error": "that exact window already exists"}), 409
        r = EmployeeAvailability(employee_id=emp_id, day_of_week=dow,
                                 start_minute=sm, end_minute=em, created_at=now)
        db.add(r)
        db.commit()
        return jsonify({"ok": True, "recurring": _ser_recurring(r)}), 201
    finally:
        db.close()


@employee_auth.route("/employee/availability/recurring/<int:rec_id>", methods=["DELETE"])
def emp_avail_recurring_del(rec_id):
    """Delete one of the employee's own recurring windows."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        r = db.query(EmployeeAvailability).filter_by(id=rec_id).first()
        if r is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        if r.employee_id != emp_id:
            return jsonify({"ok": False, "error": "not your availability"}), 403
        db.delete(r)
        db.commit()
        return jsonify({"ok": True, "deleted": rec_id}), 200
    finally:
        db.close()


@employee_auth.route("/employee/availability/block", methods=["POST"])
def emp_avail_block_add():
    """Add a date-specific unavailability block."""
    emp_id, err = _require_emp()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    start, e1 = _parse_dt(data.get("start_at"))
    if e1:
        return jsonify({"ok": False, "error": "start_at " + e1}), 400
    end, e2 = _parse_dt(data.get("end_at"))
    if e2:
        return jsonify({"ok": False, "error": "end_at " + e2}), 400
    if end <= start:
        return jsonify({"ok": False, "error": "end_at must be after start_at"}), 400
    reason = (data.get("reason") or "").strip() or None
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        b = EmployeeUnavailabilityBlock(employee_id=emp_id, start_at=start, end_at=end,
                                        reason=reason, created_at=now)
        db.add(b)
        db.commit()
        return jsonify({"ok": True, "block": _ser_block(b)}), 201
    finally:
        db.close()


@employee_auth.route("/employee/availability/block/<int:block_id>", methods=["DELETE"])
def emp_avail_block_del(block_id):
    """Delete one of the employee's own unavailability blocks."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        b = db.query(EmployeeUnavailabilityBlock).filter_by(id=block_id).first()
        if b is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        if b.employee_id != emp_id:
            return jsonify({"ok": False, "error": "not your block"}), 403
        db.delete(b)
        db.commit()
        return jsonify({"ok": True, "deleted": block_id}), 200
    finally:
        db.close()


@employee_auth.route("/employee/availability/list", methods=["GET"])
def emp_avail_list():
    """The employee's own recurring windows + unavailability blocks (JSON)."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        recs = (db.query(EmployeeAvailability).filter_by(employee_id=emp_id)
                  .order_by(EmployeeAvailability.day_of_week,
                            EmployeeAvailability.start_minute).all())
        blks = (db.query(EmployeeUnavailabilityBlock).filter_by(employee_id=emp_id)
                  .order_by(EmployeeUnavailabilityBlock.start_at).all())
        return jsonify({"ok": True,
                        "recurring": [_ser_recurring(r) for r in recs],
                        "blocks": [_ser_block(b) for b in blks]}), 200
    finally:
        db.close()
