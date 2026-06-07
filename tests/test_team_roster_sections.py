"""S4 tests: team_roster() per-store SECTION grouping (additive, back-compat).

team_roster() now adds, per store dict, pre-partitioned 'management' / 'hourly'
lists (alongside the untouched 'employees' full list), and each employee row
carries a 'section'. Placement is by the row's HIGHEST section
(management > hourly) via role_buckets.section_for_position. Driver-only /
non-section-placed rows land in NEITHER group. addable_positions_for() now
annotates each addable position with its 'section'.

DB-backed via the shared in-memory db_session fixture (tests/conftest.py).
"""
from __future__ import annotations

from app.models import (Employee, EmployeePosition, EmployeeStoreAssignment,
                        Position)
from app.services.role_buckets import SECTION_HOURLY, SECTION_MANAGEMENT
from app.services.team_roster import addable_positions_for, team_roster


def _pos(db, name, pid):
    p = Position(id=pid, name=name, store_key=None)
    db.add(p)
    return p


def _emp(db, eid, name, stores, positions_by_id, active=True):
    """Seed an Employee with store assignments + per-store positions.
    positions_by_id: {store_key: [position_id, ...]}."""
    db.add(Employee(id=eid, full_name=name, active=active))
    for sk in stores:
        db.add(EmployeeStoreAssignment(employee_id=eid, store_key=sk))
    for sk, pids in positions_by_id.items():
        for pid in pids:
            db.add(EmployeePosition(employee_id=eid, position_id=pid, store_key=sk))


def _seed(db):
    # Canonical positions we exercise (ids arbitrary but stable).
    _pos(db, "GM", 1)        # management
    _pos(db, "KM", 2)        # management
    _pos(db, "Server", 3)    # hourly
    _pos(db, "Cook", 4)      # hourly
    _pos(db, "Well", 5)      # hourly (role 'well')
    _pos(db, "Hostess", 6)   # hourly (role 'host')

    # Tomball roster:
    #  - Gina: GM only            -> management
    #  - Carl: Cook only          -> hourly
    #  - Mia:  Server + KM (span) -> management (highest wins)
    #  - Wendy: Well only         -> hourly
    _emp(db, 10, "Gina GM",  ["tomball"], {"tomball": [1]})
    _emp(db, 11, "Carl Cook", ["tomball"], {"tomball": [4]})
    _emp(db, 12, "Mia Span", ["tomball"], {"tomball": [3, 2]})
    _emp(db, 13, "Wendy Well", ["tomball"], {"tomball": [5]})
    db.commit()


def _store(result, store_key):
    return next(s for s in result["stores"] if s["store_key"] == store_key)


def test_store_dict_has_section_groups_and_keeps_employees(db_session):
    _seed(db_session)
    res = team_roster(db_session, location="tomball")
    tom = _store(res, "tomball")

    # Additive: the full 'employees' list is still present + complete.
    assert set(tom) >= {"employees", "management", "hourly"}
    assert {r["full_name"] for r in tom["employees"]} == {
        "Gina GM", "Carl Cook", "Mia Span", "Wendy Well"}
    assert tom["shown"] == 4


def test_partition_by_highest_section(db_session):
    _seed(db_session)
    res = team_roster(db_session, location="tomball")
    tom = _store(res, "tomball")

    mgmt = {r["full_name"] for r in tom["management"]}
    hrly = {r["full_name"] for r in tom["hourly"]}

    # Gina (GM) + Mia (Server+KM -> highest = management) in management.
    assert mgmt == {"Gina GM", "Mia Span"}
    # Carl (Cook) + Wendy (Well) in hourly. Mia is NOT double-counted.
    assert hrly == {"Carl Cook", "Wendy Well"}
    assert mgmt.isdisjoint(hrly)


def test_employee_row_carries_section(db_session):
    _seed(db_session)
    res = team_roster(db_session, location="tomball")
    by_name = {r["full_name"]: r for r in _store(res, "tomball")["employees"]}

    assert by_name["Gina GM"]["section"] == SECTION_MANAGEMENT
    assert by_name["Mia Span"]["section"] == SECTION_MANAGEMENT  # highest wins
    assert by_name["Carl Cook"]["section"] == SECTION_HOURLY
    assert by_name["Wendy Well"]["section"] == SECTION_HOURLY


def test_non_section_employee_in_neither_group(db_session):
    """An employee with NO section-placed position (e.g. only a tier-above
    'Partner' position) is in 'employees' but in neither management nor hourly."""
    _pos(db_session, "Partner", 7)  # tier-above -> section None
    db_session.add(Employee(id=20, full_name="Pat Partner", active=True))
    db_session.add(EmployeeStoreAssignment(employee_id=20, store_key="tomball"))
    db_session.add(EmployeePosition(employee_id=20, position_id=7,
                                    store_key="tomball"))
    db_session.commit()

    res = team_roster(db_session, location="tomball")
    tom = _store(res, "tomball")
    names_emp = {r["full_name"] for r in tom["employees"]}
    names_grouped = ({r["full_name"] for r in tom["management"]}
                     | {r["full_name"] for r in tom["hourly"]})

    assert "Pat Partner" in names_emp
    assert "Pat Partner" not in names_grouped
    # The grouped row carries section None.
    pat = next(r for r in tom["employees"] if r["full_name"] == "Pat Partner")
    assert pat["section"] is None


def test_groups_present_for_all_stores_view(db_session):
    _seed(db_session)
    # Add a Copperfield-only hourly employee.
    db_session.add(Employee(id=30, full_name="Cara Copper", active=True))
    db_session.add(EmployeeStoreAssignment(employee_id=30, store_key="copperfield"))
    db_session.add(EmployeePosition(employee_id=30, position_id=3,
                                    store_key="copperfield"))  # Server
    db_session.commit()

    res = team_roster(db_session, location="all")
    for s in res["stores"]:
        assert "management" in s and "hourly" in s
    cop = _store(res, "copperfield")
    assert {r["full_name"] for r in cop["hourly"]} == {"Cara Copper"}
    assert cop["management"] == []


def test_addable_positions_annotated_with_section(db_session):
    _seed(db_session)
    # A GM actor can add roles strictly below GM rank (Asst-KM/FOH-Mgr + floor),
    # which here means the hourly canonical positions we seeded (Server/Cook/
    # Well/Hostess). GM/KM are NOT addable by a GM (peers), so they won't appear.
    out = addable_positions_for("gm", db_session)
    assert out, "GM should have addable positions"
    by_name = {p["name"]: p for p in out}
    for nm in ("Server", "Cook", "Well", "Hostess"):
        assert nm in by_name, nm
        assert by_name[nm]["section"] == SECTION_HOURLY, nm
    # Every entry carries id, name, section (additive key).
    for p in out:
        assert set(p) >= {"id", "name", "section"}
