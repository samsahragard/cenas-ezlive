from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from flask import Flask
from sqlalchemy.orm import sessionmaker

from app.models import CenaToastLink, Employee, PerfPeriodCache, PerfRankCache
from app.services import employee_table_timelines
from app.services import toast_reports
from app.web import employee_auth as employee_mod
from app.web import employee_tables_page as tables_mod


def test_server_activity_for_guids_is_employee_scoped_and_sales_clean(monkeypatch):
    class FakeToast:
        def fetch_tables(self, location, restaurant_guid):
            return [{"guid": "table-14", "name": "14"}]

        def fetch_orders_for_date(self, location, restaurant_guid, business_date, refresh=False):
            return [{
                "openedDate": "2026-06-08T18:00:00.000+0000",
                "server": {"guid": "toast-kennya"},
                "table": {"guid": "table-14"},
                "checks": [
                    {
                        "openedDate": "2026-06-08T18:00:00.000+0000",
                        "closedDate": "2026-06-08T19:10:00.000+0000",
                        "selections": [
                            {
                                "createdDate": "2026-06-08T18:05:00.000+0000",
                                "item": {"guid": "drink-1"},
                                "displayName": "Margarita",
                            },
                            {
                                "createdDate": "2026-06-08T18:12:00.000+0000",
                                "item": {"guid": "app-1"},
                                "displayName": "Queso",
                            },
                            {
                                "createdDate": "2026-06-08T18:21:00.000+0000",
                                "item": {"guid": "entree-1"},
                                "displayName": "Enchiladas",
                            },
                        ],
                        "payments": [
                            {
                                "type": "CREDIT",
                                "amount": 100.0,
                                "tipAmount": 20.0,
                                "paidDate": "2026-06-08T19:12:00.000+0000",
                            }
                        ],
                    }
                ],
            }, {
                "openedDate": "2026-06-08T18:30:00.000+0000",
                "server": {"guid": "toast-other"},
                "table": {"name": "99"},
                "checks": [{
                    "openedDate": "2026-06-08T18:30:00.000+0000",
                    "payments": [{"type": "CREDIT", "amount": 900.0, "tipAmount": 90.0}],
                }],
            }]

    monkeypatch.setattr(toast_reports.ToastClient, "shared", staticmethod(lambda: FakeToast()))
    monkeypatch.setattr(toast_reports, "restaurant_guids", lambda: {"tomball": "rg-tomball"})
    monkeypatch.setattr(toast_reports, "_load_item_categories", lambda: {
        "drink-1": {"category": "drink"},
        "app-1": {"category": "appetizer"},
        "entree-1": {"category": "entree"},
    })

    payload = toast_reports.server_activity_for_guids({"toast-kennya"}, "tomball", "20260608")

    assert payload["tickets"] == 1
    assert payload["cc_tips"] == 20.0
    assert payload["tip_pct"] == 20.0
    assert payload["avg_drink_secs"] == 300
    assert payload["app_count"] == 1
    assert payload["activities"][0]["table_name"] == "14"
    assert payload["activities"][0]["cc_tips"] == 20.0
    assert "cc_subtotal" not in payload
    assert all("cc_subtotal" not in row for row in payload["activities"])


def test_server_table_timelines_show_employee_items_and_payment_method_only(monkeypatch):
    class FakeToast:
        def fetch_tables(self, location, restaurant_guid):
            return [{"guid": "table-63", "name": "63"}]

        def fetch_orders_for_date(self, location, restaurant_guid, business_date, refresh=False):
            return [{
                "openedDate": "2026-06-08T18:00:00.000+0000",
                "server": {"guid": "toast-kennya"},
                "table": {"guid": "table-63"},
                "checks": [{
                    "displayNumber": "119",
                    "openedDate": "2026-06-08T18:00:00.000+0000",
                    "closedDate": "2026-06-08T18:52:00.000+0000",
                    "selections": [
                        {
                            "guid": "selection-secret-1",
                            "createdDate": "2026-06-08T18:01:00.000+0000",
                            "item": {"guid": "drink-1"},
                            "displayName": "Monday Rita",
                            "price": 8.50,
                        },
                        {
                            "guid": "selection-secret-2",
                            "createdDate": "2026-06-08T18:09:00.000+0000",
                            "item": {"guid": "entree-1"},
                            "displayName": "Fish El Rey",
                            "price": 19.95,
                        },
                    ],
                    "payments": [{
                        "guid": "payment-secret",
                        "type": "CREDIT",
                        "paymentStatus": "PAID",
                        "amount": 92.78,
                        "tipAmount": 6.03,
                        "paidDate": "2026-06-08T18:53:00.000+0000",
                        "cardLast4": "4242",
                    }],
                }],
            }, {
                "openedDate": "2026-06-08T18:05:00.000+0000",
                "server": {"guid": "toast-other"},
                "table": {"name": "99"},
                "checks": [{
                    "displayNumber": "999",
                    "openedDate": "2026-06-08T18:05:00.000+0000",
                    "payments": [{"type": "CREDIT", "amount": 900.0, "tipAmount": 90.0}],
                }],
            }]

    monkeypatch.setattr(toast_reports.ToastClient, "shared", staticmethod(lambda: FakeToast()))
    monkeypatch.setattr(toast_reports, "restaurant_guids", lambda: {"copperfield": "rg-cop"})
    monkeypatch.setattr(toast_reports, "_load_item_categories", lambda: {
        "drink-1": {"category": "drink"},
        "entree-1": {"category": "entree"},
    })

    payload = toast_reports.server_table_timelines_for_guids({"toast-kennya"}, "copperfield", "20260608")
    encoded = json.dumps(payload).lower()

    assert payload["tickets"] == 1
    row = payload["timelines"][0]
    assert row["table_name"] == "63"
    assert row["display_number"] == "119"
    assert row["drink_rang_at"] == "2026-06-08T18:01:00Z"
    assert row["food_rang_at"] == "2026-06-08T18:09:00Z"
    assert row["payment_methods"] == [{
        "method": "Credit",
        "status": "Paid",
        "paid_at": "2026-06-08T18:53:00Z",
    }]
    assert {item["name"] for item in row["selections"]} == {"Monday Rita", "Fish El Rey"}
    assert payload["raw_payloads_included"] is False
    for forbidden in (
        "payment-secret",
        "selection-secret",
        "toast-other",
        "cardlast4",
        "4242",
        "amount",
        "tipamount",
        "price",
        "customer",
        "cc_subtotal",
    ):
        assert forbidden not in encoded


def test_employee_tables_yesterday_reads_personal_profile_db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        employee_table_timelines,
        "central_business_dates",
        lambda: ("20260608", "20260607", "2026-06-08"),
    )
    profile_db = tmp_path / "cena_employee_101.sqlite"
    conn = sqlite3.connect(profile_db)
    conn.executescript(
        """
        CREATE TABLE related_order_current (
            order_guid TEXT PRIMARY KEY,
            store_key TEXT,
            business_date TEXT,
            table_name TEXT,
            opened_date TEXT,
            closed_date TEXT,
            paid_date TEXT,
            payment_status TEXT
        );
        CREATE TABLE related_check_current (
            check_guid TEXT PRIMARY KEY,
            order_guid TEXT NOT NULL,
            store_key TEXT,
            business_date TEXT,
            display_number TEXT,
            payment_status TEXT,
            opened_date TEXT,
            closed_date TEXT,
            paid_date TEXT,
            voided INTEGER NOT NULL,
            deleted INTEGER NOT NULL
        );
        CREATE TABLE related_selection_current (
            selection_guid TEXT PRIMARY KEY,
            check_guid TEXT,
            order_guid TEXT NOT NULL,
            display_name TEXT,
            quantity REAL,
            business_date TEXT,
            voided INTEGER NOT NULL
        );
        CREATE TABLE related_payment_current (
            payment_guid TEXT PRIMARY KEY,
            check_guid TEXT,
            order_guid TEXT NOT NULL,
            payment_type TEXT,
            payment_status TEXT,
            paid_date TEXT,
            business_date TEXT
        );
        CREATE TABLE toast_fact (
            cena_employee_id INTEGER,
            fact_type TEXT,
            order_guid TEXT,
            check_guid TEXT,
            business_date TEXT,
            occurred_at TEXT,
            summary_json TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO related_order_current VALUES (?,?,?,?,?,?,?,?)",
        ("order-secret", "tomball", "20260607", "41", "2026-06-07T17:00:00Z", "2026-06-07T17:45:00Z", "2026-06-07T17:46:00Z", "PAID"),
    )
    conn.execute(
        "INSERT INTO related_check_current VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("check-secret", "order-secret", "tomball", "20260607", "62", "PAID", "2026-06-07T17:00:00Z", "2026-06-07T17:45:00Z", "2026-06-07T17:46:00Z", 0, 0),
    )
    conn.executemany(
        "INSERT INTO related_selection_current VALUES (?,?,?,?,?,?,?)",
        [
            ("selection-secret-1", "check-secret", "order-secret", "Monday Rita", 1, "20260607", 0),
            ("selection-secret-2", "check-secret", "order-secret", "Fajita Salad", 1, "20260607", 0),
        ],
    )
    conn.execute(
        "INSERT INTO related_payment_current VALUES (?,?,?,?,?,?,?)",
        ("payment-secret", "check-secret", "order-secret", "CREDIT", "PAID", "2026-06-07T17:46:00Z", "20260607"),
    )
    conn.executemany(
        "INSERT INTO toast_fact VALUES (?,?,?,?,?,?,?)",
        [
            (101, "item_added", "order-secret", "check-secret", "20260607", "2026-06-07T17:01:00Z", json.dumps({"name": "Monday Rita"})),
            (101, "item_added", "order-secret", "check-secret", "20260607", "2026-06-07T17:09:00Z", json.dumps({"name": "Fajita Salad"})),
        ],
    )
    conn.commit()
    conn.close()

    payload = employee_table_timelines.employee_table_timelines_payload(
        101,
        [SimpleNamespace(toast_id="toast-kennya", store_key="tomball")],
        day="yesterday",
        profile_dir=tmp_path,
    )
    encoded = json.dumps(payload).lower()

    assert payload["source"] == "profile_db"
    assert payload["used_profile_db"] is True
    assert payload["business_date"] == "20260607"
    row = payload["timelines"][0]
    assert row["table_name"] == "41"
    assert row["display_number"] == "62"
    assert row["drink_rang_at"] == "2026-06-07T17:01:00Z"
    assert row["food_rang_at"] == "2026-06-07T17:09:00Z"
    assert row["payment_methods"][0]["method"] == "Credit"
    assert payload["raw_payloads_included"] is False
    for forbidden in (
        "order-secret",
        "check-secret",
        "selection-secret",
        "payment-secret",
        "amount",
        "tip_amount",
        "price",
        "customer",
        "card",
    ):
        assert forbidden not in encoded


def test_my_performance_merges_live_service_into_today_cache(db_session, monkeypatch):
    test_session_factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(employee_mod, "SessionLocal", test_session_factory)

    emp = Employee(full_name="Kennya Garcia", active=True, session_version=1)
    db_session.add(emp)
    db_session.flush()
    db_session.add_all([
        CenaToastLink(
            cena_employee_id=emp.id,
            store_key="copperfield",
            toast_id="toast-kennya",
            toast_name="Kennya Garcia",
        ),
        PerfPeriodCache(
            cena_employee_id=emp.id,
            period="today",
            period_start="2026-06-08",
            period_end="2026-06-08",
            total_hours=0.0,
            base_pay=0.0,
            tips=0.0,
            service_json={},
        ),
        PerfRankCache(
            cena_employee_id=emp.id,
            rank_json={"is_tipped": True},
        ),
    ])
    db_session.commit()

    monkeypatch.setattr(employee_mod, "_employee_live_service_for_links", lambda links, is_tipped: {
        "date": "2026-06-08",
        "tickets": 16,
        "open_checks": 2,
        "closed_checks": 14,
        "cc_tips": 76.97,
        "tip_pct": 16.9,
        "avg_drink_secs": 289,
        "avg_app_secs": 301,
        "app_count": 2,
        "avg_entree_secs": 1086,
        "avg_gap_secs": 698,
        "avg_duration_secs": 4560,
        "activities": [{"table_name": "14", "status": "open", "cc_tips": 12.0}],
    })

    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(employee_mod.employee_auth)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["employee_id"] = emp.id
        sess["employee_session_version"] = emp.session_version
        sess["auth_ok"] = True

    res = client.get("/employee/my-performance")
    data = res.get_json()

    assert res.status_code == 200
    assert data["live_service"]["tickets"] == 16
    today = data["perf_periods"][0]
    assert today["period"] == "today"
    assert today["tips"] == 76.97
    assert today["tips_live"] is True
    assert today["service"]["live_toast"]["tickets"] == 16
    assert today["service"]["live_toast"]["tip_pct"] == 16.9


def test_employee_dashboard_has_live_today_surface():
    template = open("app/templates/employee_dashboard.html", encoding="utf-8").read()

    assert 'id="perf-live-wrap"' in template
    assert "var PERIODS = {}, shifts = [], sel = 'today'" in template
    assert "loadPerformance(true)" in template
    assert "No live table activity yet for today" in template


def test_employee_tables_page_registered_and_linked():
    init_source = open("app/__init__.py", encoding="utf-8").read()
    auth_source = open("app/web/auth.py", encoding="utf-8").read()
    route_source = open("app/web/employee_tables_page.py", encoding="utf-8").read()
    dashboard = open("app/templates/employee_dashboard.html", encoding="utf-8").read()
    profile = open("app/templates/employee_my_profile.html", encoding="utf-8").read()
    template = open("app/templates/employee_tables.html", encoding="utf-8").read()

    assert "employee_tables_page" in init_source
    assert '"/employee/tables"' in auth_source
    assert '@employee_auth.route("/employee/tables"' in route_source
    assert '@employee_auth.route("/employee/tables/data"' in route_source
    assert 'session.get("employee_id")' in route_source
    assert 'href="/employee/tables"' in dashboard
    assert 'href="/employee/tables"' in profile
    assert "Today standing" in template
    assert "Ticket items" in template
    for forbidden in ("cardLast4", "Toast GUID", "eligible_sales", "cc_subtotal"):
        assert forbidden not in template


def test_employee_tables_data_is_session_scoped(db_session, monkeypatch):
    test_session_factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(tables_mod, "SessionLocal", test_session_factory)

    emp = Employee(full_name="Kennya Garcia", active=True, session_version=1)
    db_session.add(emp)
    db_session.flush()
    db_session.add(CenaToastLink(
        cena_employee_id=emp.id,
        store_key="copperfield",
        toast_id="toast-kennya",
        toast_name="Kennya Garcia",
    ))
    db_session.commit()

    captured = {}

    def fake_payload(cena_employee_id, links, *, day, limit):
        captured["employee_id"] = cena_employee_id
        captured["day"] = day
        captured["limit"] = limit
        captured["links"] = [(link.toast_id, link.store_key) for link in links]
        return {
            "ok": True,
            "source": "toast_live",
            "business_date": "20260608",
            "tickets": 1,
            "timelines": [{"table_name": "63"}],
            "raw_payloads_included": False,
        }

    monkeypatch.setattr(tables_mod, "employee_table_timelines_payload", fake_payload)
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(employee_mod.employee_auth)
    client = app.test_client()

    assert client.get("/employee/tables/data").status_code == 401

    with client.session_transaction() as sess:
        sess["employee_id"] = emp.id
        sess["employee_session_version"] = emp.session_version
        sess["auth_ok"] = True

    res = client.get("/employee/tables/data?day=today&limit=40")
    data = res.get_json()

    assert res.status_code == 200
    assert data["linked"] is True
    assert data["timelines"][0]["table_name"] == "63"
    assert captured == {
        "employee_id": emp.id,
        "day": "today",
        "limit": 40,
        "links": [("toast-kennya", "copperfield")],
    }
