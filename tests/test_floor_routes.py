"""SA-2 self-tests for app/web/floor_routes.py (floor_contract sections 6+7).

Pattern follows tests/test_dashboard_access_routes.py: create_app() + forged
keypad sessions against the shared in-memory db_session (app.db.SessionLocal
monkeypatched). Since Gate 2, app/__init__.py registers floor_bp itself; the
fixture only registers it manually if a stripped app ever lacks it.
ToastClient is patched - no network.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("ALLOW_DEV_SECRET", "1")

import sys
import types
from datetime import datetime, timedelta

from app.floor_models import (
    FLOOR_PALETTE,
    FloorReservation,
    FloorSeating,
    FloorWaitlistEntry,
    ToastServiceArea,
    ToastTableCfg,
    ensure_floor_tables,
)
from app.models import Employee, EmployeePosition, Position, User, _local_today
from app.services.toast_client import ToastClient

UNO_GUID = "test-guid-uno"
DOS_GUID = "test-guid-dos"

FAKE_EMPLOYEES = [
    {"guid": "emp-kayla", "firstName": "Kayla", "lastName": "Gomez"},
    {"guid": "emp-marcus", "firstName": "Marcus", "lastName": "Lee"},
    {"guid": "emp-gone", "firstName": "Gone", "lastName": "Person", "deleted": True},
]

PALETTE_HEX = [c["hex"] for c in FLOOR_PALETTE]


@pytest.fixture
def floor_app(db_session, monkeypatch):
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_COPPERFIELD", UNO_GUID)
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_TOMBALL", DOS_GUID)

    from app import create_app
    from app import db as appdb

    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)

    # No network in tests: default Toast behavior is a working employee list
    # and a failing shifts pull (tests override per-case).
    monkeypatch.setattr(
        ToastClient, "fetch_employees",
        lambda self, location, guid, refresh=False: list(FAKE_EMPLOYEES))

    def _no_shifts(self, location, guid, start, end, refresh=False):
        raise RuntimeError("toast shifts unavailable in tests")

    monkeypatch.setattr(ToastClient, "fetch_shifts", _no_shifts)

    ensure_floor_tables(db_session.get_bind())

    from app.web.floor_routes import floor_bp

    flask_app = create_app()
    if "floor" not in flask_app.blueprints:  # pre-Gate-2 safety net
        flask_app.register_blueprint(floor_bp)
    flask_app.config["TESTING"] = True
    return flask_app, db_session


def _seed_user(db, *, uid: int, role: str, store_key: str | None = "copperfield",
               position: str | None = None):
    user = User(
        id=uid,
        full_name=f"{role} user {uid}",
        email=f"{role}{uid}@test.local",
        phone=f"555200{uid:04d}",
        passcode_hash="test-hash",
        permission_level=role,
        store_scope=store_key,
        active=True,
        first_login_done=True,
        session_version=1,
    )
    db.add(user)
    if position and store_key:
        emp = Employee(
            id=uid,
            full_name=f"{role} employee {uid}",
            phone=f"555300{uid:04d}",
            active=True,
            user_id=uid,
        )
        pos = Position(id=uid, name=position, store_key=None)
        db.add_all([emp, pos])
        db.flush()
        db.add(EmployeePosition(employee_id=emp.id, position_id=pos.id,
                                store_key=store_key))
    db.commit()
    return user


def _client_as(app, user: User, *, active_store: str = "copperfield",
               last_store_slug: str = "uno"):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
        sess["user_id"] = user.id
        sess["user_session_version"] = user.session_version
        sess["active_store"] = active_store
        sess["last_store_slug"] = last_store_slug
    return client


def _partner_client(app, db, uid: int = 9001):
    partner = _seed_user(db, uid=uid, role="partner", store_key=None)
    return _client_as(app, partner)


def _seed_table(db, guid: str, name: str, *, loc_guid: str = UNO_GUID,
                area: str | None = "area-1", deleted: bool = False):
    db.add(ToastTableCfg(guid=guid, location_guid=loc_guid, name=name,
                         service_area_guid=area, revenue_center_guid="rc-1",
                         deleted=deleted))
    db.commit()


# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------

def test_api_403_unauthenticated(floor_app):
    app, _db = floor_app
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["auth_ok"] = True  # past the site gate, but no dashboard user
    resp = client.get("/floor/api/floor?loc=uno")
    assert resp.status_code == 403
    assert resp.get_json() == {"ok": False, "error": "forbidden"}


def test_api_403_role_without_operations(floor_app):
    app, db = floor_app
    cook = _seed_user(db, uid=9101, role="cook", position="Cook")
    client = _client_as(app, cook)
    resp = client.get("/floor/api/floor?loc=uno")
    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False


def test_manager_routes_403_for_expo(floor_app):
    app, db = floor_app
    expo = _seed_user(db, uid=9102, role="expo", position="Expo")
    client = _client_as(app, expo)

    # expo IS host-stand level: plain operations reads work
    assert client.get("/floor/api/floor?loc=uno").status_code == 200

    assert client.put("/floor/api/layout?loc=uno",
                      json={"tables": []}).status_code == 403
    assert client.put("/floor/api/fixtures?loc=uno",
                      json={"fixtures": []}).status_code == 403
    assert client.post("/floor/api/sections?loc=uno",
                       json={"sections": []}).status_code == 403
    assert client.post("/floor/api/sync?loc=uno").status_code == 403
    # page route: map tab is manager-only
    assert client.get("/floor/uno/sections?tab=map").status_code == 403


def test_unknown_loc_400(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    resp = client.get("/floor/api/floor?loc=nope")
    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": "unknown_loc"}
    resp = client.post("/floor/api/seat",
                       json={"loc": "tres", "table_guid": "t1", "party_size": 2})
    assert resp.status_code == 400
    resp = client.get("/floor/api/floor")  # loc missing entirely
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 1+2+3: floor / layout / fixtures
# ---------------------------------------------------------------------------

def test_floor_layout_fixtures_roundtrip(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    _seed_table(db, "tbl-1", "1")
    _seed_table(db, "tbl-2", "2")
    _seed_table(db, "tbl-gone", "99", deleted=True)
    db.add(ToastServiceArea(guid="area-1", location_guid=UNO_GUID,
                            name="Dining Room"))
    db.commit()

    resp = client.get("/floor/api/floor?loc=uno")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["location"]["slug"] == "uno"
    assert data["location"]["key"] == "copperfield"
    assert data["location"]["guid"] == UNO_GUID
    assert data["tables"] == []
    assert {t["guid"] for t in data["unplaced"]} == {"tbl-1", "tbl-2"}
    assert data["service_areas"] == [{"guid": "area-1", "name": "Dining Room"}]

    resp = client.put("/floor/api/layout?loc=uno", json={"tables": [
        {"table_guid": "tbl-1", "x": 60, "y": 60, "w": 80, "h": 80,
         "shape": "circle", "rotation": 0},
    ]})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "count": 1}

    resp = client.put("/floor/api/fixtures?loc=uno", json={"fixtures": [
        {"type": "wall", "x": 20, "y": 20, "w": 620, "h": 12, "rotation": 0},
        {"type": "label", "x": 660, "y": 40, "w": 240, "h": 70,
         "rotation": 0, "label": "BAR"},
    ]})
    assert resp.status_code == 200
    assert resp.get_json()["count"] == 2

    data = client.get("/floor/api/floor?loc=uno").get_json()
    # soft-deleted table appears nowhere; placed/unplaced split is by layout
    assert {t["guid"] for t in data["tables"]} == {"tbl-1"}
    placed = data["tables"][0]
    assert (placed["x"], placed["y"], placed["shape"]) == (60, 60, "circle")
    assert {t["guid"] for t in data["unplaced"]} == {"tbl-2"}
    assert len(data["fixtures"]) == 2
    assert data["fixtures"][1]["label"] == "BAR"

    # validation
    assert client.put("/floor/api/layout?loc=uno", json={"tables": [
        {"table_guid": "tbl-1", "shape": "hexagon"}]}).status_code == 400
    assert client.put("/floor/api/fixtures?loc=uno", json={"fixtures": [
        {"type": "door"}]}).status_code == 400
    assert client.put("/floor/api/layout?loc=uno", json={}).status_code == 400


# ---------------------------------------------------------------------------
# 4+5: sections GET/POST, 409 exists + confirm overwrite, palette auto-assign
# ---------------------------------------------------------------------------

def test_sections_post_get_conflict_and_overwrite(floor_app):
    app, db = floor_app
    gm = _seed_user(db, uid=9103, role="gm", position="GM")
    client = _client_as(app, gm)
    d = _local_today().isoformat()

    resp = client.post("/floor/api/sections?loc=uno", json={
        "date": d,
        "sections": [
            {"server_employee_guid": "emp-kayla",
             "table_guids": ["tbl-1", "tbl-2", "tbl-1"]},
            {"server_employee_guid": "emp-marcus", "color": "#EC4899",
             "table_guids": ["tbl-3"]},
        ],
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True and data["date"] == d
    by_server = {s["server_employee_guid"]: s for s in data["sections"]}
    # auto-assign = first palette hex unused in this (loc, date)
    assert by_server["emp-kayla"]["color"] == PALETTE_HEX[0]
    assert by_server["emp-marcus"]["color"] == "#EC4899"
    assert by_server["emp-kayla"]["table_guids"] == ["tbl-1", "tbl-2"]  # deduped
    assert by_server["emp-kayla"]["server_name"] == "Kayla Gomez"
    assert by_server["emp-kayla"]["initials"] == "KG"

    # GET round-trips
    got = client.get(f"/floor/api/sections?loc=uno&date={d}").get_json()
    assert got["ok"] is True
    assert len(got["sections"]) == 2

    # second POST without confirm -> 409 exists
    resp = client.post("/floor/api/sections?loc=uno", json={
        "date": d,
        "sections": [{"server_employee_guid": "emp-kayla", "table_guids": []}],
    })
    assert resp.status_code == 409
    assert resp.get_json() == {"ok": False, "error": "exists", "exists": True}

    # confirm=true -> full replace
    resp = client.post("/floor/api/sections?loc=uno", json={
        "date": d, "confirm": True,
        "sections": [{"server_employee_guid": "emp-marcus",
                      "table_guids": ["tbl-9"]}],
    })
    assert resp.status_code == 200
    got = client.get(f"/floor/api/sections?loc=uno&date={d}").get_json()
    assert len(got["sections"]) == 1
    assert got["sections"][0]["server_employee_guid"] == "emp-marcus"

    # duplicate server in one payload -> 400
    resp = client.post("/floor/api/sections?loc=uno", json={
        "date": d, "confirm": True,
        "sections": [
            {"server_employee_guid": "emp-kayla", "table_guids": []},
            {"server_employee_guid": "emp-kayla", "table_guids": []},
        ],
    })
    assert resp.status_code == 400

    # empty shift (no sections yet on another date) reads back empty
    got = client.get("/floor/api/sections?loc=uno&date=2030-01-01").get_json()
    assert got["sections"] == []
    # bad date -> 400
    assert client.get(
        "/floor/api/sections?loc=uno&date=junk").status_code == 400


# ---------------------------------------------------------------------------
# 9+10+8: seat / clear lifecycle + occupied 409 + covers math
# ---------------------------------------------------------------------------

def test_seat_clear_lifecycle_and_covers(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    d = _local_today().isoformat()

    # today's section: kayla owns tbl-1 (server resolution path)
    assert client.post("/floor/api/sections?loc=uno", json={
        "date": d,
        "sections": [{"server_employee_guid": "emp-kayla",
                      "table_guids": ["tbl-1"]}],
    }).status_code == 200

    resp = client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-1", "party_size": 4})
    assert resp.status_code == 200
    seat = resp.get_json()
    assert seat["ok"] is True
    assert seat["seating"]["server_employee_guid"] == "emp-kayla"
    assert seat["seating"]["party_size"] == 4

    # seat over an open seating -> 409 occupied
    resp = client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-1", "party_size": 2})
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "occupied"

    # explicit server beats section lookup
    resp = client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-2", "party_size": 2,
        "server_employee_guid": "emp-kayla"})
    assert resp.status_code == 200

    # un-sectioned table with no explicit server -> NULL server
    resp = client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-3", "party_size": 3})
    assert resp.status_code == 200
    assert resp.get_json()["seating"]["server_employee_guid"] is None

    live = client.get("/floor/api/live?loc=uno").get_json()
    assert live["ok"] is True
    assert live["attention_minutes"] == 90
    assert len(live["open"]) == 3
    assert all(o["minutes"] >= 0 for o in live["open"])
    assert live["covers"]["emp-kayla"] == {"live": 6, "today": 6}

    # clear tbl-2: kayla live drops, today stays
    resp = client.post("/floor/api/clear", json={"loc": "uno",
                                                 "table_guid": "tbl-2"})
    assert resp.status_code == 200
    cleared = resp.get_json()
    assert cleared["ok"] is True and cleared["cleared_at"]
    live = client.get("/floor/api/live?loc=uno").get_json()
    assert live["covers"]["emp-kayla"] == {"live": 4, "today": 6}
    assert len(live["open"]) == 2

    # clear with no open seating -> 404
    resp = client.post("/floor/api/clear", json={"loc": "uno",
                                                 "table_guid": "tbl-2"})
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "no_open_seating"

    # party_size resolution failure + bounds
    assert client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-4"}).status_code == 400
    assert client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-4",
        "party_size": 0}).status_code == 400
    assert client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-4",
        "party_size": 31}).status_code == 400
    assert client.post("/floor/api/seat",
                       json={"loc": "uno"}).status_code == 400


def test_seat_links_reservation_and_waitlist(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    d = _local_today()

    # reservation -> seat carries its party size + links back
    resp = client.post("/floor/api/reservations", json={
        "loc": "uno", "guest_name": "Dana Whitfield", "party_size": 5,
        "reserved_for": f"{d.isoformat()}T18:30:00", "phone": "555-0142"})
    assert resp.status_code == 200
    res_id = resp.get_json()["reservation"]["id"]

    resp = client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-7", "reservation_id": res_id})
    assert resp.status_code == 200
    seat = resp.get_json()["seating"]
    assert seat["party_size"] == 5
    assert seat["reservation_id"] == res_id

    book = client.get(
        f"/floor/api/reservations?loc=uno&date={d.isoformat()}").get_json()
    row = next(r for r in book["reservations"] if r["id"] == res_id)
    assert row["status"] == "seated"
    assert row["seating_id"] == seat["seating_id"]

    # waitlist -> same carry + link-back
    resp = client.post("/floor/api/waitlist", json={
        "loc": "uno", "guest_name": "Tina Park", "party_size": 2,
        "quoted_minutes": 15})
    assert resp.status_code == 200
    wl_id = resp.get_json()["entry"]["id"]

    resp = client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-8", "waitlist_id": wl_id})
    assert resp.status_code == 200
    seat2 = resp.get_json()["seating"]
    assert seat2["party_size"] == 2
    assert seat2["waitlist_id"] == wl_id

    # seated entries leave the default waitlist view, show with include_done=1
    default_list = client.get("/floor/api/waitlist?loc=uno").get_json()
    assert all(w["id"] != wl_id for w in default_list["waitlist"])
    done_list = client.get(
        "/floor/api/waitlist?loc=uno&include_done=1").get_json()
    row = next(w for w in done_list["waitlist"] if w["id"] == wl_id)
    assert row["status"] == "seated"
    assert row["seating_id"] == seat2["seating_id"]

    # explicit party_size wins over the linked reservation
    resp = client.post("/floor/api/reservations", json={
        "loc": "uno", "guest_name": "Robert Chen", "party_size": 2,
        "reserved_for": f"{d.isoformat()}T19:00:00"})
    res2 = resp.get_json()["reservation"]["id"]
    resp = client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-9", "reservation_id": res2,
        "party_size": 3})
    assert resp.get_json()["seating"]["party_size"] == 3

    # unknown links -> 404
    assert client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-10",
        "reservation_id": 98765}).status_code == 404
    assert client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-10",
        "waitlist_id": 98765}).status_code == 404


# ---------------------------------------------------------------------------
# 11+12+13: reservations
# ---------------------------------------------------------------------------

def test_reservations_crud_and_validation(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    d = _local_today()

    # validation
    assert client.post("/floor/api/reservations", json={
        "loc": "uno", "party_size": 2,
        "reserved_for": f"{d.isoformat()}T18:00:00"}).status_code == 400
    assert client.post("/floor/api/reservations", json={
        "loc": "uno", "guest_name": "X", "party_size": 99,
        "reserved_for": f"{d.isoformat()}T18:00:00"}).status_code == 400
    assert client.post("/floor/api/reservations", json={
        "loc": "uno", "guest_name": "X", "party_size": 2,
        "reserved_for": "not-a-time"}).status_code == 400

    # create two, listed in reserved_for order
    r1 = client.post("/floor/api/reservations", json={
        "loc": "uno", "guest_name": "Late Guest", "party_size": 2,
        "reserved_for": f"{d.isoformat()}T20:00:00"}).get_json()["reservation"]
    r2 = client.post("/floor/api/reservations", json={
        "loc": "uno", "guest_name": "Early Guest", "party_size": 4,
        "reserved_for": f"{d.isoformat()}T17:00:00",
        "notes": "window seat"}).get_json()["reservation"]
    assert r1["status"] == "upcoming"

    book = client.get(f"/floor/api/reservations?loc=uno"
                      f"&date={d.isoformat()}").get_json()
    ids = [r["id"] for r in book["reservations"]]
    assert ids.index(r2["id"]) < ids.index(r1["id"])
    # serialization: ISO-8601 UTC Z
    assert book["reservations"][0]["reserved_for"].endswith("Z")
    assert book["date"] == d.isoformat()

    # default date is today
    book_default = client.get("/floor/api/reservations?loc=uno").get_json()
    assert book_default["date"] == d.isoformat()

    # PATCH whitelist + enum validation
    resp = client.patch(f"/floor/api/reservations/{r1['id']}",
                        json={"status": "confirmed", "party_size": 6,
                              "notes": "anniversary"})
    assert resp.status_code == 200
    patched = resp.get_json()["reservation"]
    assert patched["status"] == "confirmed"
    assert patched["party_size"] == 6
    assert patched["notes"] == "anniversary"

    assert client.patch(f"/floor/api/reservations/{r1['id']}",
                        json={"status": "bogus"}).status_code == 400
    assert client.patch(f"/floor/api/reservations/{r1['id']}",
                        json={"party_size": 0}).status_code == 400
    assert client.patch(f"/floor/api/reservations/{r1['id']}",
                        json={"guest_name": ""}).status_code == 400
    assert client.patch("/floor/api/reservations/424242",
                        json={"status": "confirmed"}).status_code == 404


# ---------------------------------------------------------------------------
# 14+15+16: waitlist
# ---------------------------------------------------------------------------

def test_waitlist_flow(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)

    resp = client.post("/floor/api/waitlist", json={
        "loc": "uno", "guest_name": "Sam Field", "party_size": 4,
        "phone": "555-0136", "quoted_minutes": 25})
    assert resp.status_code == 200
    entry = resp.get_json()["entry"]
    assert entry["status"] == "waiting"
    assert entry["quoted_minutes"] == 25
    assert entry["joined_at"].endswith("Z")
    wid = entry["id"]

    # validation
    assert client.post("/floor/api/waitlist", json={
        "loc": "uno", "party_size": 2}).status_code == 400
    assert client.post("/floor/api/waitlist", json={
        "loc": "uno", "guest_name": "X", "party_size": 2,
        "quoted_minutes": -5}).status_code == 400

    # manual 'notified' toggle - still in the default view
    resp = client.patch(f"/floor/api/waitlist/{wid}",
                        json={"status": "notified", "quoted_minutes": 10})
    assert resp.status_code == 200
    assert resp.get_json()["entry"]["status"] == "notified"
    default_list = client.get("/floor/api/waitlist?loc=uno").get_json()
    assert [w["id"] for w in default_list["waitlist"]] == [wid]

    # 'left' is terminal: hidden by default, shown with include_done=1
    assert client.patch(f"/floor/api/waitlist/{wid}",
                        json={"status": "left"}).status_code == 200
    assert client.get("/floor/api/waitlist?loc=uno").get_json()["waitlist"] == []
    done = client.get("/floor/api/waitlist?loc=uno&include_done=1").get_json()
    assert [w["id"] for w in done["waitlist"]] == [wid]

    assert client.patch(f"/floor/api/waitlist/{wid}",
                        json={"status": "vanished"}).status_code == 400
    assert client.patch(f"/floor/api/waitlist/{wid}",
                        json={"quoted_minutes": "soon"}).status_code == 400
    assert client.patch("/floor/api/waitlist/424242",
                        json={"status": "left"}).status_code == 404


# ---------------------------------------------------------------------------
# 17: history (terminal filters + names) and the business-date boundary
# ---------------------------------------------------------------------------

def test_history_buckets(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    d = _local_today()
    _seed_table(db, "tbl-h1", "H1")

    # seat + clear one table; leave another open
    assert client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-h1", "party_size": 2,
        "server_employee_guid": "emp-kayla"}).status_code == 200
    assert client.post("/floor/api/clear", json={
        "loc": "uno", "table_guid": "tbl-h1"}).status_code == 200
    assert client.post("/floor/api/seat", json={
        "loc": "uno", "table_guid": "tbl-h2",
        "party_size": 3}).status_code == 200

    # terminal + non-terminal reservations
    res = client.post("/floor/api/reservations", json={
        "loc": "uno", "guest_name": "Greg Olsen", "party_size": 3,
        "reserved_for": f"{d.isoformat()}T18:00:00"}).get_json()["reservation"]
    client.patch(f"/floor/api/reservations/{res['id']}",
                 json={"status": "no_show"})
    client.post("/floor/api/reservations", json={
        "loc": "uno", "guest_name": "Still Coming", "party_size": 2,
        "reserved_for": f"{d.isoformat()}T19:00:00"})

    # terminal + non-terminal waitlist
    wl = client.post("/floor/api/waitlist", json={
        "loc": "uno", "guest_name": "Leo Marsh",
        "party_size": 2}).get_json()["entry"]
    client.patch(f"/floor/api/waitlist/{wl['id']}", json={"status": "left"})
    client.post("/floor/api/waitlist", json={
        "loc": "uno", "guest_name": "Still Waiting", "party_size": 2})

    hist = client.get(
        f"/floor/api/history?loc=uno&date={d.isoformat()}").get_json()
    assert hist["ok"] is True
    assert len(hist["seatings"]) == 2
    by_table = {s["table_guid"]: s for s in hist["seatings"]}
    assert by_table["tbl-h1"]["cleared_at"] is not None
    assert by_table["tbl-h1"]["table_name"] == "H1"
    assert by_table["tbl-h1"]["server_name"] == "Kayla Gomez"
    assert by_table["tbl-h2"]["cleared_at"] is None

    assert [r["guest_name"] for r in hist["reservations"]] == ["Greg Olsen"]
    assert hist["reservations"][0]["status"] == "no_show"
    assert [w["guest_name"] for w in hist["waitlist"]] == ["Leo Marsh"]


def test_business_date_boundary_utc_late_evening(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    # 2026-06-12 03:30 UTC = 2026-06-11 22:30 America/Chicago (CDT, -5):
    # the seating belongs to business date 2026-06-11, not 06-12.
    db.add(FloorSeating(
        location_guid=UNO_GUID,
        table_guid="tbl-night",
        party_size=2,
        seated_at=datetime(2026, 6, 12, 3, 30, 0),
        seated_by="test",
        server_employee_guid_at_seat="emp-kayla",
        cleared_at=datetime(2026, 6, 12, 4, 15, 0),
    ))
    db.commit()

    on_day = client.get(
        "/floor/api/history?loc=uno&date=2026-06-11").get_json()
    assert [s["table_guid"] for s in on_day["seatings"]] == ["tbl-night"]
    next_day = client.get(
        "/floor/api/history?loc=uno&date=2026-06-12").get_json()
    assert next_day["seatings"] == []


# ---------------------------------------------------------------------------
# 6+7: employees + employees-on-shift
# ---------------------------------------------------------------------------

def test_employees_endpoint(floor_app, monkeypatch):
    app, db = floor_app
    client = _partner_client(app, db)
    resp = client.get("/floor/api/employees?loc=uno")
    assert resp.status_code == 200
    data = resp.get_json()
    # deleted Toast rows are excluded; initials per contract section 5
    assert data["employees"] == [
        {"employee_guid": "emp-kayla", "name": "Kayla Gomez", "initials": "KG"},
        {"employee_guid": "emp-marcus", "name": "Marcus Lee", "initials": "ML"},
    ]

    def _boom(self, location, guid, refresh=False):
        raise RuntimeError("toast down")

    monkeypatch.setattr(ToastClient, "fetch_employees", _boom)
    resp = client.get("/floor/api/employees?loc=uno")
    assert resp.status_code == 502
    assert resp.get_json()["error"] == "toast_unavailable"


def test_employees_on_shift_sources(floor_app, monkeypatch):
    app, db = floor_app
    client = _partner_client(app, db)

    # fixture default: fetch_shifts raises -> falls back to the employee list
    resp = client.get("/floor/api/employees-on-shift?loc=uno")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["source"] == "employees"
    assert [s["employee_guid"] for s in data["servers"]] == [
        "emp-kayla", "emp-marcus"]
    assert [s["color"] for s in data["servers"]] == PALETTE_HEX[:2]

    # real shifts -> source=shifts, shift order, deleted shifts skipped
    def _shifts(self, location, guid, start, end, refresh=False):
        return [
            {"employeeReference": {"guid": "emp-marcus"}},
            {"employeeReference": {"guid": "emp-kayla"}},
            {"employeeReference": {"guid": "emp-marcus"}},  # dedupe
            {"deleted": True, "employeeReference": {"guid": "emp-gone"}},
        ]

    monkeypatch.setattr(ToastClient, "fetch_shifts", _shifts)
    data = client.get("/floor/api/employees-on-shift?loc=uno").get_json()
    assert data["source"] == "shifts"
    assert [s["employee_guid"] for s in data["servers"]] == [
        "emp-marcus", "emp-kayla"]
    assert data["servers"][0]["name"] == "Marcus Lee"
    assert [s["color"] for s in data["servers"]] == PALETTE_HEX[:2]


# ---------------------------------------------------------------------------
# 18: sync (lazy import of SA-1's module)
# ---------------------------------------------------------------------------

def test_sync_happy_and_unavailable(floor_app, monkeypatch):
    app, db = floor_app
    client = _partner_client(app, db)

    fake = types.ModuleType("app.services.toast_config_sync")
    seen = {}

    def _sync_location(key):
        seen["key"] = key
        return {"tables_upserted": 2, "tables_soft_deleted": 0,
                "service_areas_upserted": 1, "service_areas_soft_deleted": 0,
                "source": "full"}

    fake.sync_location = _sync_location
    monkeypatch.setitem(sys.modules, "app.services.toast_config_sync", fake)
    resp = client.post("/floor/api/sync?loc=uno")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["tables_upserted"] == 2
    assert data["source"] == "full"
    assert seen["key"] == "copperfield"

    # module not importable -> 503 sync_unavailable
    monkeypatch.setitem(sys.modules, "app.services.toast_config_sync", None)
    resp = client.post("/floor/api/sync?loc=uno")
    assert resp.status_code == 503
    assert resp.get_json() == {"ok": False, "error": "sync_unavailable"}


# ---------------------------------------------------------------------------
# Page route (contract section 7) - render_template patched: templates are
# being built by SA-3/SA-4 in parallel and must not be required here.
# ---------------------------------------------------------------------------

@pytest.fixture
def page_render(monkeypatch):
    from app.web import floor_routes as fr

    calls = []

    def _fake_render(template_name, **ctx):
        calls.append((template_name, ctx))
        return "PAGE-OK"

    monkeypatch.setattr(fr, "render_template", _fake_render)
    return calls


def test_page_route_tabs_and_context(floor_app, page_render):
    app, db = floor_app
    client = _partner_client(app, db)

    resp = client.get("/floor/uno/sections")
    assert resp.status_code == 200
    template, ctx = page_render[-1]
    assert template == "sections_assign.html"
    assert set(ctx.keys()) == {
        "store_slug", "active_tab", "locations_json", "loc_default",
        "is_manager", "attention_minutes", "user_name",
    }
    assert ctx["store_slug"] == "uno"
    assert ctx["active_tab"] == "assign"
    assert ctx["loc_default"] == "uno"
    assert ctx["is_manager"] is True
    assert ctx["attention_minutes"] == 90
    assert ctx["user_name"]
    import json as _json
    locs = _json.loads(ctx["locations_json"])
    assert locs == [
        {"slug": "uno", "key": "copperfield", "label": "Copperfield"},
        {"slug": "dos", "key": "tomball", "label": "Tomball"},
    ]

    assert client.get("/floor/uno/sections?tab=host").status_code == 200
    assert page_render[-1][0] == "sections_host.html"
    assert client.get("/floor/uno/sections?tab=map").status_code == 200
    assert page_render[-1][0] == "sections_map.html"

    # partner/corporate slugs resolve; junk slug 404s; junk tab -> assign
    assert client.get("/floor/partner/sections").status_code == 200
    assert page_render[-1][1]["store_slug"] == "partner"
    assert client.get("/floor/banana/sections").status_code == 404
    assert client.get("/floor/uno/sections?tab=junk").status_code == 200
    assert page_render[-1][1]["active_tab"] == "assign"


def test_page_route_single_store_manager_locations(floor_app, page_render):
    app, db = floor_app
    gm = _seed_user(db, uid=9104, role="gm", store_key="tomball",
                    position="GM")
    client = _client_as(app, gm, active_store="tomball",
                        last_store_slug="dos")
    resp = client.get("/floor/dos/sections")
    assert resp.status_code == 200
    template, ctx = page_render[-1]
    import json as _json
    locs = _json.loads(ctx["locations_json"])
    assert locs == [{"slug": "dos", "key": "tomball", "label": "Tomball"}]
    assert ctx["loc_default"] == "dos"
    assert ctx["is_manager"] is True


def test_page_route_403_without_user(floor_app, page_render):
    app, _db = floor_app
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
    resp = client.get("/floor/uno/sections")
    assert resp.status_code == 403
    assert page_render == []


# ---------------------------------------------------------------------------
# Gate 4 (ck): lazy no-show flag, duplicate-guest guard, reservation_badge,
# history days=N buckets (contract section 12)
# ---------------------------------------------------------------------------

def _seed_reservation(db, guest, *, minutes=None, reserved_at=None,
                      status="upcoming", phone="", loc_guid=UNO_GUID):
    """Direct row insert so reserved_for can sit anywhere relative to now."""
    if reserved_at is None:
        reserved_at = (datetime.utcnow().replace(microsecond=0)
                       + timedelta(minutes=minutes or 0))
    r = FloorReservation(
        location_guid=loc_guid,
        guest_name=guest,
        phone=phone,
        party_size=2,
        reserved_for=reserved_at,
        status=status,
        notes="",
        created_by="test",
        created_at=datetime.utcnow(),
    )
    db.add(r)
    db.commit()
    return r


def _status(db, rid: int) -> str:
    db.expire_all()
    return db.get(FloorReservation, rid).status


def _business_date_of(dt_utc):
    """Which business date a naive-UTC datetime belongs to (deterministic
    regardless of when in the day the test runs)."""
    from app.web.floor_routes import _utc_window_for_business_date
    base = _local_today()
    for cand in (base, base - timedelta(days=1), base + timedelta(days=1)):
        s, e = _utc_window_for_business_date(cand)
        if s <= dt_utc < e:
            return cand
    raise AssertionError(f"no business date found for {dt_utc}")


def test_noshow_flag_on_reservations_get(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    # default grace = 20: -22 is just past the boundary, -18 just inside
    overdue = _seed_reservation(db, "Overdue Upcoming", minutes=-22)
    conf_over = _seed_reservation(db, "Overdue Confirmed", minutes=-40,
                                  status="confirmed")
    in_grace = _seed_reservation(db, "Inside Grace", minutes=-18)
    future = _seed_reservation(db, "Future Guest", minutes=90)
    arrived = _seed_reservation(db, "Arrived Guest", minutes=-180,
                                status="arrived")
    seated = _seed_reservation(db, "Seated Guest", minutes=-180,
                               status="seated")
    cancelled = _seed_reservation(db, "Cancelled Guest", minutes=-180,
                                  status="cancelled")

    d = _business_date_of(overdue.reserved_for)
    book = client.get(
        f"/floor/api/reservations?loc=uno&date={d.isoformat()}").get_json()
    by_name = {r["guest_name"]: r["status"] for r in book["reservations"]}
    # the read itself serves the flipped status
    assert by_name["Overdue Upcoming"] == "no_show"

    # persisted on read: upcoming/confirmed past grace flip, nothing else
    assert _status(db, overdue.id) == "no_show"
    assert _status(db, conf_over.id) == "no_show"
    assert _status(db, in_grace.id) == "upcoming"
    assert _status(db, future.id) == "upcoming"
    assert _status(db, arrived.id) == "arrived"
    assert _status(db, seated.id) == "seated"
    assert _status(db, cancelled.id) == "cancelled"

    # idempotent: a second read changes nothing
    client.get(f"/floor/api/reservations?loc=uno&date={d.isoformat()}")
    assert _status(db, overdue.id) == "no_show"
    assert _status(db, in_grace.id) == "upcoming"


def test_noshow_grace_env_boundary(floor_app, monkeypatch):
    app, db = floor_app
    client = _partner_client(app, db)
    monkeypatch.setenv("FLOOR_NOSHOW_GRACE_MINUTES", "60")
    inside = _seed_reservation(db, "Fifty Nine", minutes=-59)
    past = _seed_reservation(db, "Sixty One", minutes=-61)
    assert client.get("/floor/api/reservations?loc=uno").status_code == 200
    assert _status(db, inside.id) == "upcoming"
    assert _status(db, past.id) == "no_show"


def test_noshow_flag_on_history_get(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    overdue = _seed_reservation(db, "Hist Overdue", minutes=-30)
    arrived = _seed_reservation(db, "Hist Arrived", minutes=-30,
                                status="arrived")
    d = _business_date_of(overdue.reserved_for)
    hist = client.get(
        f"/floor/api/history?loc=uno&date={d.isoformat()}").get_json()
    # flipped on this very read -> already in the terminal bucket
    assert {r["guest_name"]: r["status"] for r in hist["reservations"]} == {
        "Hist Overdue": "no_show"}
    assert _status(db, overdue.id) == "no_show"
    assert _status(db, arrived.id) == "arrived"


def test_noshow_flag_on_live_get_and_reservation_badge(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    from app.web.floor_routes import _utc_window_for_business_date
    now = datetime.utcnow().replace(microsecond=0)
    start, end = _utc_window_for_business_date(_local_today())
    future_today = now + (end - now) / 2   # today's window, in the future
    past_today = start + (now - start) / 2  # today's window, already past

    overdue = _seed_reservation(db, "Live Overdue", minutes=-30)
    _seed_reservation(db, "Live Upcoming", reserved_at=future_today)
    _seed_reservation(db, "Live Confirmed", reserved_at=future_today,
                      status="confirmed")
    _seed_reservation(db, "Live Arrived", reserved_at=past_today,
                      status="arrived")
    _seed_reservation(db, "Live Seated", reserved_at=past_today,
                      status="seated")
    _seed_reservation(db, "Live Cancelled", reserved_at=future_today,
                      status="cancelled")
    _seed_reservation(db, "Live Tomorrow",
                      reserved_at=end + timedelta(hours=2))
    _seed_reservation(db, "Other Store", reserved_at=future_today,
                      loc_guid=DOS_GUID)

    live = client.get("/floor/api/live?loc=uno").get_json()
    assert live["ok"] is True
    # the live read flips the overdue booking...
    assert _status(db, overdue.id) == "no_show"
    # ...and the badge counts only TODAY's upcoming/confirmed/arrived at uno
    assert live["reservation_badge"] == 3
    # next poll: flag is idempotent, badge stable
    again = client.get("/floor/api/live?loc=uno").get_json()
    assert again["reservation_badge"] == 3


def test_reservation_duplicate_guard(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    d = _local_today().isoformat()
    base = {"loc": "uno", "guest_name": "Pat Original", "party_size": 2,
            "reserved_for": f"{d}T18:00:00", "phone": "555-777-0001"}
    assert client.post("/floor/api/reservations", json=base).status_code == 200

    # same loc + phone within +/-90 min -> 409 duplicate envelope
    dup = dict(base, guest_name="Pat Again",
               reserved_for=f"{d}T19:00:00")
    resp = client.post("/floor/api/reservations", json=dup)
    assert resp.status_code == 409
    assert resp.get_json() == {"ok": False, "error": "duplicate",
                               "duplicate": True}

    # confirm:true overrides the guard
    resp = client.post("/floor/api/reservations", json=dict(dup, confirm=True))
    assert resp.status_code == 200
    assert resp.get_json()["reservation"]["guest_name"] == "Pat Again"

    # different phone in the same slot passes
    assert client.post("/floor/api/reservations", json=dict(
        base, guest_name="Someone Else",
        phone="555-777-0002")).status_code == 200

    # same phone outside the window passes (21:00 is 120+ min from both rows)
    assert client.post("/floor/api/reservations", json=dict(
        base, guest_name="Pat Later",
        reserved_for=f"{d}T21:00:00")).status_code == 200

    # exactly 90 minutes apart is still "within" -> 409
    p3 = {"loc": "uno", "guest_name": "Boundary One", "party_size": 2,
          "reserved_for": f"{d}T10:00:00", "phone": "555-777-0003"}
    assert client.post("/floor/api/reservations", json=p3).status_code == 200
    assert client.post("/floor/api/reservations", json=dict(
        p3, guest_name="Boundary Two",
        reserved_for=f"{d}T11:30:00")).status_code == 409

    # empty phone is never guarded
    walkin = {"loc": "uno", "guest_name": "No Phone", "party_size": 2,
              "reserved_for": f"{d}T12:00:00", "phone": ""}
    assert client.post("/floor/api/reservations", json=walkin).status_code == 200
    assert client.post("/floor/api/reservations", json=dict(
        walkin, guest_name="No Phone Two")).status_code == 200

    # cancelled rows do not trigger the guard
    c1 = client.post("/floor/api/reservations", json={
        "loc": "uno", "guest_name": "Cancel Me", "party_size": 2,
        "reserved_for": f"{d}T08:00:00", "phone": "555-777-0004"})
    assert c1.status_code == 200
    cid = c1.get_json()["reservation"]["id"]
    assert client.patch(f"/floor/api/reservations/{cid}",
                        json={"status": "cancelled"}).status_code == 200
    assert client.post("/floor/api/reservations", json={
        "loc": "uno", "guest_name": "Cancel Replacement", "party_size": 2,
        "reserved_for": f"{d}T08:30:00",
        "phone": "555-777-0004"}).status_code == 200


def test_history_days_buckets_clamp_and_legacy_shape(floor_app):
    app, db = floor_app
    client = _partner_client(app, db)
    from app.web.floor_routes import _utc_window_for_business_date
    d = _local_today()

    # one seating + one terminal reservation on each of 3 business days
    for off in range(3):
        day = d - timedelta(days=off)
        s, e = _utc_window_for_business_date(day)
        mid = s + (e - s) / 2
        db.add(FloorSeating(
            location_guid=UNO_GUID, table_guid=f"tbl-d{off}",
            party_size=2 + off, seated_at=mid, seated_by="test",
            cleared_at=mid + timedelta(hours=1)))
        db.add(FloorReservation(
            location_guid=UNO_GUID, guest_name=f"Guest D{off}", phone="",
            party_size=2, reserved_for=mid, status="cancelled", notes="",
            created_by="test", created_at=mid))
    # one terminal waitlist row on day 1 only
    s1, e1 = _utc_window_for_business_date(d - timedelta(days=1))
    db.add(FloorWaitlistEntry(
        location_guid=UNO_GUID, guest_name="WL D1", phone="", party_size=2,
        quoted_minutes=10, joined_at=s1 + (e1 - s1) / 2, status="left"))
    db.commit()

    # days param absent -> UNCHANGED legacy single-day shape
    legacy = client.get(
        f"/floor/api/history?loc=uno&date={d.isoformat()}").get_json()
    assert legacy["ok"] is True
    assert "days" not in legacy
    assert legacy["date"] == d.isoformat()
    assert [s["table_guid"] for s in legacy["seatings"]] == ["tbl-d0"]
    assert [r["guest_name"] for r in legacy["reservations"]] == ["Guest D0"]

    # days=1 explicitly also keeps the legacy shape
    one = client.get(
        f"/floor/api/history?loc=uno&date={d.isoformat()}&days=1").get_json()
    assert "days" not in one and one["date"] == d.isoformat()

    # days=3 -> per-day buckets, anchor date first (most recent first)
    multi = client.get(
        f"/floor/api/history?loc=uno&date={d.isoformat()}&days=3").get_json()
    assert multi["ok"] is True
    assert "date" not in multi and "seatings" not in multi
    assert [b["date"] for b in multi["days"]] == [
        (d - timedelta(days=off)).isoformat() for off in range(3)]
    for off, bucket in enumerate(multi["days"]):
        assert [s["table_guid"] for s in bucket["seatings"]] == [f"tbl-d{off}"]
        assert [r["guest_name"] for r in bucket["reservations"]] == [
            f"Guest D{off}"]
    assert [w["guest_name"] for w in multi["days"][1]["waitlist"]] == ["WL D1"]
    assert multi["days"][0]["waitlist"] == []

    # max clamp: days=99 -> 30 buckets
    clamped = client.get(
        f"/floor/api/history?loc=uno&date={d.isoformat()}&days=99").get_json()
    assert len(clamped["days"]) == 30

    # days<=0 clamps to the single-day default; junk -> 400
    zero = client.get(
        f"/floor/api/history?loc=uno&date={d.isoformat()}&days=0").get_json()
    assert "days" not in zero
    neg = client.get(
        f"/floor/api/history?loc=uno&date={d.isoformat()}&days=-5").get_json()
    assert "days" not in neg
    assert client.get(
        "/floor/api/history?loc=uno&days=abc").status_code == 400
