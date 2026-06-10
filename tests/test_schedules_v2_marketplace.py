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
    ShiftOffer,
)
from app.services import scheduling_offers
from app.web.schedules_v2_market import _list_status


def _employee(eid: int, name: str, *, active: bool = True) -> Employee:
    return Employee(id=eid, full_name=name, active=active)


def _assign(db, employee_id: int, store_key: str, position_id: int | None = None) -> None:
    db.add(EmployeeStoreAssignment(employee_id=employee_id, store_key=store_key))
    if position_id is not None:
        db.add(EmployeePosition(employee_id=employee_id, store_key=store_key, position_id=position_id))


def _link(db, employee_id: int, store_key: str) -> None:
    db.add(CenaToastLink(
        cena_employee_id=employee_id,
        store_key=store_key,
        toast_id=f"toast-{store_key}-{employee_id}",
        toast_name=f"Toast {employee_id}",
    ))


def test_market_status_filter_maps_ui_labels_to_workflow_states():
    assert _list_status("pending", "offer") == "taken"
    assert _list_status("pending", "swap") == "accepted"
    assert _list_status("all", "offer") is None
    assert _list_status("", "swap") is None
    assert _list_status("open", "offer") == "open"


def test_offer_eligibility_requires_active_linked_store_and_position(db_session):
    db = db_session
    server = Position(name="Server", store_key=None)
    cook = Position(name="Cook", store_key=None)
    db.add_all([server, cook])
    db.flush()

    offerer = _employee(1, "Offer Owner")
    eligible = _employee(2, "Eligible Server")
    inactive = _employee(3, "Inactive Server", active=False)
    unlinked = _employee(4, "Unlinked Server")
    wrong_store = _employee(5, "Copperfield Server")
    wrong_position = _employee(6, "Linked Cook")
    no_position = _employee(7, "Linked No Position")
    db.add_all([offerer, eligible, inactive, unlinked, wrong_store, wrong_position, no_position])
    db.flush()

    _assign(db, offerer.id, "tomball", server.id)
    _assign(db, eligible.id, "tomball", server.id)
    _assign(db, inactive.id, "tomball", server.id)
    _assign(db, unlinked.id, "tomball", server.id)
    _assign(db, wrong_store.id, "copperfield", server.id)
    _assign(db, wrong_position.id, "tomball", cook.id)
    _assign(db, no_position.id, "tomball")
    for emp_id, store in (
        (offerer.id, "tomball"),
        (eligible.id, "tomball"),
        (inactive.id, "tomball"),
        (wrong_store.id, "copperfield"),
        (wrong_position.id, "tomball"),
        (no_position.id, "tomball"),
    ):
        _link(db, emp_id, store)

    schedule = Schedule(id=1, store_key="tomball", week_start=date(2026, 6, 7), status="published")
    db.add(schedule)
    db.flush()
    shift = Shift(
        id=1,
        schedule_id=schedule.id,
        employee_id=offerer.id,
        position_id=server.id,
        start_at=datetime(2026, 6, 9, 10),
        end_at=datetime(2026, 6, 9, 15),
        status="assigned",
    )
    offer = ShiftOffer(
        id=1,
        shift_id=shift.id,
        offered_by_employee_id=offerer.id,
        status="open",
        restricted=False,
    )
    db.add_all([shift, offer])
    db.commit()

    assert scheduling_offers.is_eligible_taker(db, offer, eligible.id)
    assert not scheduling_offers.is_eligible_taker(db, offer, offerer.id)
    assert not scheduling_offers.is_eligible_taker(db, offer, inactive.id)
    assert not scheduling_offers.is_eligible_taker(db, offer, unlinked.id)
    assert not scheduling_offers.is_eligible_taker(db, offer, wrong_store.id)
    assert not scheduling_offers.is_eligible_taker(db, offer, wrong_position.id)
    assert not scheduling_offers.is_eligible_taker(db, offer, no_position.id)
