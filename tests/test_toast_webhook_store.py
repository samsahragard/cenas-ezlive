import json

from app.services.toast_webhook_store import ToastWebhookStore


RESTAURANT_GUID = "restaurant-tomball"
SERVER_GUID = "toast-server-1"


def _event(guid: str, order: dict):
    return {
        "timestamp": "2026-06-05T23:00:00.000Z",
        "eventCategory": "order_updated",
        "eventType": "order_updated",
        "guid": guid,
        "details": {
            "restaurantGuid": RESTAURANT_GUID,
            "order": order,
        },
    }


def _order(selection_count: int = 1, paid: bool = False):
    selections = [
        {
            "guid": f"selection-{i}",
            "entityType": "MenuItemSelection",
            "displayName": f"Plate {i}",
            "quantity": 1,
            "price": 10 + i,
        }
        for i in range(1, selection_count + 1)
    ]
    payments = []
    if paid:
        payments.append({
            "guid": "payment-1",
            "entityType": "OrderPayment",
            "amount": 22.0,
            "tipAmount": 4.0,
            "type": "CREDIT",
            "paymentStatus": "CAPTURED",
            "paidDate": "2026-06-05T23:05:00.000+0000",
        })
    return {
        "guid": "order-1",
        "entityType": "Order",
        "server": {"guid": SERVER_GUID, "entityType": "RestaurantUser"},
        "source": "In Store",
        "businessDate": 20260605,
        "approvalStatus": "APPROVED",
        "table": {"guid": "table-103", "name": "103"},
        "openedDate": "2026-06-05T23:00:00.000+0000",
        "modifiedDate": "2026-06-05T23:05:00.000+0000",
        "checks": [
            {
                "guid": "check-1",
                "entityType": "Check",
                "displayNumber": "7",
                "payments": payments,
                "voided": False,
                "deleted": False,
                "paymentStatus": "CLOSED" if paid else "OPEN",
                "openedDate": "2026-06-05T23:00:00.000+0000",
                "closedDate": "2026-06-05T23:06:00.000+0000" if paid else None,
                "totalAmount": 22.0 if paid else 0.0,
                "selections": selections,
            }
        ],
    }


def _store_event(store: ToastWebhookStore, payload: dict):
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return store.store_webhook_event(
        payload=payload,
        raw_body=raw,
        headers={"Toast-Attempt-Number": "1"},
        signature_verified=True,
        source="test",
    )


def test_store_projects_order_snapshots_and_employee_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_TOMBALL", RESTAURANT_GUID)
    store = ToastWebhookStore(tmp_path / "toast.sqlite")
    store.init_schema()

    with store.connect() as conn:
        store._upsert_identity(
            conn,
            store_key="tomball",
            toast_employee_guid=SERVER_GUID,
            cena_employee_id=101,
            source="test",
            verified=True,
            confidence=1.0,
        )
        conn.commit()

    first = _store_event(store, _event("event-1", _order()))
    second = _store_event(store, _event("event-2", _order(selection_count=2, paid=True)))
    duplicate = _store_event(store, _event("event-2", _order(selection_count=2, paid=True)))

    assert first["stored"] is True
    assert second["stored"] is True
    assert duplicate["duplicate"] is True

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM toast_webhook_event").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM toast_order_current").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM toast_selection_current").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM toast_payment_current").fetchone()[0] == 1
        fact_types = {
            row[0]
            for row in conn.execute(
                "SELECT fact_type FROM employee_toast_fact WHERE cena_employee_id = 101"
            ).fetchall()
        }

    assert {"order_created", "check_opened", "item_added", "payment_added", "check_closed"} <= fact_types


def test_store_tracks_unmatched_employee_guid(tmp_path, monkeypatch):
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_TOMBALL", RESTAURANT_GUID)
    store = ToastWebhookStore(tmp_path / "toast.sqlite")

    _store_event(store, _event("event-1", _order()))

    with store.connect() as conn:
        rows = conn.execute("SELECT toast_employee_guid, context FROM employee_toast_unmatched").fetchall()

    assert {row["toast_employee_guid"] for row in rows} == {SERVER_GUID}
    assert "order_created" in {row["context"] for row in rows}
