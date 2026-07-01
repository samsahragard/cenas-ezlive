from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

from flask import Flask, g


def test_prep_timestamp_labels_render_houston_time():
    from app.web.store_routes import _prep_datetime_label, _prep_time_label

    utc_dt = datetime(2026, 7, 1, 17, 7)

    assert _prep_datetime_label(utc_dt) == "Jul 1, 2026 12:07 PM"
    assert _prep_time_label(utc_dt, utc=True) == "12:07 PM"


def test_prep_tracker_save_is_day_scoped_and_audited(db_session):
    from app.models import PrepAuditLog, PrepEntry, PrepItem
    from app.web.store_routes import _prep_list_v3_post, _prep_load_helpers

    item = PrepItem(name="Masa Flour", category="hot", kind="item", sort_order=1)
    db_session.add(item)
    db_session.commit()

    app = Flask(__name__)
    app.secret_key = "test"
    today = date(2026, 6, 7)
    user = SimpleNamespace(id=7, full_name="Sam Sahragard")

    with app.test_request_context(
        "/partner/kitchen/prep-list",
        method="POST",
        data={
            "form_action": "save_tracker",
            "item_id": str(item.id),
            "view_date": today.isoformat(),
            "on_hand": "4",
            "prep_qty": "12",
            "assignee_name": "Maria Lopez",
            "helper_names": ["Juan Perez", "Ana Diaz"],
            "status": "partly",
            "notes": "Started before lunch.",
        },
    ):
        g.current_location = "both"
        _prep_list_v3_post(db_session, None, user)

    entry = db_session.query(PrepEntry).one()
    assert entry.entry_date == today
    assert entry.selected is True
    assert entry.on_hand == 4
    assert entry.prep_qty == 12
    assert entry.assignee_name == "Maria Lopez"
    assert _prep_load_helpers(entry.helper_names) == ["Juan Perez", "Ana Diaz"]
    assert entry.status == "partly"
    assert entry.completed_by_name is None
    assert db_session.query(PrepEntry).filter(
        PrepEntry.entry_date == today + timedelta(days=1)).count() == 0

    audit = db_session.query(PrepAuditLog).one()
    assert audit.action == "updated"
    assert audit.item_name == "Masa Flour"
    assert audit.actor_name == "Sam Sahragard"


def test_recent_complete_records_completion_actor(db_session):
    from app.models import PrepAuditLog, PrepEntry, PrepItem
    from app.web.store_routes import _prep_list_v3_post

    item = PrepItem(name="Empanadas", category="hot", kind="item", sort_order=2)
    db_session.add(item)
    db_session.commit()

    app = Flask(__name__)
    app.secret_key = "test"
    day = date(2026, 6, 6)
    user = SimpleNamespace(id=9, full_name="Kitchen Lead")
    db_session.add(PrepEntry(
        entry_date=day,
        prep_item_id=item.id,
        selected=True,
        status="partly",
        assignee_name="Maria Lopez",
    ))
    db_session.commit()

    with app.test_request_context(
        "/partner/kitchen/prep-list",
        method="POST",
        data={
            "form_action": "set_status",
            "item_id": str(item.id),
            "view_date": day.isoformat(),
            "status": "completed",
        },
    ):
        g.current_location = "both"
        _prep_list_v3_post(db_session, None, user)

    entry = db_session.query(PrepEntry).one()
    assert entry.status == "completed"
    assert entry.completed_by_name == "Kitchen Lead"
    assert entry.completed_at is not None

    audit = db_session.query(PrepAuditLog).filter_by(action="completed").one()
    assert audit.entry_date == day
    assert audit.item_name == "Empanadas"


def test_prep_team_today_uses_scheduled_prep_position(db_session, monkeypatch):
    from app.models import (
        Employee,
        Position,
        PrepEntry,
        PrepItem,
        Schedule,
        Shift,
    )
    from app.web.store_routes import _render_prep_list_v3

    prep_position = Position(name="Prep", store_key=None)
    cook_position = Position(name="Cook", store_key=None)
    prep_employee = Employee(full_name="Paul Prep", active=True)
    cook_employee = Employee(full_name="Carl Cook", active=True)
    item = PrepItem(name="Masa Flour", category="hot", kind="item", sort_order=1)
    schedule = Schedule(store_key="copperfield", week_start=date(2026, 6, 1))
    db_session.add_all([
        prep_position,
        cook_position,
        prep_employee,
        cook_employee,
        item,
        schedule,
    ])
    db_session.flush()
    db_session.add_all([
        Shift(
            schedule_id=schedule.id,
            employee_id=prep_employee.id,
            position_id=prep_position.id,
            start_at=datetime(2026, 6, 7, 9, 0),
            end_at=datetime(2026, 6, 7, 15, 0),
            status="assigned",
        ),
        Shift(
            schedule_id=schedule.id,
            employee_id=cook_employee.id,
            position_id=cook_position.id,
            start_at=datetime(2026, 6, 7, 9, 0),
            end_at=datetime(2026, 6, 7, 15, 0),
            status="assigned",
        ),
        PrepEntry(
            entry_date=date(2026, 6, 7),
            prep_item_id=item.id,
            selected=True,
            status="partly",
            assignee_name="Paul Prep",
        ),
    ])
    db_session.commit()

    captured = {}

    def fake_render(_template, **context):
        captured.update(context)
        return "ok"

    monkeypatch.setattr("app.web.store_routes.render_template", fake_render)

    app = Flask(__name__)
    app.secret_key = "test"
    with app.test_request_context("/uno/kitchen/prep-list?date=2026-06-07"):
        g.current_location = "copperfield"
        assert _render_prep_list_v3(db_session, "Prep List", "kitchen_prep_list") == "ok"

    team = captured["team"]
    assert [member["name"] for member in team] == ["Paul Prep"]
    assert team[0]["has_assignment"] is True
    assert team[0]["assignment_count"] == 1
    assert team[0]["in_progress"] == 1
    assert team[0]["shift_label"] == "9:00 AM-3:00 PM"
