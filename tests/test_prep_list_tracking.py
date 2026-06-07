from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from flask import Flask, g


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
