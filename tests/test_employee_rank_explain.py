from app.models import rank_peer_rows_ok, sanitize_rank_json
from app.web.employee_auth import _PERF_DETAIL_METRICS


def test_rank_detail_metrics_registered():
    assert "rank_standing" in _PERF_DETAIL_METRICS
    assert "rank_combined" in _PERF_DETAIL_METRICS


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
