from __future__ import annotations

import os
from datetime import date, datetime

from werkzeug.security import generate_password_hash

from app.models import (
    CenaToastLink,
    Employee,
    EmployeePosition,
    EmployeeStoreAssignment,
    Position,
    Schedule,
    Shift,
    ShiftTag,
    User,
)

os.environ.setdefault("ALLOW_DEV_SECRET", "1")


def _partner_client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["partner_auth_ok"] = True
        s["auth_ok"] = True
        s["user_id"] = 1
        s["user_session_version"] = 1
    return c


def _bind_app(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb
    from app.web import permissions as perm_mod
    from app.web import schedules_v2 as sv2_mod
    from app.web import store_routes as store_mod
    from app.services import scheduling_availability as avail_mod
    from app.services import scheduling_timeoff as timeoff_mod

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
    for pid, name in ((10, "Cook"), (11, "Cashier")):
        db_session.add(Position(id=pid, name=name, store_key=None))
    db_session.add(Employee(id=20, full_name="Alex Martinez", active=True, session_version=1))
    db_session.add(EmployeeStoreAssignment(employee_id=20, store_key="tomball"))
    db_session.add(EmployeePosition(employee_id=20, position_id=10, store_key="tomball"))
    db_session.add(CenaToastLink(
        cena_employee_id=20,
        store_key="tomball",
        toast_id="toast-20",
        toast_name="Alex Martinez",
        confirmed_by=1,
    ))
    db_session.add(Schedule(
        id=100,
        store_key="tomball",
        week_start=date(2026, 6, 7),
        status="draft",
        created_by=1,
    ))
    db_session.commit()

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    sess = lambda: db_session
    for mod in (appdb, perm_mod, sv2_mod, store_mod, avail_mod, timeoff_mod):
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", sess, raising=False)
    return flask_app


def test_schedule_tags_can_be_created_and_return_on_board(db_session, monkeypatch):
    flask_app = _bind_app(db_session, monkeypatch)
    client = _partner_client(flask_app)

    created = client.post("/dos/schedules-v2/tags", json={"name": "AM"})
    assert created.status_code == 201, created.get_data(as_text=True)
    tag = created.get_json()["tag"]
    assert tag["name"] == "AM"

    duplicate = client.post("/dos/schedules-v2/tags", json={"name": "  am  "})
    assert duplicate.status_code == 200, duplicate.get_data(as_text=True)
    assert duplicate.get_json()["tag"]["id"] == tag["id"]

    board = client.get("/dos/schedules-v2/board?week=2026-06-07")
    assert board.status_code == 200, board.get_data(as_text=True)
    assert [row["name"] for row in board.get_json()["tags"]] == ["AM"]


def test_shift_create_and_update_persist_tag_ids(db_session, monkeypatch):
    flask_app = _bind_app(db_session, monkeypatch)
    client = _partner_client(flask_app)

    tag_a = client.post("/dos/schedules-v2/tags", json={"name": "AM"}).get_json()["tag"]
    tag_b = client.post("/dos/schedules-v2/tags", json={"name": "Training"}).get_json()["tag"]

    created = client.post("/dos/schedules-v2/shifts/new", json={
        "schedule_id": 100,
        "employee_id": 20,
        "position_id": 10,
        "start_at": "2026-06-07T09:00:00",
        "end_at": "2026-06-07T17:00:00",
        "break_minutes": 0,
        "tag_ids": [tag_a["id"]],
    })
    assert created.status_code == 201, created.get_data(as_text=True)
    shift_id = created.get_json()["id"]
    db_session.expire_all()
    assert [row.tag_id for row in db_session.query(ShiftTag).filter_by(shift_id=shift_id).all()] == [tag_a["id"]]

    updated = client.put(f"/dos/schedules-v2/shifts/{shift_id}", json={
        "tag_ids": [tag_b["id"]],
        "start_at": "2026-06-07T10:00:00",
        "end_at": "2026-06-07T18:00:00",
    })
    assert updated.status_code == 200, updated.get_data(as_text=True)
    db_session.expire_all()
    assert [row.tag_id for row in db_session.query(ShiftTag).filter_by(shift_id=shift_id).all()] == [tag_b["id"]]

    board = client.get("/dos/schedules-v2/board?week=2026-06-07").get_json()
    shift = next(row for row in board["shifts"] if row["id"] == shift_id)
    assert shift["tag_ids"] == [tag_b["id"]]


def test_bulk_week_copy_preserves_shift_tags(db_session, monkeypatch):
    flask_app = _bind_app(db_session, monkeypatch)
    client = _partner_client(flask_app)

    tag = client.post("/dos/schedules-v2/tags", json={"name": "Catering"}).get_json()["tag"]
    created = client.post("/dos/schedules-v2/shifts/new", json={
        "schedule_id": 100,
        "employee_id": 20,
        "position_id": 10,
        "start_at": "2026-06-07T09:00:00",
        "end_at": "2026-06-07T17:00:00",
        "tag_ids": [tag["id"]],
    })
    assert created.status_code == 201, created.get_data(as_text=True)
    db_session.add(Schedule(
        id=101,
        store_key="tomball",
        week_start=date(2026, 6, 14),
        status="draft",
        created_by=1,
    ))
    db_session.commit()

    copied = client.post("/dos/schedules-v2/shifts/bulk-copy", json={
        "from_schedule_id": 100,
        "to_schedule_id": 101,
    })
    assert copied.status_code == 201, copied.get_data(as_text=True)

    db_session.expire_all()
    new_shift = db_session.query(Shift).filter_by(schedule_id=101).one()
    assert [row.tag_id for row in db_session.query(ShiftTag).filter_by(shift_id=new_shift.id).all()] == [tag["id"]]


def test_selected_shift_copy_to_week_preserves_tags_and_day_offsets(db_session, monkeypatch):
    flask_app = _bind_app(db_session, monkeypatch)
    client = _partner_client(flask_app)

    tag = client.post("/dos/schedules-v2/tags", json={"name": "ENCH/TOGO 1"}).get_json()["tag"]
    created = client.post("/dos/schedules-v2/shifts/new", json={
        "schedule_id": 100,
        "employee_id": 20,
        "position_id": 10,
        "start_at": "2026-06-10T15:00:00",
        "end_at": "2026-06-10T22:00:00",
        "tag_ids": [tag["id"]],
    })
    assert created.status_code == 201, created.get_data(as_text=True)
    shift_id = created.get_json()["id"]

    copied = client.post("/dos/schedules-v2/shifts/copy-to-week", json={
        "shift_ids": [shift_id],
        "source_week_start": "2026-06-07",
        "target_week_start": "2026-06-14",
    })
    assert copied.status_code == 201, copied.get_data(as_text=True)
    body = copied.get_json()
    assert body["copied"] == 1
    assert body["created_schedule"] is True

    db_session.expire_all()
    target_schedule = db_session.get(Schedule, body["target_schedule_id"])
    assert target_schedule is not None
    assert target_schedule.store_key == "tomball"
    assert target_schedule.week_start == date(2026, 6, 14)
    new_shift = db_session.query(Shift).filter(
        Shift.schedule_id == target_schedule.id,
        Shift.start_at == datetime(2026, 6, 17, 15, 0),
    ).one()
    assert new_shift.end_at == datetime(2026, 6, 17, 22, 0)
    assert new_shift.published_at is None
    assert [row.tag_id for row in db_session.query(ShiftTag).filter_by(shift_id=new_shift.id).all()] == [tag["id"]]
