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
    """RETIRED (D2): availability is now manager-controlled; employees can no
    longer self-add recurring windows. Route stays registered, returns 410 Gone."""
    return jsonify({"ok": False, "error": "Availability is now manager-controlled."}), 410


@employee_auth.route("/employee/availability/recurring/<int:rec_id>", methods=["DELETE"])
def emp_avail_recurring_del(rec_id):
    """RETIRED (D2): availability is now manager-controlled; employees can no
    longer self-delete recurring windows. Route stays registered, returns 410 Gone."""
    return jsonify({"ok": False, "error": "Availability is now manager-controlled."}), 410


@employee_auth.route("/employee/availability/block", methods=["POST"])
def emp_avail_block_add():
    """RETIRED (D2): availability is now manager-controlled; employees can no
    longer self-add unavailability blocks. Route stays registered, returns 410 Gone."""
    return jsonify({"ok": False, "error": "Availability is now manager-controlled."}), 410


@employee_auth.route("/employee/availability/block/<int:block_id>", methods=["DELETE"])
def emp_avail_block_del(block_id):
    """RETIRED (D2): availability is now manager-controlled; employees can no
    longer self-delete unavailability blocks. Route stays registered, returns 410 Gone."""
    return jsonify({"ok": False, "error": "Availability is now manager-controlled."}), 410


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
