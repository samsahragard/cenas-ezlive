from __future__ import annotations

import os
from datetime import date, datetime

from werkzeug.security import generate_password_hash

from app.models import (
    CenaToastLink,
    Employee,
    EmployeePosition,
    EmployeeStoreAssignment,
    EmployeeUnavailabilityBlock,
    Position,
    Schedule,
    Shift,
    TimeOffRequest,
    User,
)
from app.services.schedule_import import _week_start
from app.web.schedules_v2_employee import _week_bounds

os.environ.setdefault("ALLOW_DEV_SECRET", "1")


def test_schedule_week_starts_on_sunday():
    assert _week_start(date(2026, 6, 7)) == date(2026, 6, 7)
    assert _week_start(date(2026, 6, 9)) == date(2026, 6, 7)
    assert _week_start(date(2026, 6, 13)) == date(2026, 6, 7)

    this_week, next_week = _week_bounds(date(2026, 6, 9))
    assert this_week == date(2026, 6, 7)
    assert next_week == date(2026, 6, 14)


def _partner_client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["partner_auth_ok"] = True
        s["auth_ok"] = True
        s["user_id"] = 1
        s["user_session_version"] = 1
    return c


def _seed_position(db, pid, name):
    db.add(Position(id=pid, name=name, store_key=None))


def _seed_employee(db, eid, name, active=True, position_id=10, linked=True):
    db.add(Employee(
        id=eid,
        full_name=name,
        active=active,
        session_version=1,
    ))
    db.add(EmployeeStoreAssignment(employee_id=eid, store_key="tomball"))
    if position_id is not None:
        db.add(EmployeePosition(employee_id=eid, position_id=position_id, store_key="tomball"))
    if linked:
        db.add(CenaToastLink(
            cena_employee_id=eid,
            store_key="tomball",
            toast_id=f"toast-{eid}",
            toast_name=name,
            confirmed_by=1,
        ))


def _seed_shift(db, sched_id, eid, hour):
    db.add(Shift(
        schedule_id=sched_id,
        employee_id=eid,
        position_id=10,
        start_at=datetime(2026, 6, 7, hour, 0),
        end_at=datetime(2026, 6, 7, hour + 4, 0),
        break_minutes=0,
        status="assigned",
        published_at=datetime(2026, 6, 1, 12, 0),
    ))


def test_week_board_roster_uses_active_linked_positioned_team_source(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb
    from app.web import permissions as perm_mod
    from app.web import schedules_v2 as sv2_mod
    from app.web import store_routes as store_mod

    partner = User(
        id=1,
        full_name="Partner",
        email="partner@test.local",
        passcode_hash=generate_password_hash("12345"),
        permission_level="partner",
        store_scope=None,
        active=True,
        first_login_done=True,
        session_version=1,
    )
    db_session.add(partner)
    _seed_position(db_session, 10, "Cook")

    _seed_employee(db_session, 20, "Linked Active", active=True, position_id=10, linked=True)
    _seed_employee(db_session, 21, "Inactive Employee", active=False, position_id=10, linked=True)
    _seed_employee(db_session, 22, "Unlinked Employee", active=True, position_id=10, linked=False)
    _seed_employee(db_session, 23, "No Position", active=True, position_id=None, linked=True)

    sched = Schedule(
        id=100,
        store_key="tomball",
        week_start=date(2026, 6, 7),
        status="published",
        published_at=datetime(2026, 6, 1, 12, 0),
        created_by=1,
    )
    db_session.add(sched)
    for offset, eid in enumerate((20, 21, 22, 23)):
        _seed_shift(db_session, sched.id, eid, 8 + offset)
    db_session.commit()

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    sess = lambda: db_session
    for mod in (appdb, perm_mod, sv2_mod, store_mod):
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", sess, raising=False)

    response = _partner_client(flask_app).get("/dos/schedules-v2/board?week=2026-06-07")
    assert response.status_code == 200, response.get_data(as_text=True)[:500]
    data = response.get_json()

    assert [r["full_name"] for r in data["roster"]] == ["Linked Active"]
    assert {sh["employee_id"] for sh in data["shifts"]} == {20}


def test_week_board_reads_legacy_saturday_schedule_key(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb
    from app.web import permissions as perm_mod
    from app.web import schedules_v2 as sv2_mod
    from app.web import store_routes as store_mod

    db_session.add(User(
        id=1,
        full_name="Partner",
        email="partner@test.local",
        passcode_hash=generate_password_hash("12345"),
        permission_level="partner",
        store_scope=None,
        active=True,
        first_login_done=True,
        session_version=1,
    ))
    _seed_position(db_session, 10, "Cook")
    _seed_employee(db_session, 20, "Linked Active", active=True, position_id=10, linked=True)
    sched = Schedule(
        id=100,
        store_key="tomball",
        week_start=date(2026, 6, 6),
        status="published",
        published_at=datetime(2026, 6, 1, 12, 0),
        created_by=1,
    )
    db_session.add(sched)
    _seed_shift(db_session, sched.id, 20, 8)
    db_session.commit()

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    sess = lambda: db_session
    for mod in (appdb, perm_mod, sv2_mod, store_mod):
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", sess, raising=False)

    response = _partner_client(flask_app).get("/dos/schedules-v2/board?week=2026-06-07")
    assert response.status_code == 200, response.get_data(as_text=True)[:500]
    data = response.get_json()

    assert data["schedule"]["week_start"] == "2026-06-06"
    assert [r["full_name"] for r in data["roster"]] == ["Linked Active"]
    assert {sh["employee_id"] for sh in data["shifts"]} == {20}


def test_week_board_includes_time_off_and_unavailability_markers(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb
    from app.web import permissions as perm_mod
    from app.web import schedules_v2 as sv2_mod
    from app.web import store_routes as store_mod

    db_session.add(User(
        id=1,
        full_name="Partner",
        email="partner@test.local",
        passcode_hash=generate_password_hash("12345"),
        permission_level="partner",
        store_scope=None,
        active=True,
        first_login_done=True,
        session_version=1,
    ))
    _seed_position(db_session, 10, "Cook")
    _seed_employee(db_session, 20, "Linked Active", active=True, position_id=10, linked=True)
    db_session.add(Schedule(
        id=100,
        store_key="tomball",
        week_start=date(2026, 6, 7),
        status="draft",
        created_by=1,
    ))
    db_session.add(TimeOffRequest(
        employee_id=20,
        start_date=date(2026, 6, 9),
        end_date=date(2026, 6, 10),
        status="pending",
        reason="family",
    ))
    db_session.add(TimeOffRequest(
        employee_id=20,
        start_date=date(2026, 6, 12),
        end_date=date(2026, 6, 12),
        status="approved",
        reason="appointment",
    ))
    db_session.add(TimeOffRequest(
        employee_id=20,
        start_date=date(2026, 6, 11),
        end_date=date(2026, 6, 11),
        status="denied",
        reason="hidden",
    ))
    db_session.add(EmployeeUnavailabilityBlock(
        employee_id=20,
        start_at=datetime(2026, 6, 11, 10, 0),
        end_at=datetime(2026, 6, 11, 16, 0),
        reason="school",
    ))
    db_session.commit()

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    sess = lambda: db_session
    for mod in (appdb, perm_mod, sv2_mod, store_mod):
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", sess, raising=False)

    response = _partner_client(flask_app).get("/dos/schedules-v2/board?week=2026-06-07")
    assert response.status_code == 200, response.get_data(as_text=True)[:500]
    data = response.get_json()

    assert [r["status"] for r in data["time_off_requests"]] == ["pending", "approved"]
    assert data["time_off_requests"][0]["start_date"] == "2026-06-09"
    assert data["time_off_requests"][0]["end_date"] == "2026-06-10"
    assert data["time_off_requests"][1]["start_date"] == "2026-06-12"
    assert len(data["unavailability_blocks"]) == 1
    assert data["unavailability_blocks"][0]["start_at"] == "2026-06-11T10:00:00"
    assert data["unavailability_blocks"][0]["end_at"] == "2026-06-11T16:00:00"
