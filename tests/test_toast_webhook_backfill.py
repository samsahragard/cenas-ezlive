from scripts import toast_webhook_backfill as backfill_mod
from scripts.toast_webhook_backfill import _stable_order_event_payload, backfill_orders


def test_stable_order_event_payload_ignores_unmodified_response_noise():
    first = {
        "guid": "order-1",
        "modifiedDate": "2026-06-20T12:30:00.000-0500",
        "server": {"guid": "server-1", "displayName": "First"},
    }
    second = {
        "guid": "order-1",
        "modifiedDate": "2026-06-20T12:30:00.000-0500",
        "server": {"guid": "server-1", "displayName": "First"},
        "transientResponseField": "changed",
    }
    changed = {
        "guid": "order-1",
        "modifiedDate": "2026-06-20T12:35:00.000-0500",
    }

    assert _stable_order_event_payload("tomball", "20260620", first) == _stable_order_event_payload(
        "tomball",
        "20260620",
        second,
    )
    assert _stable_order_event_payload("tomball", "20260620", first) != _stable_order_event_payload(
        "tomball",
        "20260620",
        changed,
    )


def test_backfill_orders_records_chronological_scope_and_latest_watermark(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.seen_dates = []

        def fetch_orders_for_date(self, store_key, restaurant_guid, business_date, refresh):
            self.seen_dates.append((store_key, restaurant_guid, business_date, refresh))
            return []

    class FakeToastClient:
        @staticmethod
        def shared():
            return client

    class FakeStore:
        def __init__(self):
            self.watermarks = []
            self.pull_logs = []

        def set_watermark(self, **kwargs):
            self.watermarks.append(kwargs)

        def record_pull_log(self, **kwargs):
            self.pull_logs.append(kwargs)

        def store_webhook_event(self, **kwargs):
            raise AssertionError("no orders should be written in this test")

    client = FakeClient()
    store = FakeStore()
    monkeypatch.setattr(backfill_mod, "ToastClient", FakeToastClient)
    monkeypatch.setattr(backfill_mod, "restaurant_guids", lambda: {"copperfield": "rest-1"})
    monkeypatch.setattr(backfill_mod, "business_dates_for_backfill", lambda days: ["20260621", "20260620"])

    assert backfill_orders(store, days=2, refresh=True) == {"copperfield": 0}

    assert client.seen_dates == [
        ("copperfield", "rest-1", "20260621", True),
        ("copperfield", "rest-1", "20260620", True),
    ]
    assert {
        row["key"]: row["value"]
        for row in store.watermarks
    }["last_business_date"] == "20260621"
    assert store.pull_logs[0]["scope_start"] == "20260620"
    assert store.pull_logs[0]["scope_end"] == "20260621"
