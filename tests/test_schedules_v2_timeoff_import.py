from __future__ import annotations

from datetime import date

from werkzeug.security import generate_password_hash

from app.models import (
    CenaToastLink,
    Employee,
    EmployeePosition,
    EmployeeStoreAssignment,
    Position,
    TimeOffRequest,
    User,
)


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
    from app.web import schedules_v2_timeoff as timeoff_mod
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
    db_session.add(Position(id=10, name="Server", store_key=None))
    db_session.add(Employee(id=20, full_name="Jordyn Lalena Brooks", active=True, session_version=1))
    db_session.add(EmployeeStoreAssignment(employee_id=20, store_key="tomball"))
    db_session.add(EmployeePosition(employee_id=20, position_id=10, store_key="tomball"))
    db_session.add(CenaToastLink(
        cena_employee_id=20,
        store_key="tomball",
        toast_id="toast-20",
        toast_name="Jordyn Lalena Brooks",
        confirmed_by=1,
    ))
    db_session.commit()

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    sess = lambda: db_session
    for mod in (appdb, perm_mod, sv2_mod, timeoff_mod, store_mod):
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", sess, raising=False)
    return flask_app


def test_sling_timeoff_import_dry_run_and_commit_feed_manager_employee_and_board(db_session, monkeypatch):
    flask_app = _bind_app(db_session, monkeypatch)
    client = _partner_client(flask_app)
    payload = {
        "requests": [{
            "name": "jordyn lalena brooks",
            "start_date": "2026-06-28",
            "end_date": "2026-07-07",
            "status": "pending",
            "reason": "summer camps",
        }]
    }

    dry = client.post("/dos/schedules-v2/time-off/import-sling", json=payload)
    assert dry.status_code == 200, dry.get_data(as_text=True)
    dry_data = dry.get_json()
    assert dry_data["commit"] is False
    assert dry_data["created"] == 0
    assert len(dry_data["created_requests"]) == 1
    assert db_session.query(TimeOffRequest).count() == 0

    committed = client.post("/dos/schedules-v2/time-off/import-sling", json={**payload, "commit": True})
    assert committed.status_code == 200, committed.get_data(as_text=True)
    committed_data = committed.get_json()
    assert committed_data["commit"] is True
    assert committed_data["created"] == 1

    row = db_session.query(TimeOffRequest).one()
    assert row.employee_id == 20
    assert row.start_date == date(2026, 6, 28)
    assert row.end_date == date(2026, 7, 7)
    assert row.reason == "summer camps"
    assert row.status == "pending"

    own_list = client.get("/dos/schedules-v2/time-off/list?status=pending")
    assert own_list.status_code == 200, own_list.get_data(as_text=True)
    listed = own_list.get_json()["requests"]
    assert listed[0]["employee_name"] == "Jordyn Lalena Brooks"
    assert listed[0]["start_date"] == "2026-06-28"

    board = client.get("/dos/schedules-v2/board?week=2026-06-28")
    assert board.status_code == 200, board.get_data(as_text=True)
    board_data = board.get_json()
    assert board_data["time_off_requests"][0]["employee_id"] == 20
    assert board_data["time_off_requests"][0]["start_date"] == "2026-06-28"

    duplicate = client.post("/dos/schedules-v2/time-off/import-sling", json={**payload, "commit": True})
    assert duplicate.status_code == 200, duplicate.get_data(as_text=True)
    assert duplicate.get_json()["created"] == 0
    assert len(duplicate.get_json()["duplicates"]) == 1

