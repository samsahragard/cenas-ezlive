from __future__ import annotations

import os

os.environ.setdefault("ALLOW_DEV_SECRET", "1")

import pytest
from werkzeug.security import generate_password_hash

from app.models import CenaToastIgnore, CenaToastLink, Employee, EmployeeStoreAssignment, Position, Signal, User


class _FakeToastClient:
    def __init__(self, employees_by_store):
        self.employees_by_store = employees_by_store

    def fetch_employees(self, store, guid):
        return list(self.employees_by_store.get(store, []))


@pytest.fixture
def link_app(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb
    from app.web import permissions as perm_mod
    from app.web import schedules_v2 as sv2_mod
    from app.web import schedules_v2_roster as roster_mod
    from app.web import store_routes as store_mod
    from app.web import toast_link_routes as toast_mod
    from app.services import toast_employee_profiles as profile_mod

    db_session.add(User(
        id=1,
        full_name="Sam Partner",
        email="sam@test.local",
        passcode_hash=generate_password_hash("12345"),
        permission_level="partner",
        store_scope=None,
        active=True,
        first_login_done=True,
        session_version=1,
    ))
    for name in ("Server", "Cook", "GM"):
        db_session.add(Position(name=name, store_key=None))
    db_session.commit()

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    sess = lambda: db_session
    for mod in (appdb, perm_mod, sv2_mod, roster_mod, store_mod, toast_mod, profile_mod):
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", sess, raising=False)

    fake_toast = _FakeToastClient({
        "tomball": [
            {"guid": "toast-a", "firstName": "Austin", "lastName": "Markham", "phoneNumber": "9362529997"},
            {"guid": "toast-b", "firstName": "Deylin", "lastName": "Garza", "phoneNumber": "8322668832"},
            {"guid": "toast-c", "firstName": "Different", "lastName": "Person", "phoneNumber": "8320001111"},
        ],
        "copperfield": [
            {"guid": "toast-x", "firstName": "Copper", "lastName": "Only", "phoneNumber": "2810002222"},
        ],
    })

    class FakeToastClass:
        @staticmethod
        def shared():
            return fake_toast

    monkeypatch.setattr(toast_mod, "ToastClient", FakeToastClass, raising=True)
    monkeypatch.setattr(profile_mod, "ToastClient", FakeToastClass, raising=True)
    monkeypatch.setattr(
        toast_mod,
        "restaurant_guids",
        lambda: {"tomball": "guid-tomball", "copperfield": "guid-copperfield"},
        raising=True,
    )
    monkeypatch.setattr(
        profile_mod,
        "restaurant_guids",
        lambda: {"tomball": "guid-tomball", "copperfield": "guid-copperfield"},
        raising=True,
    )

    return flask_app, db_session


def _client(flask_app):
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["partner_auth_ok"] = True
        sess["auth_ok"] = True
        sess["user_id"] = 1
        sess["user_session_version"] = 1
    return client


def _employee(db, emp_id, name, store):
    emp = Employee(id=emp_id, full_name=name, active=True, session_version=1)
    db.add(emp)
    db.add(EmployeeStoreAssignment(employee_id=emp_id, store_key=store))
    db.commit()
    return emp


def _ids(rows, key):
    return {str(row.get(key)) for row in rows}


def test_manual_link_returns_as_confirmed_and_is_removed_from_candidate_pools(link_app):
    flask_app, db = link_app
    client = _client(flask_app)
    _employee(db, 10, "Augustin Martinez", "tomball")
    _employee(db, 20, "Copper Person", "copperfield")

    before = client.get("/dos/schedules-v2/toast/match-suggestions")
    assert before.status_code == 200, before.get_data(as_text=True)
    body = before.get_json()
    assert "10" in _ids(body["unmatched_cena"], "emp_id")
    assert "toast-a" in _ids(body["unmatched_toast"], "toast_id")

    linked = client.post(
        "/dos/schedules-v2/toast/link",
        json={"cena_emp_id": 10, "toast_id": "toast-a", "toast_name": "Austin Martinez"},
    )
    assert linked.status_code == 200, linked.get_data(as_text=True)

    after = client.get("/dos/schedules-v2/toast/match-suggestions")
    assert after.status_code == 200, after.get_data(as_text=True)
    body = after.get_json()
    confirmed = body["confirmed_links"]
    assert [(row["cena_emp_id"], row["toast_id"]) for row in confirmed] == [(10, "toast-a")]
    assert "10" not in _ids(body["unmatched_cena"], "emp_id")
    assert "toast-a" not in _ids(body["unmatched_toast"], "toast_id")

    copper = client.get("/uno/schedules-v2/toast/match-suggestions")
    assert copper.status_code == 200, copper.get_data(as_text=True)
    copper_text = copper.get_data(as_text=True)
    assert "Augustin Martinez" not in copper_text
    assert "toast-a" not in copper_text


def test_relink_updates_employee_and_moves_duplicate_toast_link(link_app):
    flask_app, db = link_app
    client = _client(flask_app)
    _employee(db, 11, "Deylin Garcia Garcia", "tomball")
    _employee(db, 12, "Other Cenas Person", "tomball")

    first = client.post(
        "/dos/schedules-v2/toast/link",
        json={"cena_emp_id": 11, "toast_id": "toast-a", "toast_name": "Austin Martinez"},
    )
    assert first.status_code == 200, first.get_data(as_text=True)
    second = client.post(
        "/dos/schedules-v2/toast/link",
        json={"cena_emp_id": 11, "toast_id": "toast-b", "toast_name": "Deylin Garza"},
    )
    assert second.status_code == 200, second.get_data(as_text=True)
    row = db.query(CenaToastLink).filter_by(cena_employee_id=11, store_key="tomball").one()
    assert row.toast_id == "toast-b"

    moved = client.post(
        "/dos/schedules-v2/toast/link",
        json={"cena_emp_id": 12, "toast_id": "toast-b", "toast_name": "Deylin Garza"},
    )
    assert moved.status_code == 200, moved.get_data(as_text=True)
    rows = db.query(CenaToastLink).filter_by(store_key="tomball").all()
    assert [(r.cena_employee_id, r.toast_id) for r in rows] == [(12, "toast-b")]


def test_link_rejects_employee_not_assigned_to_url_store(link_app):
    flask_app, db = link_app
    client = _client(flask_app)
    _employee(db, 10, "Tomball Only", "tomball")

    resp = client.post(
        "/uno/schedules-v2/toast/link",
        json={"cena_emp_id": 10, "toast_id": "toast-x", "toast_name": "Copper Only"},
    )
    assert resp.status_code == 400
    assert "not active at this store" in resp.get_json()["error"]
    assert db.query(CenaToastLink).count() == 0


def test_ignore_hides_cenas_and_toast_only_records(link_app):
    flask_app, db = link_app
    client = _client(flask_app)
    _employee(db, 10, "No Match Person", "tomball")

    cena_ignored = client.post(
        "/dos/schedules-v2/toast/ignore",
        json={"source": "cena", "cena_emp_id": 10},
    )
    assert cena_ignored.status_code == 200, cena_ignored.get_data(as_text=True)
    toast_ignored = client.post(
        "/dos/schedules-v2/toast/ignore",
        json={"source": "toast", "toast_id": "toast-c", "display_name": "Different Person"},
    )
    assert toast_ignored.status_code == 200, toast_ignored.get_data(as_text=True)

    body = client.get("/dos/schedules-v2/toast/match-suggestions").get_json()
    assert "10" not in _ids(body["unmatched_cena"], "emp_id")
    assert "toast-c" not in _ids(body["unmatched_toast"], "toast_id")
    assert db.query(CenaToastIgnore).count() == 2


def test_toast_profile_reconcile_creates_and_links_toast_only_people(link_app):
    flask_app, db = link_app
    client = _client(flask_app)

    from app.services.toast_employee_profiles import reconcile_toast_employee_profiles

    summary = reconcile_toast_employee_profiles(only_store="tomball", db=db)
    assert summary["created"] == 3
    assert summary["linked"] == 3

    body = client.get("/dos/schedules-v2/toast/match-suggestions").get_json()
    assert _ids(body["confirmed_links"], "toast_id") == {"toast-a", "toast-b", "toast-c"}
    assert body["unmatched_toast"] == []
    assert db.query(Signal).filter_by(rule_name="labor.toast_employee_profile_created").count() == 3


def test_toast_profile_reconcile_reuses_existing_employee_by_phone(link_app):
    _flask_app, db = link_app
    emp = Employee(id=30, full_name="Austin Existing", phone="9362529997",
                   active=True, session_version=1)
    db.add(emp)
    db.add(EmployeeStoreAssignment(employee_id=30, store_key="tomball"))
    db.commit()

    from app.services.toast_employee_profiles import reconcile_toast_employee_profiles

    summary = reconcile_toast_employee_profiles(only_store="tomball", db=db)
    assert summary["reused"] == 1
    assert summary["created"] == 2

    row = db.query(CenaToastLink).filter_by(store_key="tomball", toast_id="toast-a").one()
    assert row.cena_employee_id == 30
    assert db.query(Employee).filter(Employee.phone == "9362529997").count() == 1


def test_toast_profile_reconcile_respects_ignored_toast_rows(link_app):
    _flask_app, db = link_app
    db.add(CenaToastIgnore(store_key="tomball", source="toast", source_id="toast-c",
                           display_name="Different Person"))
    db.commit()

    from app.services.toast_employee_profiles import reconcile_toast_employee_profiles

    summary = reconcile_toast_employee_profiles(only_store="tomball", db=db)
    assert summary["created"] == 2
    assert summary["linked"] == 2
    assert db.query(CenaToastLink).filter_by(store_key="tomball", toast_id="toast-c").count() == 0


def test_partner_session_can_nudge_toast_profile_reconcile_for_store(link_app):
    flask_app, db = link_app
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
        sess["partner_auth_ok"] = True

    resp = client.post("/uno/schedules-v2/toast/reconcile-profiles")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["store"] == "copperfield"
    assert body["profiles"]["created"] == 1
    assert db.query(CenaToastLink).filter_by(store_key="copperfield", toast_id="toast-x").count() == 1


def test_team_link_page_defaults_to_url_store_and_contains_cleanup_controls(link_app):
    flask_app, _db = link_app
    client = _client(flask_app)

    html = client.get("/uno/team?sub=link").get_data(as_text=True)
    assert 'var LINK_DEFAULT_STORE = "uno";' in html
    assert 'data-link-act="change-link"' in html
    assert 'data-link-act="ignore-toast"' in html
    assert 'data-link-act="delete-cena"' in html
