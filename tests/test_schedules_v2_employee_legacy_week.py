"""Regression: the employee my-schedule endpoint must surface shifts that live
in a PUBLISHED schedule still keyed to the legacy SATURDAY week_start.

Schedules were re-keyed Saturday -> Sunday (manager FE WEEK_START_DOW 6 -> 0).
The manager board kept old rows visible via schedules_v2._schedule_for_week()'s
compat shim, but the employee endpoint matched week_start by exact Sunday
equality with no shim -- so a current week still keyed to Saturday (e.g.
2026-06-06) matched zero schedules and the employee saw "No shifts posted yet"
even though the manager had published the week and assigned the shifts.

These tests pin the shim helpers and prove a Saturday-keyed published schedule
is matched by the same week set the view feeds into `week_start IN (...)`.
"""
from __future__ import annotations

from datetime import date, datetime

from app.models import Employee, Schedule, Shift
from app.web.schedules_v2_employee import (
    _legacy_saturday_week_start,
    _week_bounds,
    _weeks_with_legacy,
)


def test_legacy_saturday_week_start_maps_sunday_to_prior_saturday():
    # A Sunday week_start -> its pre-migration Saturday key (the day before).
    assert _legacy_saturday_week_start(date(2026, 6, 7)) == date(2026, 6, 6)
    assert _legacy_saturday_week_start(date(2026, 6, 14)) == date(2026, 6, 13)
    # Non-Sunday inputs are never expanded (only Sunday weeks have a legacy twin).
    assert _legacy_saturday_week_start(date(2026, 6, 6)) is None   # Saturday
    assert _legacy_saturday_week_start(date(2026, 6, 8)) is None   # Monday


def test_weeks_with_legacy_keeps_canonical_and_adds_saturdays():
    weeks = [date(2026, 6, 7), date(2026, 6, 14)]  # this/next Sunday
    expanded = _weeks_with_legacy(weeks)
    # canonical Sundays preserved...
    assert date(2026, 6, 7) in expanded
    assert date(2026, 6, 14) in expanded
    # ...and their legacy Saturdays added.
    assert date(2026, 6, 6) in expanded
    assert date(2026, 6, 13) in expanded


def test_published_saturday_schedule_is_matched_by_expanded_weeks(db_session):
    """The exact live failure: today Sat 2026-06-13, employee has a published
    shift in copperfield schedule keyed to SATURDAY 2026-06-06. The view's
    Sunday week set {06-07, 06-14} misses it; the expanded set must catch it."""
    db = db_session
    emp = Employee(id=63, full_name="Alexa Rodriguez", active=True)
    sched = Schedule(id=19, store_key="copperfield",
                     week_start=date(2026, 6, 6), status="published")  # legacy Saturday key
    db.add_all([emp, sched])
    db.flush()
    shift = Shift(id=2359, schedule_id=sched.id, employee_id=emp.id,
                  start_at=datetime(2026, 6, 13, 16), end_at=datetime(2026, 6, 13, 22),
                  status="assigned", published_at=datetime(2026, 6, 10, 3, 21))
    db.add(shift)
    db.commit()

    this_week, next_week = _week_bounds(date(2026, 6, 13))
    weeks = [this_week, next_week]
    assert this_week == date(2026, 6, 7)  # Sunday-anchored, as the view computes

    # BEFORE the fix: exact Sunday match finds nothing.
    miss = (db.query(Schedule)
              .filter(Schedule.week_start.in_(weeks),
                      Schedule.status == "published").all())
    assert miss == []

    # AFTER the fix: the expanded set matches the Saturday-keyed schedule...
    scheds = (db.query(Schedule)
                .filter(Schedule.week_start.in_(_weeks_with_legacy(weeks)),
                        Schedule.status == "published").all())
    assert [s.id for s in scheds] == [19]

    # ...and the employee's published shift is then reachable.
    rows = (db.query(Shift)
              .filter(Shift.employee_id == emp.id,
                      Shift.schedule_id.in_([s.id for s in scheds]),
                      Shift.published_at.isnot(None)).all())
    assert [r.id for r in rows] == [2359]
