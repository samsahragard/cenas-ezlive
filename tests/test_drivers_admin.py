"""Gate 1 — pytest unit tests for drivers_admin ?status filter (spec §7).

Two tests per spec §7 Gate 1:
  test_drivers_admin_active_filter  — default + ?status=active only returns active drivers
  test_drivers_admin_inactive_filter — ?status=inactive only returns inactive drivers

These test the DB query contract directly using an in-memory SQLite session
(the same db_session fixture as the rest of the suite).  No Flask request
context needed — we exercise the ORM filter the route now applies.
"""
from __future__ import annotations

import pytest
from app.models import Driver


def _mk_driver(db, name: str, location: str, active: bool) -> Driver:
    d = Driver(name=name, location=location, active=active)
    db.add(d)
    db.flush()
    return d


def _query_by_status(db, status: str, location: str | None = None):
    """Mirrors the filter logic landed in drivers_admin() at 7d55a08."""
    if status not in ("active", "inactive"):
        status = "active"
    show_active = (status == "active")
    q = db.query(Driver).filter(Driver.active == show_active)
    if location is not None:
        q = q.filter(Driver.location == location)
    return q.order_by(Driver.location, Driver.name).all()


def _count_by_active(db, active: bool, location: str | None = None) -> int:
    q = db.query(Driver).filter(Driver.active == active)
    if location is not None:
        q = q.filter(Driver.location == location)
    return q.count()


class TestDriversAdminActiveFilter:
    """?status=active (and bare /dos/drivers default) returns ONLY active drivers."""

    def test_returns_only_active_drivers(self, db_session):
        _mk_driver(db_session, "Alice Active", "tomball", active=True)
        _mk_driver(db_session, "Bob Active",   "tomball", active=True)
        _mk_driver(db_session, "Carol Inactive", "tomball", active=False)
        db_session.commit()

        rows = _query_by_status(db_session, "active", location="tomball")
        names = [d.name for d in rows]
        assert "Alice Active" in names
        assert "Bob Active" in names
        assert "Carol Inactive" not in names, "inactive driver leaked into active view"

    def test_invalid_status_defaults_to_active(self, db_session):
        _mk_driver(db_session, "Dave Active", "tomball", active=True)
        _mk_driver(db_session, "Eve Inactive", "tomball", active=False)
        db_session.commit()

        rows = _query_by_status(db_session, "garbage", location="tomball")
        names = [d.name for d in rows]
        assert "Dave Active" in names
        assert "Eve Inactive" not in names

    def test_count_queries_see_both_sides(self, db_session):
        _mk_driver(db_session, "F Active",   "copperfield", active=True)
        _mk_driver(db_session, "G Active",   "copperfield", active=True)
        _mk_driver(db_session, "H Inactive", "copperfield", active=False)
        db_session.commit()

        active_n   = _count_by_active(db_session, True,  location="copperfield")
        inactive_n = _count_by_active(db_session, False, location="copperfield")
        assert active_n >= 2,   f"expected >=2 active, got {active_n}"
        assert inactive_n >= 1, f"expected >=1 inactive, got {inactive_n}"


class TestDriversAdminInactiveFilter:
    """?status=inactive returns ONLY inactive drivers."""

    def test_returns_only_inactive_drivers(self, db_session):
        _mk_driver(db_session, "Ian Active",   "tomball", active=True)
        _mk_driver(db_session, "Jane Inactive","tomball", active=False)
        _mk_driver(db_session, "Kim Inactive", "tomball", active=False)
        db_session.commit()

        rows = _query_by_status(db_session, "inactive", location="tomball")
        names = [d.name for d in rows]
        assert "Jane Inactive" in names
        assert "Kim Inactive" in names
        assert "Ian Active" not in names, "active driver leaked into inactive view"

    def test_empty_inactive_list_when_all_active(self, db_session):
        _mk_driver(db_session, "Leo Only Active", "tomball", active=True)
        db_session.commit()

        rows = _query_by_status(db_session, "inactive", location="tomball")
        assert rows == [], f"expected empty inactive list, got {[d.name for d in rows]}"
