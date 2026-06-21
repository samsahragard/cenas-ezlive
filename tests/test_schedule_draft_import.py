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
    ShiftTag,
    Tag,
)
from app.services.schedule_draft_import import import_draft_records


def _seed_schedulable_employee(db, *, emp_id=20, name="Alex Martinez", store="tomball"):
    if db.get(Position, 10) is None:
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
            "tags": ["ENCH/TOGO 1", " WINDOW ", "ENCH/TOGO 1"],
        },
        {
            "employee_name": "Unmatched Person",
            "store_key": "FM 529 - Copperfield",
            "shift_date": "2026-06-14",
            "start": "10:00 AM",
            "end": "4:00 PM",
            "position_name": "Floor Manager",
            "notes": "training shadow",
            "tags": ["Floor-Close"],
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
    assert summary["mapped_roles"] == {"Floor Manager -> FOH Manager": 1}
    assert summary["per_tag"] == {"ENCH/TOGO 1": 1, "WINDOW": 1, "Floor-Close": 1}

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
    assert "Sling role: Floor Manager" in shifts[1].notes
    tags = {tag.name: tag.id for tag in db_session.query(Tag).all()}
    assert {"ENCH/TOGO 1", "WINDOW", "Floor-Close"}.issubset(tags)
    by_shift = {
        shift_id: {tag_id for (tag_id,) in db_session.query(ShiftTag.tag_id).filter_by(shift_id=shift_id)}
        for shift_id in [shifts[0].id, shifts[1].id]
    }
    assert by_shift[shifts[0].id] == {tags["ENCH/TOGO 1"], tags["WINDOW"]}
    assert by_shift[shifts[1].id] == {tags["Floor-Close"]}


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


def test_draft_import_replace_clears_legacy_week_and_imports_only_payload(db_session):
    _seed_schedulable_employee(db_session)
    legacy = Schedule(
        id=100,
        store_key="tomball",
        week_start=date(2026, 6, 13),
        status="published",
        published_at=datetime(2026, 6, 6, 12, 0),
        created_by=1,
    )
    db_session.add(legacy)
    db_session.add(Shift(
        id=200,
        schedule_id=100,
        employee_id=20,
        position_id=10,
        start_at=datetime(2026, 6, 13, 16, 0),
        end_at=datetime(2026, 6, 13, 22, 0),
        break_minutes=0,
        status="assigned",
        published_at=datetime(2026, 6, 6, 12, 0),
    ))
    db_session.commit()

    summary = import_draft_records(
        [{
            "employee_name": "Alex Martinez",
            "store_key": "tomball",
            "shift_date": "2026-06-14",
            "start": "9:00 AM",
            "end": "5:00 PM",
            "position_name": "Cook",
        }],
        db_session,
        week_start=date(2026, 6, 14),
        actor_id=1,
        commit=True,
        replace_existing=True,
        target_store="tomball",
    )

    assert summary["ok"] is True
    assert summary["replace_existing"] is True
    assert summary["cleared"] == {"schedules": 1, "shifts": 1}
    assert db_session.query(Schedule).filter_by(week_start=date(2026, 6, 13)).count() == 0
    schedules = db_session.query(Schedule).all()
    assert len(schedules) == 1
    assert schedules[0].store_key == "tomball"
    assert schedules[0].week_start == date(2026, 6, 14)
    assert schedules[0].status == "draft"
    assert schedules[0].published_at is None
    shifts = db_session.query(Shift).all()
    assert len(shifts) == 1
    assert shifts[0].id != 200
    assert shifts[0].published_at is None
    assert shifts[0].start_at == datetime(2026, 6, 14, 9, 0)


def test_draft_import_replace_requires_target_store_match(db_session):
    _seed_schedulable_employee(db_session)
    db_session.add(Schedule(
        id=100,
        store_key="tomball",
        week_start=date(2026, 6, 13),
        status="published",
        published_at=datetime(2026, 6, 6, 12, 0),
        created_by=1,
    ))
    db_session.add(Shift(
        id=200,
        schedule_id=100,
        employee_id=20,
        position_id=10,
        start_at=datetime(2026, 6, 13, 16, 0),
        end_at=datetime(2026, 6, 13, 22, 0),
        break_minutes=0,
        status="assigned",
        published_at=datetime(2026, 6, 6, 12, 0),
    ))
    db_session.commit()

    summary = import_draft_records(
        [{
            "employee_name": "Alex Martinez",
            "store_key": "tomball",
            "shift_date": "2026-06-14",
            "start": "9:00 AM",
            "end": "5:00 PM",
            "position_name": "Cook",
        }],
        db_session,
        week_start=date(2026, 6, 14),
        actor_id=1,
        commit=True,
        replace_existing=True,
    )

    assert summary["ok"] is False
    assert "target_store is required" in summary["error"]
    assert db_session.query(Schedule).filter_by(id=100).count() == 1
    assert db_session.query(Shift).filter_by(id=200).count() == 1

    mismatch = import_draft_records(
        [{
            "employee_name": "Alex Martinez",
            "store_key": "tomball",
            "shift_date": "2026-06-14",
            "start": "9:00 AM",
            "end": "5:00 PM",
            "position_name": "Cook",
        }],
        db_session,
        week_start=date(2026, 6, 14),
        actor_id=1,
        commit=True,
        replace_existing=True,
        target_store="copperfield",
    )

    assert mismatch["ok"] is False
    assert "must match every record" in mismatch["error"]
    assert db_session.query(Schedule).filter_by(id=100).count() == 1
    assert db_session.query(Shift).filter_by(id=200).count() == 1


def test_draft_import_replace_leaves_other_store_untouched(db_session):
    _seed_schedulable_employee(db_session)
    _seed_schedulable_employee(db_session, emp_id=21, name="Copper Person", store="copperfield")
    tomball = Schedule(
        id=100,
        store_key="tomball",
        week_start=date(2026, 6, 13),
        status="published",
        published_at=datetime(2026, 6, 6, 12, 0),
        created_by=1,
    )
    copperfield = Schedule(
        id=101,
        store_key="copperfield",
        week_start=date(2026, 6, 14),
        status="draft",
        published_at=None,
        created_by=1,
    )
    db_session.add_all([tomball, copperfield])
    db_session.add(Shift(
        id=200,
        schedule_id=100,
        employee_id=20,
        position_id=10,
        start_at=datetime(2026, 6, 13, 16, 0),
        end_at=datetime(2026, 6, 13, 22, 0),
        break_minutes=0,
        status="assigned",
        published_at=datetime(2026, 6, 6, 12, 0),
    ))
    db_session.add(Shift(
        id=201,
        schedule_id=101,
        employee_id=21,
        position_id=10,
        start_at=datetime(2026, 6, 14, 16, 0),
        end_at=datetime(2026, 6, 14, 22, 0),
        break_minutes=0,
        status="assigned",
        published_at=None,
    ))
    db_session.commit()

    summary = import_draft_records(
        [{
            "employee_name": "Alex Martinez",
            "store_key": "tomball",
            "shift_date": "2026-06-14",
            "start": "9:00 AM",
            "end": "5:00 PM",
            "position_name": "Cook",
        }],
        db_session,
        week_start=date(2026, 6, 14),
        actor_id=1,
        commit=True,
        replace_existing=True,
        target_store="tomball",
    )

    assert summary["ok"] is True
    assert db_session.query(Schedule).filter_by(id=101).count() == 1
    assert db_session.query(Shift).filter_by(id=201).count() == 1
