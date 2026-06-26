from __future__ import annotations

import os
import re
from datetime import date, datetime

import pytest

os.environ.setdefault("ALLOW_DEV_SECRET", "1")

from app.models import (
    Employee,
    EmployeePosition,
    FreshFoodOrder,
    FreshFoodOrderLine,
    Order,
    Position,
    User,
    VendorRecentOrder,
)


@pytest.fixture
def dashboard_app(db_session, monkeypatch):
    from app import create_app
    from app import db as appdb
    from app.web import driver_system as driver_mod
    from app.web import schedules_v2_roster as roster_mod
    from app.web import store_routes as store_mod

    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(driver_mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(roster_mod, "SessionLocal", lambda: db_session)

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


def _grant_corporate_order_scope(client, store_slug: str):
    with client.session_transaction() as sess:
        sess["corporate_order_scope"] = store_slug


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


def _attr_values(html: str, attr: str) -> list[str]:
    values: list[str] = []
    marker = f'{attr}="'
    start = 0
    while True:
        idx = html.find(marker, start)
        if idx == -1:
            return values
        idx += len(marker)
        end = html.find('"', idx)
        values.append(html[idx:end])
        start = end + 1


def _active_manager_group(html: str) -> str:
    match = re.search(r'class="mgd-tab mgd-group-tab active"[^>]*data-tab-group="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _active_manager_leaf(html: str) -> str | None:
    match = re.search(r'class="mgd-subtab mgd-leaf-tab active"[^>]*data-tab="([^"]+)"', html)
    return match.group(1) if match else None


def _manager_panel_class(html: str, key: str) -> str:
    match = re.search(
        rf'<div class="([^"]*)"[^>]*data-tab-panel="{re.escape(key)}"',
        html,
    )
    assert match is not None
    return match.group(1)


def _manager_frame_src(html: str, key: str) -> str:
    match = re.search(
        rf'<iframe class="mgd-embed-frame"[^>]*data-embed-frame="{re.escape(key)}"[^>]*data-src="([^"]+)"',
        html,
    )
    assert match is not None
    return match.group(1)


def _active_operations_group(html: str) -> str:
    match = re.search(r'class="opsd-tab opsd-group-tab active"[^>]*data-tab-group="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _active_operations_leaf(html: str) -> str | None:
    match = re.search(r'class="opsd-subtab opsd-leaf-tab active"[^>]*data-tab="([^"]+)"', html)
    return match.group(1) if match else None


def _operations_panel_class(html: str, key: str) -> str:
    match = re.search(
        rf'<div class="([^"]*)"[^>]*data-tab-panel="{re.escape(key)}"',
        html,
    )
    assert match is not None
    return match.group(1)


def _operations_frame_src(html: str, key: str) -> str:
    match = re.search(
        rf'<iframe class="opsd-embed-frame"[^>]*data-embed-frame="{re.escape(key)}"[^>]*data-src="([^"]+)"',
        html,
    )
    assert match is not None
    return match.group(1)


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
    ops_groups = _attr_values(ops.get_data(as_text=True), "data-tab-group")
    assert ops_groups == ["team", "corp-order"]

    assert client.get("/dos/team").status_code == 200
    assert client.get("/dos/schedules-v2/team-roster").status_code == 200
    assert client.get("/dos/corporate-order").status_code == 302
    assert client.get("/dos/corporate-order/reports").status_code == 302
    assert client.get("/dos/performance").status_code == 403


def test_corporate_order_renders_backend_catalog_for_store(dashboard_app, monkeypatch):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=121, role="km", position="KM")
    client = _client_as(flask_app, km)
    _grant_corporate_order_scope(client, "dos")

    from app.services import corporate_shop

    monkeypatch.setattr(corporate_shop, "is_configured", lambda: True)
    monkeypatch.setattr(corporate_shop, "ensure_catalog_seeded", lambda: {"added": 0})
    monkeypatch.setattr(
        corporate_shop,
        "list_products",
        lambda category=None: [{
            "id": 42,
            "name": "Bleach (6/case)",
            "in_stock": 15,
            "picture": "",
            "picture_url": "https://cenaskitchen.com/media/Bleach.webp",
            "category": "Cleaning Supplies",
            "sort_order": 10,
            "date_added": None,
        }],
    )
    monkeypatch.setattr(corporate_shop, "list_categories", lambda: ["Cleaning Supplies"])
    monkeypatch.setattr(corporate_shop, "list_orders", lambda *args, **kwargs: [])

    resp = client.get("/dos/corporate-order")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Bleach (6/case)" in html
    assert 'src="https://cenaskitchen.com/media/Bleach.webp"' in html
    assert 'name="qty_42"' in html
    assert "Departments" in html
    assert "corp-cat-pill active" in html
    assert 'href="/dos/corporate-order?category=Cleaning+Supplies"' in html
    assert "corporate_order_demo.html" not in html


def test_corporate_order_submit_maps_dos_to_tomball(dashboard_app, monkeypatch):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=122, role="km", position="KM")
    client = _client_as(flask_app, km)
    _grant_corporate_order_scope(client, "dos")

    from app.services import corporate_shop
    from app.web import corporate_order as corporate_order_mod

    submitted = {}

    monkeypatch.setattr(corporate_shop, "is_configured", lambda: True)

    def _place_order(store_key, items):
        submitted["store_key"] = store_key
        submitted["items"] = items
        return {
            "order_id": 9001,
            "submitted_at": None,
            "store_key": store_key,
            "store_label": "Tomball Kitchen",
            "items": [],
        }

    monkeypatch.setattr(corporate_shop, "place_order", _place_order)
    monkeypatch.setattr(corporate_order_mod, "_send_corporate_order_email", lambda order: (True, ""))

    resp = client.post("/dos/corporate-order/submit", data={"qty_42": "2"}, follow_redirects=False)
    assert resp.status_code == 302
    assert submitted == {"store_key": "tomball", "items": [(42, 2)]}


def test_corporate_order_public_pin_gate_opens_store_portal(dashboard_app, monkeypatch):
    flask_app, _db = dashboard_app
    client = flask_app.test_client()

    resp = client.get("/dos/corporate-order", follow_redirects=False)
    assert resp.status_code == 302
    assert "/corporate-order?target=tomball" in resp.headers["Location"]
    legacy = client.get("/partner/corporate-order", follow_redirects=False)
    assert legacy.status_code == 302
    assert "/corporate-order?target=corporate" in legacy.headers["Location"]

    bad = client.post(
        "/corporate-order/login",
        data={"scope": "tomball", "pin": "0000"},
    )
    assert bad.status_code == 401

    from app.services import corporate_shop

    monkeypatch.setattr(corporate_shop, "is_configured", lambda: True)
    monkeypatch.setattr(corporate_shop, "ensure_catalog_seeded", lambda: {"added": 0})
    monkeypatch.setattr(
        corporate_shop,
        "list_products",
        lambda category=None: [{
            "id": 42,
            "name": "Bleach (6/case)",
            "in_stock": 15,
            "picture": "",
            "category": "Cleaning Supplies",
            "sort_order": 10,
            "date_added": None,
        }],
    )
    monkeypatch.setattr(corporate_shop, "list_categories", lambda: ["Cleaning Supplies"])
    monkeypatch.setattr(corporate_shop, "list_orders", lambda *args, **kwargs: [])

    ok = client.post(
        "/corporate-order/login",
        data={"scope": "tomball", "pin": "8804"},
        follow_redirects=False,
    )
    assert ok.status_code == 302
    assert ok.headers["Location"].endswith("/dos/corporate-order")
    repeat = client.get("/corporate-order", follow_redirects=False)
    assert repeat.status_code == 302
    assert repeat.headers["Location"].endswith("/dos/corporate-order")
    switch = client.get("/corporate-order?switch=1")
    assert switch.status_code == 200
    assert "Corporate order login choices" in switch.get_data(as_text=True)

    page = client.get("/dos/corporate-order")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Bleach (6/case)" in html
    assert 'name="qty_42"' in html


def test_corporate_fulfillment_update_saves_actual_sent_counts(dashboard_app, monkeypatch):
    flask_app, db = dashboard_app
    corp = _seed_actor(db, uid=123, role="corporate", position="GM")
    client = _client_as(flask_app, corp)
    _grant_corporate_order_scope(client, "corporate")

    from app.services import corporate_shop

    saved = {}

    def _update(order_id, fulfilled_by_line, *, new_status=None):
        saved["order_id"] = order_id
        saved["fulfilled_by_line"] = fulfilled_by_line
        saved["new_status"] = new_status
        return True

    monkeypatch.setattr(corporate_shop, "update_order_fulfillment", _update)

    resp = client.post(
        "/corporate/corporate-order/admin/order/501/status",
        data={
            "status": "In Progress",
            "fulfilled_10": "4",
            "fulfilled_11": "0",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert saved == {
        "order_id": 501,
        "fulfilled_by_line": {10: 4, 11: 0},
        "new_status": "In Progress",
    }


def test_corporate_admin_page_renders_catalog_management_and_fulfillment(dashboard_app, monkeypatch):
    flask_app, db = dashboard_app
    corp = _seed_actor(db, uid=124, role="corporate", position="GM")
    client = _client_as(flask_app, corp)
    _grant_corporate_order_scope(client, "corporate")

    from app.services import corporate_shop

    monkeypatch.setattr(corporate_shop, "is_configured", lambda: True)
    monkeypatch.setattr(corporate_shop, "ensure_catalog_seeded", lambda: {"added": 0})
    monkeypatch.setattr(
        corporate_shop,
        "list_products",
        lambda category=None: [{
            "id": 42,
            "name": "Bleach (6/case)",
            "in_stock": 15,
            "picture": "",
            "category": "Cleaning Supplies",
            "sort_order": 10,
            "date_added": None,
        }],
    )
    monkeypatch.setattr(corporate_shop, "list_categories", lambda: ["Cleaning Supplies"])
    monkeypatch.setattr(
        corporate_shop,
        "list_orders",
        lambda *args, **kwargs: [{
            "id": 501,
            "submitted_at": None,
            "status": "Submitted",
            "customer_email": "store-tomball@cenaskitchen.com",
            "customer_username": "Tomball Kitchen",
            "store_key": "tomball",
            "lines": [{
                "id": 10,
                "name": "Bleach (6/case)",
                "category": "Cleaning Supplies",
                "quantity": 12,
                "fulfilled_quantity": 4,
                "remaining_quantity": 8,
            }],
            "total_quantity": 12,
            "total_fulfilled": 4,
        }],
    )

    resp = client.get("/corporate/corporate-order")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Add Catalog Item" in html
    assert 'action="/corporate/corporate-order/admin/product/add"' in html
    assert 'action="/corporate/corporate-order/admin/product/42/delete"' in html
    assert 'name="fulfilled_10"' in html
    assert 'value="4"' in html
    assert "ordered 12" in html


def test_corporate_admin_can_save_department_product_order(dashboard_app, monkeypatch):
    flask_app, db = dashboard_app
    corp = _seed_actor(db, uid=127, role="corporate", position="GM")
    client = _client_as(flask_app, corp)
    _grant_corporate_order_scope(client, "corporate")

    from app.services import corporate_shop

    saved = {}

    monkeypatch.setattr(corporate_shop, "is_configured", lambda: True)
    monkeypatch.setattr(corporate_shop, "ensure_catalog_seeded", lambda: {"added": 0})
    monkeypatch.setattr(
        corporate_shop,
        "list_products",
        lambda category=None: [{
            "id": 42,
            "name": "Bleach (6/case)",
            "in_stock": 15,
            "picture": "",
            "picture_url": "https://cenaskitchen.com/media/Bleach.webp",
            "category": category or "Cleaning Supplies",
            "sort_order": 10,
            "date_added": None,
        }],
    )
    monkeypatch.setattr(corporate_shop, "list_categories", lambda: ["BOH"])
    monkeypatch.setattr(corporate_shop, "list_orders", lambda *args, **kwargs: [])

    def _save(category, product_ids):
        saved["category"] = category
        saved["product_ids"] = product_ids
        return len(product_ids)

    monkeypatch.setattr(corporate_shop, "update_product_order", _save)

    page = client.get("/corporate/corporate-order?category=BOH")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Organize BOH" in html
    assert 'id="corpSortForm"' in html
    assert 'data-product-id="42"' in html

    resp = client.post(
        "/corporate/corporate-order/admin/products/order",
        data={"category": "BOH", "product_order": "42,99"},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(
        "/corporate/corporate-order?category=BOH"
    )
    assert saved == {"category": "BOH", "product_ids": [42, 99]}


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
    ops_html = ops.get_data(as_text=True)
    assert _attr_values(ops_html, "data-tab-group") == [
        "team",
        "corp-order",
        "analytics",
        "sections",
    ]
    assert _attr_values(ops_html, "data-subtabs") == ["analytics"]
    assert {"sales", "labor", "performance", "forecasts"}.issubset(_tab_keys(ops_html))


def test_operations_dashboard_groups_analytics_and_keeps_team_default(dashboard_app):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=110, role="km", position="KM")
    client = _client_as(flask_app, km)

    resp = client.get("/dos/operations")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert _attr_values(html, "data-tab-group") == [
        "team",
        "corp-order",
        "analytics",
        "sections",
    ]
    assert _attr_values(html, "data-tab-default")[:4] == [
        "team",
        "corp-order",
        "performance",
        "sections",
    ]
    assert _attr_values(html, "data-subtabs") == ["analytics"]
    assert _active_operations_group(html) == "team"
    assert _active_operations_leaf(html) is None
    assert "hidden" not in _operations_panel_class(html, "team")
    assert _operations_frame_src(html, "team") == "/dos/team"


@pytest.mark.parametrize("path", ["/dos/operations?tab=corp-order", "/partner/operations?tab=corp-order"])
def test_operations_corp_order_tab_embeds_public_pin_portal(dashboard_app, path):
    flask_app, db = dashboard_app
    user = _seed_actor(
        db,
        uid=125 if path.startswith("/dos") else 126,
        role="km" if path.startswith("/dos") else "partner",
        position="KM" if path.startswith("/dos") else "GM",
    )
    client = _client_as(flask_app, user)
    if path.startswith("/partner"):
        with client.session_transaction() as sess:
            sess["partner_auth_ok"] = True

    resp = client.get(path)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert _active_operations_group(html) == "corp-order"
    assert "hidden" not in _operations_panel_class(html, "corp-order")
    assert _operations_frame_src(html, "corp-order") == "/corporate-order"


@pytest.mark.parametrize(
    ("tab", "src"),
    [
        ("performance", "/dos/reports/server-performance"),
        ("sales", "/dos/reports/sales"),
        ("labor", "/dos/reports/labor"),
    ],
)
def test_operations_dashboard_analytics_deep_links_select_child(dashboard_app, tab, src):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=111, role="km", position="KM")
    client = _client_as(flask_app, km)

    resp = client.get(f"/dos/operations?tab={tab}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert _active_operations_group(html) == "analytics"
    assert _active_operations_leaf(html) == tab
    assert "hidden" not in _operations_panel_class(html, tab)
    assert _operations_frame_src(html, tab) == src


def test_operations_dashboard_forecasts_deep_link_stays_under_analytics(dashboard_app):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=112, role="km", position="KM")
    client = _client_as(flask_app, km)

    resp = client.get("/dos/operations?tab=forecasts")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert _active_operations_group(html) == "analytics"
    assert _active_operations_leaf(html) == "forecasts"
    assert "hidden" not in _operations_panel_class(html, "forecasts")
    assert "Forecasts isn't live yet" in html


def test_operations_schedule_reports_deep_link_opens_team_subtab(dashboard_app):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=113, role="km", position="KM")
    client = _client_as(flask_app, km)

    resp = client.get("/dos/operations?tab=schedule-reports")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert _active_operations_group(html) == "team"
    assert "hidden" not in _operations_panel_class(html, "team")
    assert _operations_frame_src(html, "team") == "/dos/team?sub=schedule-reports"


def test_manager_dashboard_groups_existing_pages_without_dropping_sports(dashboard_app):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=107, role="km", position="KM")
    client = _client_as(flask_app, km)

    resp = client.get("/dos/manager")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert _attr_values(html, "data-tab-group") == [
        "daily",
        "hr",
        "onboarding",
        "maintenance",
        "sports",
    ]
    assert _attr_values(html, "data-tab-default")[:5] == [
        "log",
        "counseling",
        "interview",
        "maintenance",
        "sports",
    ]
    assert _attr_values(html, "data-subtabs") == ["daily", "hr", "onboarding"]
    assert _active_manager_group(html) == "daily"
    assert _active_manager_leaf(html) == "log"
    assert "hidden" not in _manager_panel_class(html, "log")
    assert _manager_frame_src(html, "sports") == "/dos/sports"


@pytest.mark.parametrize(
    ("tab", "group", "leaf", "src"),
    [
        ("attendance", "daily", "attendance", "/dos/manager/attendance"),
        ("incidents", "hr", "incidents", "/dos/manager/incident-reports"),
        ("training", "onboarding", "training", "/dos/manager/training"),
        ("maintenance", "maintenance", None, "/dos/manager/maintenance"),
        ("sports", "sports", None, "/dos/sports"),
    ],
)
def test_manager_dashboard_leaf_deep_links_select_the_right_group(
    dashboard_app, tab, group, leaf, src
):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=108, role="km", position="KM")
    client = _client_as(flask_app, km)

    resp = client.get(f"/dos/manager?tab={tab}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert _active_manager_group(html) == group
    assert _active_manager_leaf(html) == leaf
    assert "hidden" not in _manager_panel_class(html, tab)
    assert _manager_frame_src(html, tab) == src
    assert ('id="mgdSubtabBank"') in html


def test_manager_dashboard_invalid_deep_link_falls_back_to_daily_log(dashboard_app):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=109, role="km", position="KM")
    client = _client_as(flask_app, km)

    resp = client.get("/dos/manager?tab=not-real")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert _active_manager_group(html) == "daily"
    assert _active_manager_leaf(html) == "log"
    assert "hidden" not in _manager_panel_class(html, "log")


def test_cook_keeps_kitchen_but_not_manager_or_operations(dashboard_app):
    flask_app, db = dashboard_app
    cook = _seed_actor(db, uid=103, role="cook", position="Cook")
    client = _client_as(flask_app, cook)

    assert client.get("/dos/kitchen").status_code == 200
    assert client.get("/dos/recipes").status_code == 200
    assert client.get("/dos/manager").status_code == 403
    assert client.get("/dos/operations").status_code == 403


def test_fresh_food_completion_ignores_blank_order_rows(dashboard_app):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=118, role="km", position="KM")
    client = _client_as(flask_app, km)

    order = FreshFoodOrder(
        id=13,
        order_date=date(2026, 6, 26),
        placed_by_user_id=km.id,
        placed_by_name=km.full_name,
        status="active",
    )
    ordered_line = FreshFoodOrderLine(
        id=1301,
        order_id=13,
        item_slug="beef-fajita",
        item_category="MEAT",
        inv_qty=5,
        or_qty=9,
    )
    blank_or_line = FreshFoodOrderLine(
        id=1302,
        order_id=13,
        item_slug="chipotle-cream",
        item_category="SAUCES",
        inv_qty=4,
        or_qty=None,
    )
    zero_or_line = FreshFoodOrderLine(
        id=1303,
        order_id=13,
        item_slug="cochinita",
        item_category="MEAT",
        inv_qty=3,
        or_qty=0,
    )
    db.add_all([order, ordered_line, blank_or_line, zero_or_line])
    db.commit()

    resp = client.post(
        "/dos/fresh-food/recent-orders/13/fulfill",
        json={
            "fulfilled_by_name": "Janeth Arvizu Animas",
            "sent_date": "2026-06-26",
            "sent_lines": {"1301": "9"},
        },
    )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "completed"
    assert db.get(FreshFoodOrder, 13).status == "completed"
    assert db.get(FreshFoodOrderLine, 1301).sent_qty == 9
    assert db.get(FreshFoodOrderLine, 1302).sent_qty is None
    assert db.get(FreshFoodOrderLine, 1303).sent_qty is None


def test_store_scope_blocks_other_store_dashboard(dashboard_app):
    flask_app, db = dashboard_app
    km = _seed_actor(db, uid=104, role="km", position="KM", store_key="tomball")
    client = _client_as(flask_app, km)

    resp = client.get("/uno/manager", follow_redirects=False)
    assert resp.status_code in {302, 403}
    if resp.status_code == 302:
        assert resp.headers["Location"].endswith("/dos/")


@pytest.mark.parametrize(
    ("uid", "role", "position"),
    [
        (201, "corporate", "Corporate"),
        (202, "corporate_chef", "Corporate Chef"),
        (203, "gm", "GM"),
        (204, "km", "KM"),
        (205, "foh_manager", "FOH Manager"),
        (206, "assistant_km", "Assistant KM"),
        (207, "expo", "Expo"),
    ],
)
def test_management_roles_can_access_catering_driver_manage_and_team_roster(
    dashboard_app, uid, role, position
):
    flask_app, db = dashboard_app
    actor = _seed_actor(db, uid=uid, role=role, position=position)
    client = _client_as(flask_app, actor)

    assert client.get("/dos/catering?tab=ez-manage").status_code == 200
    assert client.get("/ez-manage").status_code == 200
    assert client.get("/dos/team").status_code == 200
    assert client.get("/dos/vendors?tab=performance-food").status_code == 200
    assert client.get("/dos/vendors/performance-food/recent-orders").status_code == 200
    roster = client.get("/dos/schedules-v2/team-roster")
    assert roster.status_code == 200
    assert roster.get_json() is not None


def test_corporate_ez_orders_can_filter_combined_store_scope(dashboard_app):
    flask_app, db = dashboard_app
    corporate = _seed_actor(db, uid=230, role="corporate", position="Corporate")
    db.add_all([
        Order(
            external_order_id="TOM-230",
            origin_store_id="store_2",
            delivery_date="2099-06-08",
            deliver_at="2099-06-08T11:00:00",
            status="confirmed",
            client="Tomball Catering",
            potential_payout=35.0,
        ),
        Order(
            external_order_id="COP-230",
            origin_store_id="store_1",
            delivery_date="2099-06-08",
            deliver_at="2099-06-08T12:00:00",
            status="confirmed",
            client="Copperfield Catering",
            potential_payout=35.0,
        ),
    ])
    db.commit()
    client = _client_as(flask_app, corporate)

    combined = client.get("/corporate/orders")
    assert combined.status_code == 200
    combined_html = combined.get_data(as_text=True)
    assert "TOM-230" in combined_html
    assert "COP-230" in combined_html
    assert 'href="/corporate/orders?store=copperfield"' in combined_html
    assert 'href="/corporate/orders?store=tomball"' in combined_html
    assert 'class="ezo-combined"' not in combined_html
    assert 'class="ezo-combined-tab"' in combined_html

    tomball = client.get("/corporate/orders?store=tomball")
    assert tomball.status_code == 200
    tomball_html = tomball.get_data(as_text=True)
    assert "TOM-230" in tomball_html
    assert "COP-230" not in tomball_html
    assert 'href="/orders/tomball/2099-06-08?collapse_empty_rows=1"' in tomball_html

    copperfield = client.get("/corporate/orders?store=copperfield")
    assert copperfield.status_code == 200
    copperfield_html = copperfield.get_data(as_text=True)
    assert "COP-230" in copperfield_html
    assert "TOM-230" not in copperfield_html
    assert 'href="/orders/copperfield/2099-06-08?collapse_empty_rows=1"' in copperfield_html


def test_expo_can_access_all_vendor_tabs_but_not_insights(dashboard_app):
    flask_app, db = dashboard_app
    expo = _seed_actor(db, uid=120, role="expo", position="Expo")
    client = _client_as(flask_app, expo)

    vendors = client.get("/dos/vendors?tab=performance-food")
    assert vendors.status_code == 200
    assert _tab_keys(vendors.get_data(as_text=True)) == {
        "produce",
        "webstaurant",
        "performance-food",
        "restaurant-depot",
        "specs",
        "reports",
    }

    for path in (
        "/dos/produce/",
        "/dos/vendors/webstaurant/recent-orders",
        "/dos/vendors/performance-food/recent-orders",
        "/dos/vendors/restaurant-depot/recent-orders",
        "/dos/vendors/specs/recent-orders",
        "/dos/vendors/reports",
    ):
        assert client.get(path).status_code == 200, path

    assert client.get("/dos/performance").status_code == 403
    assert client.get("/dos/reports/server-performance").status_code == 403


def test_vendor_reports_render_order_items_and_price_watch(dashboard_app):
    flask_app, db = dashboard_app
    partner = _seed_actor(db, uid=240, role="partner", position="Partner")
    db.add_all([
        VendorRecentOrder(
            vendor="webstaurant",
            store_scope="tomball",
            order_number="W-200",
            placed_at=datetime(2099, 6, 10, 9, 0),
            total_cents=3000,
            status="confirmed",
            customer_or_caterer="Sam",
            parse_status="parsed",
            items_json=[{
                "name": "Nitrile Gloves",
                "sku": "GLV-XL",
                "qty": "2",
                "unit_price_cents": 1500,
                "subtotal_cents": 3000,
            }],
        ),
        VendorRecentOrder(
            vendor="webstaurant",
            store_scope="tomball",
            order_number="W-201",
            placed_at=datetime(2099, 6, 18, 9, 0),
            total_cents=1800,
            status="confirmed",
            customer_or_caterer="Chef Luis",
            parse_status="parsed",
            items_json={"items": [{
                "name": "Nitrile Gloves",
                "sku": "GLV-XL",
                "qty": "1",
                "unit_price_cents": 1800,
                "subtotal_cents": 1800,
            }]},
        ),
    ])
    db.commit()
    client = _client_as(flask_app, partner)
    with client.session_transaction() as sess:
        sess["partner_auth_ok"] = True

    resp = client.get(
        "/partner/vendors/reports"
        "?vendor=webstaurant&start=2099-06-01&end=2099-06-30"
    )

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Nitrile Gloves" in html
    assert "$48.00" in html
    assert "$18.00" in html
    assert "+20.0%" in html
    match = re.search(r'<option value="([^"]+)"[^>]*>Nitrile Gloves', html)
    assert match is not None

    detail_resp = client.get(
        "/partner/vendors/reports"
        f"?vendor=webstaurant&start=2099-06-01&end=2099-06-30&item={match.group(1)}"
    )
    assert detail_resp.status_code == 200
    detail_html = detail_resp.get_data(as_text=True)
    assert "Price History" in detail_html
    assert "Orders With This Item" in detail_html
    assert "Chef Luis" in detail_html
