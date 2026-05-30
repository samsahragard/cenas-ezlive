"""Schedules V2 - time-off conflict hook (Block 7).

The B4 shift-create endpoint (schedules_v2.py sv2_shift_new) already calls
conflict() so the B7 integration point was wired in at B4 with no retrofit -
ckai fills the real body here in B7 + owns the time_off_requests table.

Only an APPROVED time-off request blocks a shift; pending/denied/cancelled never
do. The return is a human-readable string the caller drops straight into a 409
({"ok": false, "error": <this string>}), so it must read well to a manager.
"""
from __future__ import annotations

from datetime import date as _date, datetime as _datetime

from app.db import SessionLocal
from app.models import Employee, TimeOffRequest


def conflict(employee_id, on_date) -> str | None:
    """Blocker string (caller -> HTTP 409) if `employee_id` has an APPROVED
    time-off request covering `on_date`, else None.

    `on_date` is normally a date (the caller passes start_at.date()); a datetime
    is narrowed to its date defensively. NB datetime subclasses date, so the
    narrowing tests datetime explicitly."""
    if employee_id is None or on_date is None:
        return None
    if isinstance(on_date, _datetime):
        on_date = on_date.date()
    elif not isinstance(on_date, _date):
        return None  # unparseable input never blocks (fail-open: don't wedge shift-create)
    db = SessionLocal()
    try:
        row = (db.query(TimeOffRequest)
                 .filter(TimeOffRequest.employee_id == employee_id,
                         TimeOffRequest.status == "approved",
                         TimeOffRequest.start_date <= on_date,
                         TimeOffRequest.end_date >= on_date)
                 .first())
        if row is None:
            return None
        emp = db.query(Employee).filter_by(id=employee_id).first()
        who = emp.full_name if (emp and emp.full_name) else "This employee"
        return ("%s has approved time off %s to %s"
                % (who, row.start_date.isoformat(), row.end_date.isoformat()))
    finally:
        db.close()
