"""Schedules V2 - availability soft-warning hook (Block 8, ckai).

B4 shift-create (sv2_shift_new) already calls warning() so the integration point
was wired in at B4 with no retrofit. This fills the body in B8.

warning() is a SOFT advisory: the caller surfaces the returned string in the
response ({"warning": ...}) and the shift still saves - it NEVER blocks (contrast
B7 conflict(), which 409s). It flags a shift that lands in a one-off
unavailability block OR outside the employee's recurring availability for that
weekday. If the employee has set no availability for that weekday, there's no
opinion -> None (don't nag). Never raises - an advisory must not break the save.
"""
from __future__ import annotations

import logging
from datetime import datetime as _datetime

from app.db import SessionLocal
from app.models import Employee, EmployeeAvailability, EmployeeUnavailabilityBlock

log = logging.getLogger(__name__)

_DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _fmt_minute(m) -> str:
    """minutes-since-midnight -> 'H:MM AM/PM'."""
    h, mi = divmod(int(m), 60)
    ap = "AM" if h < 12 else "PM"
    return "%d:%02d %s" % (h % 12 or 12, mi, ap)


def warning(employee_id, at_dt) -> str | None:
    """Soft advisory string (else None) for a shift at at_dt. See module docstring.
    Never blocks; never raises."""
    if employee_id is None or not isinstance(at_dt, _datetime):
        return None
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=employee_id).first()
        who = emp.full_name if (emp and emp.full_name) else "This employee"

        # (a) a one-off unavailability block covering the shift start
        blk = (db.query(EmployeeUnavailabilityBlock)
                 .filter(EmployeeUnavailabilityBlock.employee_id == employee_id,
                         EmployeeUnavailabilityBlock.start_at <= at_dt,
                         EmployeeUnavailabilityBlock.end_at >= at_dt)
                 .first())
        if blk is not None:
            return "%s marked themselves unavailable %s to %s" % (
                who,
                blk.start_at.strftime("%a %b %d %I:%M %p"),
                blk.end_at.strftime("%a %b %d %I:%M %p"))

        # (b) recurring availability for that weekday (0=Mon..6=Sun = at_dt.weekday())
        dow = at_dt.weekday()
        windows = (db.query(EmployeeAvailability)
                     .filter(EmployeeAvailability.employee_id == employee_id,
                             EmployeeAvailability.day_of_week == dow)
                     .all())
        if not windows:
            return None  # nothing set for that weekday -> no opinion
        mins = at_dt.hour * 60 + at_dt.minute
        if any(w.start_minute <= mins <= w.end_minute for w in windows):
            return None  # inside an available window
        return "%s is not normally available %s at %s" % (who, _DOW[dow], _fmt_minute(mins))
    except Exception as e:  # advisory must never break shift-create
        log.warning("[availability] warning() failed (ignored): %s", e)
        return None
    finally:
        db.close()
