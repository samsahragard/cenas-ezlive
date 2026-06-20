from scripts.toast_webhook_backfill import _stable_order_event_payload


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
