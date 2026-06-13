"""Regression: the employee Shifts tab is SERVER-RENDERED, so the page route must
hand the template a real shift list. It previously passed shifts=[] (and there is
no client-side fetch on that page), so every employee saw 'No shifts posted yet'
regardless of what the JSON endpoint returned. These tests pin employee_schedule_rows
-- the selection the page now renders -- including the legacy-Saturday week key,
the per-shift publish gate, and the published-schedule gate.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.models import Employee, Position, Schedule, Shift
from app.web.schedules_v2_employee import (
    _published_week_shift_rows,
    _store_today,
    _week_bounds,
    employee_schedule_rows,
)


def _setup(db, week_start, *, sched_published=True, shift_published=True):
    """One employee (63) with one shift in a schedule keyed to `week_start`."""
    emp = Employee(id=63, full_name="Alexa Rodriguez", active=True)
    pos = Position(id=5, name="Server", store_key=None)
    sched = Schedule(id=19, store_key="copperfield", week_start=week_start,
                     status="published" if sched_published else "draft")
    db.add_all([emp, pos, sched])
    db.flush()
    start = datetime.combine(week_start + timedelta(days=4),
                             datetime.min.time()).replace(hour=16)  # 4:00 PM
    sh = Shift(id=2359, schedule_id=19, employee_id=63, position_id=5,
               start_at=start, end_at=start.replace(hour=22), status="assigned",
               published_at=(datetime(2026, 6, 10, 3, 21) if shift_published else None))
    db.add(sh)
    db.commit()


def test_page_render_shows_published_shift_this_week(db_session):
    db = db_session
    this_week, _ = _week_bounds(_store_today())   # the real current (Sunday) week
    _setup(db, this_week)
    rows = employee_schedule_rows(db, 63)
    assert len(rows) == 1
    r = rows[0]
    assert r["position_name"] == "Server"
    assert r["status_label"] == "Assigned"
    assert r["date_label"]                                  # non-empty human label
    assert ("AM" in r["time_label"]) or ("PM" in r["time_label"])
    assert " - " in r["time_label"]                         # start - end


def test_page_render_finds_legacy_saturday_schedule(db_session):
    db = db_session
    this_week, _ = _week_bounds(_store_today())
    legacy_sat = this_week - timedelta(days=1)              # pre-migration Saturday key
    _setup(db, legacy_sat)
    # The shim must still surface a Saturday-keyed schedule for the current week.
    assert len(employee_schedule_rows(db, 63)) == 1


def test_page_render_hides_unpublished_shift(db_session):
    db = db_session
    this_week, _ = _week_bounds(_store_today())
    _setup(db, this_week, shift_published=False)            # hollow shift (published_at NULL)
    assert employee_schedule_rows(db, 63) == []


def test_page_render_hides_draft_schedule(db_session):
    db = db_session
    this_week, _ = _week_bounds(_store_today())
    _setup(db, this_week, sched_published=False)            # schedule still a draft
    assert employee_schedule_rows(db, 63) == []


def test_published_week_shift_rows_matches_legacy_saturday_deterministic(db_session):
    db = db_session
    _setup(db, date(2026, 6, 6))                            # Saturday key (legacy)
    rows, week_by = _published_week_shift_rows(db, 63, [date(2026, 6, 7)])  # query the Sunday week
    assert list(week_by.values()) == [date(2026, 6, 6)]
    assert [r.id for r in rows] == [2359]


def test_store_today_is_a_date():
    # store-local "today" drives the week window; must be a real date (Houston).
    assert isinstance(_store_today(), date)
