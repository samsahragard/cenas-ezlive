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
    ShiftSwap,
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


def test_manager_market_cards_opt_into_profile_employee_ids(db_session):
    db = db_session
    server = Position(name="Server", store_key=None)
    db.add(server)
    db.flush()

    offerer = _employee(101, "Offer Owner")
    taker = _employee(102, "Profile Linked Server")
    db.add_all([offerer, taker])
    db.flush()

    schedule = Schedule(id=101, store_key="tomball", week_start=date(2026, 6, 7), status="published")
    shift = Shift(
        id=101,
        schedule_id=schedule.id,
        employee_id=offerer.id,
        position_id=server.id,
        start_at=datetime(2026, 6, 9, 10),
        end_at=datetime(2026, 6, 9, 15),
        status="assigned",
    )
    offer = ShiftOffer(
        id=101,
        shift_id=shift.id,
        offered_by_employee_id=offerer.id,
        taken_by_employee_id=taker.id,
        status="taken",
        restricted=False,
    )
    db.add_all([schedule, shift, offer])
    db.commit()

    employee_card = scheduling_offers.offer_card(db, offer)
    manager_card = scheduling_offers.offer_card(db, offer, include_employee_ids=True)

    assert "id" not in employee_card["offered_by"]
    assert "id" not in employee_card["taken_by"]
    assert manager_card["offered_by"]["id"] == offerer.id
    assert manager_card["taken_by"]["id"] == taker.id


def test_manager_market_swap_cards_opt_into_profile_employee_ids(db_session):
    db = db_session
    server = Position(name="Server", store_key=None)
    db.add(server)
    db.flush()

    first = _employee(111, "First Server")
    second = _employee(112, "Second Server")
    db.add_all([first, second])
    db.flush()

    schedule = Schedule(id=111, store_key="tomball", week_start=date(2026, 6, 7), status="published")
    from_shift = Shift(
        id=111,
        schedule_id=schedule.id,
        employee_id=first.id,
        position_id=server.id,
        start_at=datetime(2026, 6, 9, 10),
        end_at=datetime(2026, 6, 9, 15),
        status="assigned",
    )
    to_shift = Shift(
        id=112,
        schedule_id=schedule.id,
        employee_id=second.id,
        position_id=server.id,
        start_at=datetime(2026, 6, 10, 10),
        end_at=datetime(2026, 6, 10, 15),
        status="assigned",
    )
    swap = ShiftSwap(
        id=111,
        from_shift_id=from_shift.id,
        to_shift_id=to_shift.id,
        from_employee_id=first.id,
        to_employee_id=second.id,
        status="accepted",
    )
    db.add_all([schedule, from_shift, to_shift, swap])
    db.commit()

    employee_card = scheduling_offers.swap_card(db, swap)
    manager_card = scheduling_offers.swap_card(db, swap, include_employee_ids=True)

    assert "id" not in employee_card["from_employee"]
    assert "id" not in employee_card["to_employee"]
    assert manager_card["from_employee"]["id"] == first.id
    assert manager_card["to_employee"]["id"] == second.id


def test_marketplace_profile_links_are_access_gated():
    api_source = open("app/web/schedules_v2_market.py", encoding="utf-8").read()
    page_source = open("app/web/schedules_v2_pages.py", encoding="utf-8").read()
    template = open("app/templates/schedules_v2_marketplace.html", encoding="utf-8").read()

    assert "def _include_profile_ids" in api_source
    assert "level_at_least" in api_source
    assert '"profileBaseUrl": profile_base_url' in page_source
    assert "CFG.profileBaseUrl" in template


def test_marketplace_has_mobile_card_table_layout():
    template = open("app/templates/schedules_v2_marketplace.html", encoding="utf-8").read()

    assert "@media (max-width: 780px)" in template
    assert ".sv2-mk table{min-width:0" in template
    assert ".sv2-mk,.sv2-mk .mk-surface,.sv2-mk .mk-pad{max-width:100%;overflow-x:clip}" in template
    assert "margin-right:0;scrollbar-width:none" in template
    assert ".sv2-mk thead{display:none}" in template
    assert "content:attr(data-label)" in template
    assert 'data-label="Shift"' in template
    assert 'data-label="Employee A"' in template
    assert 'class="swap-arrow"' in template
    assert "@media (max-width: 430px)" in template


def test_manager_marketplace_omits_summary_stat_cards():
    template = open("app/templates/schedules_v2_marketplace.html", encoding="utf-8").read()

    assert 'id="mk-stats"' not in template
    assert "mk-stat" not in template
    assert "function renderStats()" not in template
    assert "function render(){ renderChips(); renderFilters(); renderContent(); }" in template


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
