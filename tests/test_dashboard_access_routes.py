from __future__ import annotations

import os

import pytest

os.environ.setdefault("ALLOW_DEV_SECRET", "1")

from app.models import Employee, EmployeePosition, Position, User


@pytest.fixture
def dashboard_app(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb
    from app.web import store_routes as store_mod

    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)

    def _get_db():
        yield db_session

    monkeypatch.setattr(store_mod, "get_db", _get_db)

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app, db_session


def _seed_actor(db, *, uid: int, role: str, position: str, store_key: str = "tomball"):
    user = User(
        id=uid,
        full_name=f"{role} user",
        email=f"{role}{uid}@test.local",
        phone=f"555000{uid:04d}",
        passcode_hash="test-hash",
        permission_level=role,
        store_scope=store_key,
        active=True,
        first_login_done=True,
        session_version=1,
    )
    emp = Employee(
        id=uid,
        full_name=f"{role} employee",
        phone=f"555100{uid:04d}",
        active=True,
        user_id=uid,
    )
    pos = Position(id=uid, name=position, store_key=None)
    db.add_all([user, emp, pos])
    db.flush()
    db.add(EmployeePosition(employee_id=emp.id, position_id=pos.id, store_key=store_key))
    db.commit()
    return user


def _client_as(app, user: User):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
        sess["user_id"] = user.id
        sess["user_session_version"] = user.session_version
        sess["active_store"] = "tomball"
    return client


def _tab_keys(html: str) -> set[str]:
    keys: set[str] = set()
    marker = 'data-tab="'
    start = 0
    while True:
        idx = html.find(marker, start)
        if idx == -1:
            return keys
        idx += len(marker)
        end = html.find('"', idx)
        keys.add(html[idx:end])
        start = end + 1


def test_expo_today_and_operations_are_limited_to_allowed_tabs(dashboard_app):
    flask_app, db = dashboard_app
    expo = _seed_actor(db, uid=101, role="expo", position="Expo")
    client = _client_as(flask_app, expo)

    today = client.get("/dos/today?tab=dashboard")
    assert today.status_code == 200
    today_tabs = _tab_keys(today.get_data(as_text=True))
    assert today_tabs == {"notifications"}

    assert client.get("/dos/").status_code == 403

    ops = client.get("/dos/operations?tab=team")
    assert ops.status_code == 200
    ops_tabs = _tab_keys(ops.get_data(as_text=True))
    assert ops_tabs == {"corp-order"}

    assert client.get("/dos/team").status_code == 403
    assert client.get("/dos/corporate-order").status_code == 200
    assert client.get("/dos/corporate-order/reports").status_code == 403


def test_km_gets_manager_and_full_operations_tabs(dashboard_app):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=102, role="km", position="KM")
    client = _client_as(flask_app, km)

    assert client.get("/dos/manager").status_code == 200

    today = client.get("/dos/today")
    assert today.status_code == 200
    assert {"dashboard", "notifications"}.issubset(_tab_keys(today.get_data(as_text=True)))

    ops = client.get("/dos/operations?tab=sales")
    assert ops.status_code == 200
    assert {"team", "corp-order", "sales", "labor", "performance"}.issubset(
        _tab_keys(ops.get_data(as_text=True))
    )


def test_cook_keeps_kitchen_but_not_manager_or_operations(dashboard_app):
    flask_app, db = dashboard_app
    cook = _seed_actor(db, uid=103, role="cook", position="Cook")
    client = _client_as(flask_app, cook)

    assert client.get("/dos/kitchen").status_code == 200
    assert client.get("/dos/recipes").status_code == 200
    assert client.get("/dos/manager").status_code == 403
    assert client.get("/dos/operations").status_code == 403


def test_store_scope_blocks_other_store_dashboard(dashboard_app):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=104, role="km", position="KM", store_key="tomball")
    client = _client_as(flask_app, km)

    resp = client.get("/uno/manager", follow_redirects=False)
    assert resp.status_code in {302, 403}
    if resp.status_code == 302:
        assert resp.headers["Location"].endswith("/dos/")
