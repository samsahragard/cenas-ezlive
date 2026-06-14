"""Time-off request policy (Sam 2026-06-13): a manager sets approval-required +
an N-days-in-advance cutoff per store (Operations -> Team -> Settings); the
employee's date picker + the request endpoint then block today..today+N. These
tests pin the policy store + the most-restrictive resolution across stores + the
earliest-allowed-date math.
"""
from __future__ import annotations

from datetime import date

from app.models import Employee, EmployeeStoreAssignment
from app.services import timeoff_policy


def test_defaults_and_set_get(db_session):
    db = db_session
    assert timeoff_policy.get_policy(db, "copperfield") == {
        "require_approval": True, "cutoff_enabled": False, "cutoff_days": 14}
    timeoff_policy.set_policy(db, "copperfield", require_approval=False,
                              cutoff_enabled=True, cutoff_days=10)
    assert timeoff_policy.get_policy(db, "copperfield") == {
        "require_approval": False, "cutoff_enabled": True, "cutoff_days": 10}


def test_set_policy_clamps_days(db_session):
    db = db_session
    assert timeoff_policy.set_policy(db, "x", require_approval=True,
                                     cutoff_enabled=True, cutoff_days=9999)["cutoff_days"] == 365
    assert timeoff_policy.set_policy(db, "y", require_approval=True,
                                     cutoff_enabled=True, cutoff_days=-5)["cutoff_days"] == 0


def test_effective_is_most_restrictive_across_stores(db_session):
    db = db_session
    db.add(Employee(id=63, full_name="Alexa", active=True))
    db.add_all([EmployeeStoreAssignment(employee_id=63, store_key="copperfield"),
                EmployeeStoreAssignment(employee_id=63, store_key="tomball")])
    timeoff_policy.set_policy(db, "copperfield", require_approval=False,
                              cutoff_enabled=True, cutoff_days=7)
    timeoff_policy.set_policy(db, "tomball", require_approval=True,
                              cutoff_enabled=True, cutoff_days=21)
    eff = timeoff_policy.effective_for_employee(db, 63)
    assert eff["require_approval"] is True   # approval if ANY store requires it
    assert eff["cutoff_enabled"] is True
    assert eff["cutoff_days"] == 21          # the largest enabled cutoff


def test_no_assignment_returns_defaults(db_session):
    db = db_session
    db.add(Employee(id=99, full_name="Nobody", active=True))
    db.commit()
    assert timeoff_policy.effective_for_employee(db, 99) == {
        "require_approval": True, "cutoff_enabled": False, "cutoff_days": 14}


def test_earliest_allowed_start_math():
    today = date(2026, 6, 13)
    assert timeoff_policy.earliest_allowed_start(
        {"cutoff_enabled": False, "cutoff_days": 14}, today) is None
    assert timeoff_policy.earliest_allowed_start(
        {"cutoff_enabled": True, "cutoff_days": 14}, today) == date(2026, 6, 27)
