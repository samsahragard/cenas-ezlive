from __future__ import annotations

from flask import Flask
from sqlalchemy.orm import sessionmaker

from app.models import CenaToastLink, Employee, PerfPeriodCache, PerfRankCache
from app.services import toast_reports
from app.web import employee_auth as employee_mod


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
