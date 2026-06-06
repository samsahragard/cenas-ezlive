from app.services.toast_table_activity import latest_table_activity_payload


class FakeToastClient:
    def fetch_tables(self, location, restaurant_guid):
        assert location == "tomball"
        assert restaurant_guid == "restaurant-guid"
        return [{"guid": "table-guid", "name": "311"}]

    def fetch_employees(self, location, restaurant_guid):
        assert location == "tomball"
        assert restaurant_guid == "restaurant-guid"
        return [
            {"guid": "opened-by-guid", "firstName": "Ana", "lastName": "Lopez"},
            {"guid": "server-guid", "firstName": "Maria", "lastName": "Garcia"},
        ]

    def fetch_orders_for_date(self, location, restaurant_guid, business_date, refresh=True):
        assert location == "tomball"
        assert restaurant_guid == "restaurant-guid"
        assert business_date == "20260605"
        assert refresh is True
        return [
            {
                "source": "In Store",
                "server": {"guid": "server-guid", "entityType": "RestaurantUser"},
                "table": {"guid": "table-guid", "entityType": "Table"},
                "checks": [
                    {
                        "openedDate": "2026-06-06T00:54:00.000+0000",
                        "openedBy": {"guid": "opened-by-guid", "entityType": "RestaurantUser"},
                        "displayNumber": "117",
                    }
                ],
            }
        ]


def test_latest_table_activity_payload_includes_waiter_and_opened_by(monkeypatch):
    monkeypatch.setattr(
        "app.services.toast_table_activity.restaurant_guids",
        lambda: {"tomball": "restaurant-guid"},
    )

    payload = latest_table_activity_payload(
        "tomball",
        client=FakeToastClient(),
        business_date="20260605",
    )

    latest = payload["latest"]
    assert latest["table_name"] == "311"
    assert latest["opened_at_local"] == "2026-06-05 7:54 PM CT"
    assert latest["opened_by_name"] == "Ana Lopez"
    assert latest["server_name"] == "Maria Garcia"
    assert latest["employee_lookup_available"] is True
