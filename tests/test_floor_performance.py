"""Gate 3 self-tests for app/floor_performance.py (floor_contract section 11).

No network: a FakeToast client is injected via performance_for_date's
`client=` seam. DB pattern follows tests/test_floor_routes.py: the shared
in-memory db_session with app.db.SessionLocal monkeypatched.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("ALLOW_DEV_SECRET", "1")

from datetime import date, datetime

from app.floor_models import (
    FloorSeating,
    FloorSection,
    FloorSectionTable,
    ToastTableCfg,
    ensure_floor_tables,
)
from app.floor_performance import main, performance_for_date

UNO_GUID = "test-guid-uno"
DOS_GUID = "test-guid-dos"

# Fixed business date: 2026-06-10 (CDT, UTC-5). 18:00 UTC = 13:00 local.
D = date(2026, 6, 10)
D_ISO = D.isoformat()
MIDDAY = datetime(2026, 6, 10, 18, 0, 0)

FAKE_EMPLOYEES = [
    {"guid": "emp-kayla", "firstName": "Kayla", "lastName": "Gomez"},
    {"guid": "emp-marcus", "firstName": "Marcus", "lastName": "Lee"},
]


class FakeToast:
    """Stand-in for ToastClient: canned orders + employees, call log."""

    def __init__(self, orders=None, employees=FAKE_EMPLOYEES,
                 employees_raise=False):
        self.orders = orders or []
        self.employees = employees
        self.employees_raise = employees_raise
        self.order_calls: list[tuple] = []

    def fetch_orders_for_date(self, location, restaurant_guid, business_date,
                              refresh=False):
        self.order_calls.append((location, restaurant_guid, business_date))
        return self.orders

    def fetch_employees(self, location, restaurant_guid, refresh=False):
        if self.employees_raise:
            raise RuntimeError("toast down")
        return list(self.employees)


def _order(table=None, server=None, checks=None, **extra):
    o: dict = {"checks": [] if checks is None else checks}
    if table is not None:
        o["table"] = {"guid": table}
    if server is not None:
        o["server"] = {"guid": server}
    o.update(extra)
    return o


def _check(amount=0.0, table=None, server=None, **extra):
    c: dict = {"amount": amount}
    if table is not None:
        c["table"] = {"guid": table}
    if server is not None:
        c["server"] = {"guid": server}
    c.update(extra)
    return c


@pytest.fixture
def perf_db(db_session, monkeypatch):
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_COPPERFIELD", UNO_GUID)
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_TOMBALL", DOS_GUID)
    from app import db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    ensure_floor_tables(db_session.get_bind())
    return db_session


def _seed_section(db, server_guid, table_guids, *, d=D, color="#14B8A6",
                  loc_guid=UNO_GUID):
    sec = FloorSection(location_guid=loc_guid, shift_date=d,
                       server_employee_guid=server_guid, color=color,
                       created_by="test", created_at=MIDDAY)
    db.add(sec)
    db.flush()
    for tg in table_guids:
        db.add(FloorSectionTable(section_id=sec.id, table_guid=tg))
    db.commit()
    return sec


def _seed_seating(db, table_guid, party_size, server=None, *,
                  seated_at=MIDDAY, cleared_at=None, loc_guid=UNO_GUID):
    db.add(FloorSeating(location_guid=loc_guid, table_guid=table_guid,
                        party_size=party_size, seated_at=seated_at,
                        seated_by="test",
                        server_employee_guid_at_seat=server,
                        cleared_at=cleared_at))
    db.commit()


def _seed_table_cfg(db, guid, name, *, loc_guid=UNO_GUID, deleted=False):
    db.add(ToastTableCfg(guid=guid, location_guid=loc_guid, name=name,
                         deleted=deleted))
    db.commit()


def _by_server(report):
    return {r["server_employee_guid"]: r for r in report["per_server"]}


def _by_table(report):
    return {r["table_guid"]: r for r in report["planned_vs_actual"]}


# ---------------------------------------------------------------------------
# Location/date resolution
# ---------------------------------------------------------------------------

def test_location_resolution_key_and_slug(perf_db):
    fake = FakeToast()
    by_slug = performance_for_date("uno", D_ISO, client=fake)
    by_key = performance_for_date("copperfield", D_ISO, client=fake)
    assert by_slug["location"] == by_key["location"]
    assert by_slug["location"]["guid"] == UNO_GUID
    assert by_slug["date"] == D_ISO
    # Toast called with the location key + restaurant guid + YYYYMMDD
    assert fake.order_calls[0] == ("copperfield", UNO_GUID, "20260610")

    with pytest.raises(ValueError):
        performance_for_date("tres", D_ISO, client=fake)
    with pytest.raises(ValueError):
        performance_for_date("uno", "junk-date", client=fake)


# ---------------------------------------------------------------------------
# Covers from seatings (incl. cleared) + per-location isolation
# ---------------------------------------------------------------------------

def test_covers_from_seatings_including_cleared(perf_db):
    db = perf_db
    _seed_section(db, "emp-kayla", ["tbl-1", "tbl-2"])
    _seed_seating(db, "tbl-1", 4, "emp-kayla")
    _seed_seating(db, "tbl-2", 2, "emp-kayla",
                  cleared_at=datetime(2026, 6, 10, 19, 0, 0))  # cleared counts
    _seed_seating(db, "tbl-9", 3, None)  # NULL server: not attributable
    _seed_seating(db, "tbl-d1", 6, "emp-kayla", loc_guid=DOS_GUID)  # other loc

    report = performance_for_date("uno", D_ISO, client=FakeToast())
    rows = _by_server(report)
    assert rows["emp-kayla"]["covers_seated"] == 6  # 4 + 2, dos excluded
    assert rows["emp-kayla"]["tables_planned"] == 2
    assert rows["emp-kayla"]["server_name"] == "Kayla Gomez"
    assert rows["emp-kayla"]["toast_checks"] == 0
    assert rows["emp-kayla"]["toast_net_sales"] == 0.0
    assert set(rows) == {"emp-kayla"}


# ---------------------------------------------------------------------------
# Section sales aggregation by table guid
# ---------------------------------------------------------------------------

def test_section_sales_aggregation_by_table_guid(perf_db):
    db = perf_db
    s1 = _seed_section(db, "emp-kayla", ["tbl-1", "tbl-2"], color="#14B8A6")
    s2 = _seed_section(db, "emp-marcus", ["tbl-3"], color="#8B5CF6")

    fake = FakeToast(orders=[
        _order(table="tbl-1", server="emp-kayla",
               checks=[_check(40.0), _check(10.5)]),
        _order(table="tbl-2", server="emp-kayla", checks=[_check(20.0)]),
        _order(table="tbl-3", server="emp-marcus", checks=[_check(99.99)]),
        # voided order + voided/deleted checks are skipped entirely
        _order(table="tbl-1", server="emp-kayla", voided=True,
               checks=[_check(500.0)]),
        _order(table="tbl-2", server="emp-kayla",
               checks=[_check(300.0, voided=True), _check(200.0, deleted=True)]),
    ])
    report = performance_for_date("uno", D_ISO, client=fake)

    secs = {s["section_id"]: s for s in report["per_section"]}
    assert secs[s1.id]["table_guids"] == ["tbl-1", "tbl-2"]
    assert secs[s1.id]["toast_checks"] == 3
    assert secs[s1.id]["toast_net_sales"] == 70.5
    assert secs[s1.id]["color"] == "#14B8A6"
    assert secs[s2.id]["toast_checks"] == 1
    assert secs[s2.id]["toast_net_sales"] == 99.99

    rows = _by_server(report)
    assert rows["emp-kayla"]["toast_checks"] == 3
    assert rows["emp-kayla"]["toast_net_sales"] == 70.5
    assert rows["emp-marcus"]["toast_net_sales"] == 99.99
    # ordering: net sales desc
    assert report["per_server"][0]["server_employee_guid"] == "emp-marcus"
    assert report["unmatched"] == {"checks_without_table": 0,
                                   "checks_on_unsectioned_tables": 0}


# ---------------------------------------------------------------------------
# Planned vs actual: match / mismatch / null
# ---------------------------------------------------------------------------

def test_planned_vs_actual_match_mismatch_and_null(perf_db):
    db = perf_db
    _seed_section(db, "emp-kayla", ["tbl-1", "tbl-2", "tbl-3"])
    _seed_table_cfg(db, "tbl-1", "T1")
    _seed_table_cfg(db, "tbl-old", "Old 9", deleted=True)  # soft-deleted name

    fake = FakeToast(orders=[
        # tbl-1 served by the planned server -> match True
        _order(table="tbl-1", server="emp-kayla", checks=[_check(10.0)]),
        # tbl-2 served only by other servers -> match False, all actuals kept
        _order(table="tbl-2", server="emp-marcus", checks=[_check(15.0)]),
        _order(table="tbl-2", server="emp-zoe", checks=[_check(5.0)]),
        # tbl-3: planned, no checks -> match None (covered below)
        # tbl-old: checks on an unsectioned table -> planned None, match None
        _order(table="tbl-old", server="emp-marcus", checks=[_check(7.0)]),
    ])
    report = performance_for_date("uno", D_ISO, client=fake)
    rows = _by_table(report)

    assert rows["tbl-1"]["match"] is True
    assert rows["tbl-1"]["planned_server_guid"] == "emp-kayla"
    assert rows["tbl-1"]["actual_server_guids"] == ["emp-kayla"]
    assert rows["tbl-1"]["table_name"] == "T1"

    assert rows["tbl-2"]["match"] is False
    assert set(rows["tbl-2"]["actual_server_guids"]) == {"emp-marcus",
                                                         "emp-zoe"}

    # planned table with no checks: actual empty, match null
    assert rows["tbl-3"]["match"] is None
    assert rows["tbl-3"]["actual_server_guids"] == []
    assert rows["tbl-3"]["planned_server_guid"] == "emp-kayla"

    # unplanned table with checks: planned None, match null, name from the
    # soft-deleted config row
    assert rows["tbl-old"]["planned_server_guid"] is None
    assert rows["tbl-old"]["match"] is None
    assert rows["tbl-old"]["table_name"] == "Old 9"
    assert report["unmatched"]["checks_on_unsectioned_tables"] == 1

    # unknown table guid (no config row): display falls back to guid prefix
    assert rows["tbl-2"]["table_name"] == "tbl-2"[:8]


def test_planned_table_with_serverless_checks_is_null(perf_db):
    db = perf_db
    _seed_section(db, "emp-kayla", ["tbl-1"])
    fake = FakeToast(orders=[
        _order(table="tbl-1", checks=[_check(12.0)]),  # no server anywhere
    ])
    report = performance_for_date("uno", D_ISO, client=fake)
    row = _by_table(report)["tbl-1"]
    assert row["actual_server_guids"] == []
    assert row["match"] is None  # actual unknown, not a mismatch
    # the sales still land on the table/section join
    sec = report["per_section"][0]
    assert sec["toast_net_sales"] == 12.0 and sec["toast_checks"] == 1


# ---------------------------------------------------------------------------
# Unmatched buckets
# ---------------------------------------------------------------------------

def test_unmatched_buckets(perf_db):
    db = perf_db
    _seed_section(db, "emp-kayla", ["tbl-1"])
    fake = FakeToast(orders=[
        # two checks with no table at any level (takeout-style)
        _order(server="emp-kayla", checks=[_check(8.0), _check(9.0)]),
        # check on a table outside every section
        _order(table="tbl-x", server="emp-marcus", checks=[_check(11.0)]),
        # sectioned table -> matched, not in either bucket
        _order(table="tbl-1", server="emp-kayla", checks=[_check(22.0)]),
    ])
    report = performance_for_date("uno", D_ISO, client=fake)
    assert report["unmatched"] == {
        "checks_without_table": 2,
        "checks_on_unsectioned_tables": 1,
    }
    # tableless checks still count toward the server's toast totals
    assert _by_server(report)["emp-kayla"]["toast_checks"] == 3
    assert _by_server(report)["emp-kayla"]["toast_net_sales"] == 39.0


# ---------------------------------------------------------------------------
# Date boundary: UTC evening -> Chicago business date
# ---------------------------------------------------------------------------

def test_date_boundary_utc_late_evening(perf_db):
    db = perf_db
    # 2026-06-12 03:30 UTC = 2026-06-11 22:30 America/Chicago (CDT):
    # business date 2026-06-11, not 06-12 (same case as the routes test).
    _seed_seating(db, "tbl-night", 2, "emp-kayla",
                  seated_at=datetime(2026, 6, 12, 3, 30, 0),
                  cleared_at=datetime(2026, 6, 12, 4, 15, 0))

    fake = FakeToast()
    on_day = performance_for_date("uno", "2026-06-11", client=fake)
    assert _by_server(on_day)["emp-kayla"]["covers_seated"] == 2
    assert fake.order_calls[-1][2] == "20260611"

    next_day = performance_for_date("uno", "2026-06-12", client=fake)
    assert next_day["per_server"] == []
    assert fake.order_calls[-1][2] == "20260612"


# ---------------------------------------------------------------------------
# Tolerant parsing of weird order payloads
# ---------------------------------------------------------------------------

def test_tolerant_parsing_of_malformed_orders(perf_db):
    db = perf_db
    _seed_section(db, "emp-kayla", ["tbl-1"])
    fake = FakeToast(orders=[
        None,                              # junk entries
        "not-a-dict",
        {"checks": None},                  # checks not a list
        {"checks": ["junk", None]},        # junk checks
        _order(table="tbl-1", server="emp-kayla",
               checks=[_check(None)]),     # amount None -> 0.0
        _order(checks=[_check("oops")]),   # amount unparseable -> 0.0
        # check-level table/server override the order level
        _order(table="tbl-x", server="emp-marcus",
               checks=[_check(30.0, table="tbl-1", server="emp-kayla")]),
        # table ref present but guid empty -> treated as missing
        {"table": {"guid": ""}, "server": {"name": "no guid"},
         "checks": [_check(2.0)]},
    ], employees_raise=True)  # names unavailable -> degrade, never raise

    report = performance_for_date("uno", D_ISO, client=fake)
    rows = _by_server(report)
    # kayla: 0.0 (None amount) + 30.0 (check-level override)
    assert rows["emp-kayla"]["toast_checks"] == 2
    assert rows["emp-kayla"]["toast_net_sales"] == 30.0
    # name lookup degraded to guid prefix
    assert rows["emp-kayla"]["server_name"] == "emp-kayl"
    sec = report["per_section"][0]
    assert sec["toast_checks"] == 2 and sec["toast_net_sales"] == 30.0
    # the "oops"-amount check and the empty-guid-table check have no table
    assert report["unmatched"]["checks_without_table"] == 2
    assert report["unmatched"]["checks_on_unsectioned_tables"] == 0


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------

def test_cli_main(perf_db, monkeypatch, capsys):
    import app.floor_performance as fp

    monkeypatch.setattr(
        fp, "performance_for_date",
        lambda loc, d, client=None: {"date": d, "loc": loc, "ok": 1})
    assert main(["uno", "2026-06-10"]) == 0
    out = capsys.readouterr().out
    import json
    assert json.loads(out) == {"date": "2026-06-10", "loc": "uno", "ok": 1}

    assert main([]) == 2
    assert main(["uno"]) == 2

    # bad args surface as exit 2 with the usage line (real function)
    monkeypatch.undo()
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_COPPERFIELD", UNO_GUID)
    assert main(["tres", "2026-06-10"]) == 2
    err = capsys.readouterr().err
    assert "usage:" in err
