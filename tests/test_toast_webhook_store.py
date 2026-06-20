import json
import sqlite3

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


def test_materializes_personal_employee_profile_database(tmp_path, monkeypatch):
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
        conn.execute(
            """
            INSERT INTO employee_profile_current
                (cena_employee_id, profile_json, source, generated_at)
            VALUES (?, ?, ?, ?)
            """,
            (101, json.dumps({"name": "Server One"}), "test", "2026-06-06T00:00:00Z"),
        )
        conn.commit()

    _store_event(store, _event("event-1", _order(selection_count=2, paid=True)))

    result = store.materialize_employee_profile_databases(output_dir=tmp_path / "employee_profiles")

    profile_db = tmp_path / "employee_profiles" / "cena_employee_101.sqlite"
    assert result["databases"] == 1
    assert profile_db.exists()

    conn = sqlite3.connect(profile_db)
    try:
        assert conn.execute("SELECT value FROM metadata WHERE key = 'raw_payloads_included'").fetchone()[0] == "false"
        assert conn.execute("SELECT COUNT(*) FROM employee_profile_current").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM toast_identity_map").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM toast_fact").fetchone()[0] >= 4
        assert conn.execute("SELECT COUNT(*) FROM related_order_current").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM related_selection_current").fetchone()[0] == 2
        table_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()

    assert "toast_webhook_event" not in table_names


def test_store_auto_materializes_impacted_employee_database(tmp_path, monkeypatch):
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_TOMBALL", RESTAURANT_GUID)
    monkeypatch.setenv("TOAST_EMPLOYEE_PROFILE_DBS_AUTO_EXPORT", "1")
    monkeypatch.setenv("TOAST_EMPLOYEE_PROFILE_DB_DIR", str(tmp_path / "live_profiles"))
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

    result = _store_event(store, _event("event-1", _order()))

    assert result["employee_profile_db_error"] is None
    assert result["employee_profile_dbs"]["databases"] == 1
    assert (tmp_path / "live_profiles" / "cena_employee_101.sqlite").exists()


def test_api_entity_mirror_projects_operational_tables(tmp_path):
    store = ToastWebhookStore(tmp_path / "toast.sqlite")
    store.init_schema()

    rows = [
        (
            "employee",
            {
                "guid": "employee-1",
                "externalEmployeeId": "EMP-1",
                "firstName": "Yadira",
                "chosenName": "Yadi",
                "lastName": "Hernandez",
                "email": "yadira@example.test",
            },
        ),
        ("job", {"guid": "job-server", "title": "Server", "wageType": "HOURLY"}),
        (
            "service_area",
            {
                "guid": "service-area-main",
                "name": "Main Dining",
            },
        ),
        (
            "table",
            {
                "guid": "table-101",
                "name": "101",
                "serviceArea": {"guid": "service-area-main"},
            },
        ),
        (
            "time_entry",
            {
                "guid": "time-entry-1",
                "employeeReference": {"guid": "employee-1"},
                "jobReference": {"guid": "job-server"},
                "businessDate": 20260620,
                "inDate": "2026-06-20T09:00:00.000-0500",
                "outDate": "2026-06-20T13:30:00.000-0500",
                "regularHours": 4.0,
                "overtimeHours": 0.5,
            },
        ),
        (
            "shift",
            {
                "guid": "shift-1",
                "employeeReference": {"guid": "employee-1"},
                "jobReference": {"guid": "job-server"},
                "businessDate": 20260620,
                "inDate": "2026-06-20T09:00:00.000-0500",
                "outDate": "2026-06-20T14:00:00.000-0500",
            },
        ),
    ]

    for domain, payload in rows:
        assert store.upsert_api_entity(
            domain=domain,
            store_key="tomball",
            payload=payload,
            source="test_sync",
        )

    assert store.upsert_api_entity(
        domain="employee",
        store_key="tomball",
        payload={
            "guid": "employee-1",
            "firstName": "Yadira",
            "chosenName": "Yadira R",
            "lastName": "Hernandez",
        },
        source="test_sync",
    )
    assert not store.upsert_api_entity(
        domain="employee",
        store_key="tomball",
        payload={"firstName": "Missing Guid"},
        source="test_sync",
    )

    store.set_watermark(
        domain="employee",
        store_key="tomball",
        key="last_success_at",
        value="2026-06-20T14:00:00Z",
    )
    store.record_pull_log(
        domain="employee",
        store_key="tomball",
        scope_start=None,
        scope_end=None,
        started_at="2026-06-20T13:59:00Z",
        ok=True,
        row_count=1,
    )

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM toast_dimension_item").fetchone()[0] == 6
        employee = conn.execute(
            "SELECT chosen_name, last_name FROM toast_employee_current WHERE employee_guid = 'employee-1'"
        ).fetchone()
        assert dict(employee) == {"chosen_name": "Yadira R", "last_name": "Hernandez"}
        table = conn.execute(
            "SELECT name, service_area_guid FROM toast_table_current WHERE table_guid = 'table-101'"
        ).fetchone()
        assert dict(table) == {"name": "101", "service_area_guid": "service-area-main"}
        time_entry = conn.execute(
            """
            SELECT employee_guid, job_guid, business_date, total_hours
            FROM toast_time_entry_current
            WHERE time_entry_guid = 'time-entry-1'
            """
        ).fetchone()
        assert dict(time_entry) == {
            "employee_guid": "employee-1",
            "job_guid": "job-server",
            "business_date": "20260620",
            "total_hours": 4.5,
        }
        shift = conn.execute(
            """
            SELECT employee_guid, job_guid, business_date
            FROM toast_shift_current
            WHERE shift_guid = 'shift-1'
            """
        ).fetchone()
        assert dict(shift) == {
            "employee_guid": "employee-1",
            "job_guid": "job-server",
            "business_date": "20260620",
        }
        assert conn.execute("SELECT COUNT(*) FROM toast_mirror_pull_log").fetchone()[0] == 1

    health = store.health()["counts"]
    assert health["employees"] == 1
    assert health["jobs"] == 1
    assert health["tables"] == 1
    assert health["service_areas"] == 1
    assert health["time_entries"] == 1
    assert health["shifts"] == 1
    assert health["watermarks"] == 1
    assert store.get_watermark(
        domain="employee",
        store_key="tomball",
        key="last_success_at",
    ) == "2026-06-20T14:00:00Z"
