from app.models import rank_peer_rows_ok, sanitize_rank_json
from app.web.perf_push_routes import _SALES_WALL
from app.web.employee_auth import _PERF_DETAIL_METRICS


def test_rank_detail_metrics_registered():
    assert "rank_standing" in _PERF_DETAIL_METRICS
    assert "rank_combined" in _PERF_DETAIL_METRICS


def test_performance_center_does_not_emit_peer_source_key():
    source = open("app/web/employee_auth.py", encoding="utf-8").read()

    assert '"peer_source"' not in source
    assert "'peer_source'" not in source


def test_my_performance_uses_sanitized_cache_only():
    source = open("app/web/employee_auth.py", encoding="utf-8").read()
    start = source.index('def my_performance():')
    end = source.index('@employee_auth.route("/employee/performance-center"', start)
    body = source[start:end]

    assert "read_snapshot" not in body
    assert '"timecards"' not in body
    assert '"performance"' not in body
    assert '"payroll"' not in body


def test_employee_marketplace_person_refs_hide_employee_ids():
    service = open("app/services/scheduling_offers.py", encoding="utf-8").read()
    template = open("app/templates/employee_shift_marketplace.html", encoding="utf-8").read()
    emp_ref = service[service.index("def emp_ref"):service.index("def offer_card")]

    assert '"id"' not in emp_ref
    assert "(\"#\"+p.id)" not in template


def test_my_profile_hub_is_registered_and_session_scoped():
    init_source = open("app/__init__.py", encoding="utf-8").read()
    auth_source = open("app/web/auth.py", encoding="utf-8").read()
    route_source = open("app/web/employee_my_profile_page.py", encoding="utf-8").read()

    assert "employee_my_profile_page" in init_source
    assert '"/employee/my-profile"' in auth_source
    assert '@employee_auth.route("/employee/my-profile"' in route_source
    assert "import request" not in route_source
    assert "request.args" not in route_source
    assert "request.get_json" not in route_source
    assert 'session.get("employee_id")' in route_source
    for forbidden in ("phone", "email", "address", "passcode_hash", "toast_id"):
        assert forbidden not in route_source


def test_my_profile_template_omits_hidden_identity_fields():
    template = open("app/templates/employee_my_profile.html", encoding="utf-8").read()

    for forbidden in (
        "employee_id",
        "toast_id",
        "passcode",
        "eligible_sales",
        "cashSales",
        "nonCashSales",
        "GUID",
        "guid",
        "syncing",
        "synced",
        "refresh",
        "scheduleUrl",
        "rosterUrl",
        "/employee/my-schedule/shifts",
        "/employee/roster",
    ):
        assert forbidden not in template

    assert "profile-data" in template
    assert "SAFE_DATA.schedule" in template
    assert "SAFE_DATA.roster" in template

    assert "/employee/my-profile" not in open("app/templates/partials/_employee_nav.html", encoding="utf-8").read()
    assert 'href="/employee/my-profile"' in open("app/templates/employee_dashboard.html", encoding="utf-8").read()


def test_performance_center_zeroes_non_tipped_tip_dollars():
    source = open("app/web/employee_auth.py", encoding="utf-8").read()
    body = source[source.index("def performance_center():"):source.index("_PERF_DETAIL_METRICS")]

    assert "tips = round(float(r.tips or 0), 2) if is_tipped else 0.0" in body
    assert "dtips = round(sum(float(x.tips or 0) for x in ss), 2) if is_tipped else 0.0" in body


def test_perf_push_sales_wall_catches_camel_case_terms():
    blocked = [
        '{"eligibleSales": 1}',
        '{"grossSales": 1}',
        '{"netSales": 1}',
        '{"sourceSales": 1}',
        '{"checkTotal": 1}',
        '{"storeTotal": 1}',
        '{"salesBasis": 1}',
        '{"eligibleSalesBasis": 1}',
        '{"salesAttributed": 1}',
        '{"salesDollars": 1}',
        '{"cashAmount": 1}',
        '{"ccSubtotal": 1}',
    ]

    for body in blocked:
        assert _SALES_WALL.search(body), body


def test_dashboard_rank_links_keep_selected_period():
    template = open("app/templates/employee_dashboard.html", encoding="utf-8").read()

    assert "encodeURIComponent(sel)" in template
    assert "detailHref('rank_standing')" in template
    assert "detailHref('rank_combined')" in template


def test_rank_detail_uses_rank_safe_summary_cards():
    template = open("app/templates/employee_performance_detail.html", encoding="utf-8").read()

    rank_branch = template[template.index("if (cfg.rank){"):template.index("} else {", template.index("if (cfg.rank){"))]
    assert "Compare group" in rank_branch
    assert "Base pay" not in rank_branch
    assert "Total pay" not in rank_branch
    assert "Tips" not in rank_branch


def test_rank_peer_rows_allow_only_safe_explanation_fields():
    payload = {
        "leaderboards": {
            "last30": {
                "effective_hourly": {
                    "rows": [
                        {
                            "name": "Yadira",
                            "rank": 1,
                            "effective_hourly": 29.14,
                            "tip_percent": 17.5,
                            "tips_per_hour": 8.25,
                            "combined": 91.2,
                            "combined_rank": 1,
                            "is_me": True,
                        }
                    ]
                }
            }
        }
    }

    ok, offending = rank_peer_rows_ok(payload)

    assert ok is True
    assert offending == []


def test_rank_sanitizer_strips_unsafe_peer_explanation_fields():
    payload = {
        "cena_employee_id": 71,
        "leaderboards": {
            "last30": {
                "effective_hourly": {
                    "cohort_key": "internal-store-role-key",
                    "rows": [
                        {
                            "name": "Peer",
                            "rank": 2,
                            "effective_hourly": 21.5,
                            "tip_percent": 13.2,
                            "tips_per_hour": 4.1,
                            "combined": 72.0,
                            "employee_id": 123,
                            "base_pay": 100.0,
                            "tips": 50.0,
                            "GUID": "secret-guid",
                            "eligible_sales": 999.0,
                        }
                    ],
                }
            }
        },
    }

    sanitized = sanitize_rank_json(payload)
    row = sanitized["leaderboards"]["last30"]["effective_hourly"]["rows"][0]

    assert "cena_employee_id" not in sanitized
    assert "cohort_key" not in sanitized["leaderboards"]["last30"]["effective_hourly"]
    assert row == {
        "name": "Peer",
        "rank": 2,
        "effective_hourly": 21.5,
        "tip_percent": 13.2,
        "tips_per_hour": 4.1,
        "combined": 72.0,
    }
