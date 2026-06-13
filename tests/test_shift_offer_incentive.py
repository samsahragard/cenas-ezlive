"""Shift-market money offers (Sam 2026-06-13): an employee can attach a cash
incentive to a released shift. Money is a DISPLAYED incentive only (settled
offline) -- these tests pin the dollar->cents parse bounds and that offer_card
surfaces the amount to both the employee browse cards and the manager dashboard.
"""
from __future__ import annotations

from datetime import date, datetime

from app.models import Employee, Schedule, Shift, ShiftOffer
from app.services.scheduling_offers import offer_card
from app.web.employee_shift_market import _parse_incentive


def test_parse_incentive_bounds():
    assert _parse_incentive(None) == (None, None)
    assert _parse_incentive("") == (None, None)
    assert _parse_incentive("0") == (None, None)            # zero -> no money attached
    assert _parse_incentive("25") == (2500, None)
    assert _parse_incentive("$20.50") == (2050, None)
    assert _parse_incentive("-5")[1] is not None            # negative rejected
    assert _parse_incentive("9999")[1] is not None          # over the $500 cap
    assert _parse_incentive("abc")[1] is not None           # non-numeric rejected


def _seed(db):
    emp = Employee(id=63, full_name="Alexa Rodriguez", active=True)
    sched = Schedule(id=19, store_key="copperfield",
                     week_start=date(2026, 6, 6), status="published")
    db.add_all([emp, sched])
    db.flush()
    sh = Shift(id=2359, schedule_id=19, employee_id=63, position_id=5, status="assigned",
               start_at=datetime(2026, 6, 13, 16), end_at=datetime(2026, 6, 13, 22))
    db.add(sh)
    db.flush()
    return sh


def test_offer_card_surfaces_incentive(db_session):
    db = db_session
    _seed(db)
    paid = ShiftOffer(id=1, shift_id=2359, offered_by_employee_id=63,
                      status="open", restricted=True, incentive_cents=2500)
    db.add(paid)
    db.commit()
    card = offer_card(db, paid)                              # employee browse card
    assert card["incentive_cents"] == 2500
    assert card["incentive"] == 25.0
    mgr = offer_card(db, paid, include_employee_ids=True)    # manager dashboard card
    assert mgr["incentive"] == 25.0


def test_offer_card_free_offer_has_null_incentive(db_session):
    db = db_session
    _seed(db)
    free = ShiftOffer(id=2, shift_id=2359, offered_by_employee_id=63,
                      status="open", restricted=True)        # no incentive
    db.add(free)
    db.commit()
    card = offer_card(db, free)
    assert card["incentive_cents"] is None
    assert card["incentive"] is None
