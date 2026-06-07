from app.services import toast_analytics_summary as summary


class _FakeToastAnalyticsClient:
    def metrics(self, start_ymd, end_ymd, restaurant_ids):
        assert (start_ymd, end_ymd, restaurant_ids) == ("20260607", "20260607", [])
        return [
            {
                "restaurantGuid": "restaurant-1",
                "netSalesAmount": 150.89,
                "grossSalesAmount": 150.89,
                "discountAmount": 0,
                "voidOrdersAmount": 0,
                "refundAmount": 0,
                "ordersCount": 5,
                "guestCount": 8,
                "hourlyJobTotalHours": 52,
                "hourlyJobTotalPay": 534.68,
            }
        ]

    def labor(self, start_ymd, end_ymd, restaurant_ids, group_by):
        assert (start_ymd, end_ymd, restaurant_ids, group_by) == (
            "20260607",
            "20260607",
            [],
            ["JOB"],
        )
        return [{"jobTitle": "Server", "totalCost": 534.68, "totalHours": 52}]

    def menu(self, start_ymd, end_ymd, restaurant_ids):
        assert (start_ymd, end_ymd, restaurant_ids) == ("20260607", "20260607", [])
        return []


def test_analytics_summary_payload_includes_date_label_and_labor_ratio_guard(monkeypatch):
    monkeypatch.setattr(
        summary,
        "period_to_ymd_range",
        lambda period: ("20260607", "20260607", "This Week"),
    )

    payload = summary.analytics_summary_payload("week", client=_FakeToastAnalyticsClient())

    assert payload["period"] == "week"
    assert payload["label"] == "This Week"
    assert payload["date_range"] == {
        "start": "20260607",
        "end": "20260607",
        "label": "2026-06-07",
    }
    assert payload["date_range_label"] == "2026-06-07"
    assert payload["sales"]["orders"] == 5
    assert payload["labor"]["ratio_pct"] == 354.4
    assert payload["labor"]["ratio_denominator_ok"] is False
    assert payload["labor"]["ratio_guard"]["min_orders"] == summary.LABOR_RATIO_MIN_ORDERS
    assert "$150.89 net sales" in payload["labor"]["ratio_guard"]["note"]


def test_analytics_summary_payload_supports_yesterday_period(monkeypatch):
    monkeypatch.setattr(
        summary,
        "period_to_ymd_range",
        lambda period: ("20260606", "20260606", "Yesterday"),
    )

    class YesterdayClient(_FakeToastAnalyticsClient):
        def metrics(self, start_ymd, end_ymd, restaurant_ids):
            assert (start_ymd, end_ymd, restaurant_ids) == ("20260606", "20260606", [])
            return super().metrics("20260607", "20260607", restaurant_ids)

        def labor(self, start_ymd, end_ymd, restaurant_ids, group_by):
            assert (start_ymd, end_ymd, restaurant_ids, group_by) == (
                "20260606",
                "20260606",
                [],
                ["JOB"],
            )
            return super().labor("20260607", "20260607", restaurant_ids, group_by)

        def menu(self, start_ymd, end_ymd, restaurant_ids):
            assert (start_ymd, end_ymd, restaurant_ids) == ("20260606", "20260606", [])
            return []

    payload = summary.analytics_summary_payload("yesterday", client=YesterdayClient())

    assert payload["period"] == "yesterday"
    assert payload["label"] == "Yesterday"
    assert payload["date_range_label"] == "2026-06-06"
