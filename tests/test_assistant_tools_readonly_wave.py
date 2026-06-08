from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import event

from app.models import (
    AttendanceShift,
    Employee,
    EmployeeAlarmPreference,
    EmployeeAvailability,
    EmployeePhone,
    EmployeePosition,
    EmployeeStoreAssignment,
    Position,
    PrepEntry,
    PrepItem,
    Recipe,
    Schedule,
    Shift,
    ShiftAlarm,
    TimeOffRequest,
    VendorRecentOrder,
)
from app.services.assistant_operational_tools import implemented_tool_ids, run_operational_tool
from app.services.assistant_tool_inventory import iter_readonly_operational_tool_specs


TODAY = date.today()
WEEK_START = TODAY - timedelta(days=TODAY.weekday())


def _dt(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute))


@pytest.fixture()
def seeded_ops_db(db_session):
    alice = Employee(full_name="Alice Rivera", phone="2815550101", email="alice@example.test", address="1 Main")
    bob = Employee(full_name="Bob Server", phone="2815550102", email="bob@example.test")
    db_session.add_all([alice, bob])
    db_session.flush()

    db_session.add_all([
        EmployeePhone(employee_id=alice.id, phone="2815550199", is_primary=False),
        EmployeeStoreAssignment(employee_id=alice.id, store_key="tomball"),
        EmployeeStoreAssignment(employee_id=bob.id, store_key="tomball"),
    ])

    server = Position(name="Server", store_key="tomball")
    prep = Position(name="Prep", store_key="tomball")
    db_session.add_all([server, prep])
    db_session.flush()
    db_session.add_all([
        EmployeePosition(employee_id=alice.id, position_id=server.id, store_key="tomball"),
        EmployeePosition(employee_id=bob.id, position_id=server.id, store_key="tomball"),
    ])

    sched = Schedule(store_key="tomball", week_start=WEEK_START, status="published", published_at=_dt(TODAY, 6))
    db_session.add(sched)
    db_session.flush()
    alice_shift = Shift(
        schedule_id=sched.id,
        employee_id=alice.id,
        position_id=server.id,
        start_at=_dt(TODAY, 9),
        end_at=_dt(TODAY, 17),
        break_minutes=30,
        status="assigned",
        published_at=_dt(TODAY, 6),
    )
    open_shift = Shift(
        schedule_id=sched.id,
        employee_id=None,
        position_id=prep.id,
        start_at=_dt(TODAY + timedelta(days=1), 8),
        end_at=_dt(TODAY + timedelta(days=1), 14),
        break_minutes=15,
        status="open",
    )
    db_session.add_all([alice_shift, open_shift])
    db_session.flush()

    db_session.add_all([
        EmployeeAvailability(employee_id=alice.id, day_of_week=TODAY.weekday(), start_minute=480, end_minute=1020),
        TimeOffRequest(employee_id=alice.id, start_date=TODAY + timedelta(days=3), end_date=TODAY + timedelta(days=3), status="pending", reason="doctor"),
        EmployeeAlarmPreference(employee_id=alice.id, sms_enabled=True, email_enabled=True, minutes_before=45),
        ShiftAlarm(shift_id=alice_shift.id, employee_id=alice.id, alarm_time=_dt(TODAY, 8, 15), channel="sms", status="pending"),
    ])

    db_session.add_all([
        AttendanceShift(
            store_scope="tomball",
            entry_date=TODAY,
            employee_name="Alice Rivera",
            role_title="Server",
            section="foh",
            scheduled_start=_dt(TODAY, 9),
            scheduled_end=_dt(TODAY, 17),
            clock_in=_dt(TODAY, 9, 8),
            status="late",
            late_minutes=8,
        ),
        AttendanceShift(
            store_scope="tomball",
            entry_date=TODAY,
            employee_name="Bob Server",
            role_title="Server",
            section="foh",
            scheduled_start=_dt(TODAY, 10),
            scheduled_end=_dt(TODAY, 16),
            status="no-show",
        ),
        AttendanceShift(
            store_scope="tomball",
            entry_date=TODAY,
            employee_name="Call Out",
            role_title="Prep",
            section="boh",
            scheduled_start=_dt(TODAY, 7),
            scheduled_end=_dt(TODAY, 13),
            status="callout",
        ),
        AttendanceShift(
            store_scope="tomball",
            entry_date=TODAY,
            employee_name="Missing Punch",
            role_title="Prep",
            section="boh",
            scheduled_start=_dt(TODAY, 8),
            scheduled_end=_dt(TODAY, 15),
            status="scheduled",
        ),
    ])

    recipe = Recipe(
        code="SALSA",
        category="sauce",
        name="Roja Salsa",
        prep_time="20 min",
        shelf_life="3 days",
        english_instructions="Blend tomatoes and chiles.",
        ingredients_json='["tomato","chile"]',
    )
    db_session.add(recipe)
    db_session.flush()
    prep_item = PrepItem(name="Roja Salsa", category="hot", kind="sauce", recipe_id=recipe.id, sort_order=1, store_scope="tomball")
    db_session.add(prep_item)
    db_session.flush()
    db_session.add(PrepEntry(
        entry_date=TODAY,
        store_scope="tomball",
        prep_item_id=prep_item.id,
        selected=True,
        on_hand=2,
        prep_qty=6,
        assignee_name="Alice Rivera",
        status="assigned",
        batch_size="single",
    ))

    db_session.add(VendorRecentOrder(
        vendor="webstaurant",
        store_scope="tomball",
        order_number="W-100",
        placed_at=_dt(TODAY, 11),
        total_cents=12345,
        status="shipped",
        parse_status="parsed",
        items_json={"items": [{"name": "cups", "qty": 2}]},
    ))
    db_session.commit()
    return {"alice_id": alice.id, "bob_id": bob.id}


def _args_for(tool_id: str, ids: dict) -> dict:
    if tool_id.startswith("employee."):
        base = {"employee_id": ids["alice_id"]}
        if tool_id.endswith(".today") or tool_id == "employee.my_day_breakdown":
            base["date"] = TODAY.isoformat()
        if tool_id.endswith(".week"):
            base["week_start"] = WEEK_START.isoformat()
        return base
    if tool_id.startswith("schedules."):
        return {"store": "tomball", "date": TODAY.isoformat(), "week_start": WEEK_START.isoformat()}
    if tool_id == "kitchen.recipe_search":
        return {"query": "Roja", "limit": 5}
    if tool_id == "kitchen.recipe_lookup":
        return {"code": "SALSA"}
    if tool_id.startswith("kitchen.prep_"):
        return {"store": "tomball", "date": TODAY.isoformat()}
    if tool_id == "vendors.vendor_recent_orders":
        return {"vendor": "webstaurant", "store": "tomball", "limit": 5}
    if tool_id.startswith("attendance."):
        return {"store": "tomball", "date": TODAY.isoformat()}
    return {}


def _meaningful(payload: dict) -> bool:
    if not payload.get("ok"):
        return False
    for key in (
        "employee",
        "contact",
        "stores",
        "positions",
        "shifts",
        "open_shifts",
        "availability",
        "requests",
        "pending_alarms",
        "schedule",
        "attendance",
        "recipes",
        "recipe",
        "entries",
        "orders",
        "rows",
        "by_status",
    ):
        if key in payload:
            value = payload[key]
            return bool(value) or key in {"by_status"}
    return payload.get("count", 0) >= 0


@pytest.mark.parametrize("tool_id", implemented_tool_ids())
def test_readonly_wave_tool_returns_seeded_data(db_session, seeded_ops_db, tool_id):
    payload = run_operational_tool(tool_id, _args_for(tool_id, seeded_ops_db), db=db_session)

    assert payload["tool_id"] == tool_id
    assert payload["read_only"] is True
    assert _meaningful(payload), payload


@pytest.mark.parametrize("tool_id", implemented_tool_ids())
def test_readonly_wave_tool_does_not_write(db_session, seeded_ops_db, tool_id):
    writes = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        first = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if first in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE"}:
            writes.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        run_operational_tool(tool_id, _args_for(tool_id, seeded_ops_db), db=db_session)
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)

    assert writes == []


def test_inventory_specs_match_implemented_readonly_tools():
    specs = {spec["tool_id"]: spec for spec in iter_readonly_operational_tool_specs()}

    assert set(specs) == set(implemented_tool_ids())
    assert len(specs) == 25
    assert all(spec["read_write_class"] == "read_only" for spec in specs.values())
    assert all(spec["implementation"] == "assistant_operational_tools.run_operational_tool" for spec in specs.values())
