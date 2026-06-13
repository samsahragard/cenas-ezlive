from __future__ import annotations

from datetime import date, datetime

from app.models import (
    CenaToastLink,
    Employee,
    EmployeePosition,
    EmployeeStoreAssignment,
    Position,
    Schedule,
    Shift,
)
from app.services.schedule_draft_import import import_draft_records


def _seed_schedulable_employee(db, *, emp_id=20, name="Alex Martinez", store="tomball"):
    db.add(Position(id=10, name="Cook", store_key=None))
    db.add(Employee(id=emp_id, full_name=name, active=True, session_version=1))
    db.add(EmployeeStoreAssignment(employee_id=emp_id, store_key=store))
    db.add(EmployeePosition(employee_id=emp_id, position_id=10, store_key=store))
    db.add(CenaToastLink(
        cena_employee_id=emp_id,
        store_key=store,
        toast_id=f"toast-{emp_id}",
        toast_name=name,
        confirmed_by=1,
    ))
    db.commit()


def test_draft_import_creates_unpublished_schedule_and_name_only_rows(db_session):
    _seed_schedulable_employee(db_session)
    records = [
        {
            "employee_name": "Alex Martinez",
            "store_key": "tomball",
            "shift_date": "2026-06-14",
            "start": "9:00 AM",
            "end": "5:00 PM",
            "position_name": "Cook",
        },
        {
            "employee_name": "Unmatched Person",
            "store_key": "FM 529 - Copperfield",
            "shift_date": "2026-06-14",
            "start": "10:00 AM",
            "end": "4:00 PM",
            "position_name": "Server Trainee",
            "notes": "training shadow",
        },
    ]

    summary = import_draft_records(
        records,
        db_session,
        week_start=date(2026, 6, 14),
        actor_id=1,
        commit=True,
    )

    assert summary["ok"] is True
    assert summary["committed"] is True
    assert summary["shifts"] == 2
    assert summary["matched"] == 1
    assert summary["name_only"] == 1
    assert summary["mapped_roles"] == {"Server Trainee -> Training": 1}

    schedules = db_session.query(Schedule).order_by(Schedule.store_key).all()
    assert [(s.store_key, s.week_start, s.status, s.published_at) for s in schedules] == [
        ("copperfield", date(2026, 6, 14), "draft", None),
        ("tomball", date(2026, 6, 14), "draft", None),
    ]
    shifts = db_session.query(Shift).order_by(Shift.start_at).all()
    assert [shift.published_at for shift in shifts] == [None, None]
    assert shifts[0].employee_id == 20
    assert shifts[0].display_name is None
    assert shifts[1].employee_id is None
    assert shifts[1].display_name == "Unmatched Person"
    assert "Sling role: Server Trainee" in shifts[1].notes


def test_draft_import_refuses_week_with_existing_shifts(db_session):
    _seed_schedulable_employee(db_session)
    sched = Schedule(
        id=100,
        store_key="tomball",
        week_start=date(2026, 6, 14),
        status="draft",
        published_at=None,
        created_by=1,
    )
    db_session.add(sched)
    db_session.add(Shift(
        schedule_id=100,
        employee_id=20,
        position_id=10,
        start_at=datetime(2026, 6, 14, 9, 0),
        end_at=datetime(2026, 6, 14, 17, 0),
        break_minutes=0,
        status="assigned",
        published_at=None,
    ))
    db_session.commit()

    summary = import_draft_records(
        [{
            "employee_name": "Alex Martinez",
            "store_key": "tomball",
            "shift_date": "2026-06-15",
            "start": "9:00 AM",
            "end": "5:00 PM",
            "position_name": "Cook",
        }],
        db_session,
        week_start=date(2026, 6, 14),
        actor_id=1,
        commit=True,
    )

    assert summary["ok"] is False
    assert "already has schedule data" in summary["error"]
    assert db_session.query(Shift).count() == 1
