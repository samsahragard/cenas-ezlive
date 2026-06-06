import json
import os
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post(port: int, payload: dict, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/assistant/answer",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    return urllib.request.urlopen(req, timeout=3)


def _principal(role: str = "gm") -> dict:
    return {
        "kind": "staff",
        "role": role,
        "principal_id": 7,
        "display_name": "Test User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/dos/manager",
        "permissions": ["ai.ask_claude", "ai.ask_claude_personal"],
        "can_ask_personal": True,
        "can_ask_operational": role in {"partner", "gm", "km"},
    }


def _available_tool(tool_id: str) -> dict:
    return {
        "tool_id": tool_id,
        "label": tool_id,
        "available": True,
        "status": "active",
        "deny_reason": None,
        "read_write_class": "read_only",
    }


WAVE1_ORDER_READ_TOOL_IDS = [
    "orders.catering_by_status",
    "orders.catering_by_store",
    "orders.catering_count",
    "orders.catering_driver_assignment_summary",
    "orders.catering_fees_summary",
    "orders.catering_item_mix",
    "orders.catering_late_risk",
    "orders.catering_live_tracking",
    "orders.catering_needs_driver",
    "orders.catering_next_30_days",
    "orders.catering_order_items_safe",
    "orders.catering_order_lookup",
    "orders.catering_payout_safe_summary",
    "orders.catering_pdf_status",
    "orders.catering_returning_customers_aggregate",
    "orders.catering_today",
    "orders.catering_tomorrow",
    "orders.catering_tracking_missing",
    "orders.catering_uuid_status",
    "orders.catering_week",
    "orders.in_house_quote_lookup",
    "orders.in_house_quotes_summary",
]


WAVE1_SCHEDULE_READ_TOOL_IDS = [
    "schedule.alarm_pending_summary",
    "schedule.availability_conflicts",
    "schedule.open_shifts",
    "schedule.shift_acceptance_summary",
    "schedule.shift_offer_summary",
    "schedule.shift_swap_summary",
    "schedule.store_today",
    "schedule.store_week",
    "schedule.time_off_pending",
    "schedule.unavailability_blocks",
    "schedule.view",
]


def _wave1_order_runtime_payload(tool_id: str) -> dict:
    base = {
        "ok": True,
        "generated_at": "2026-06-06T12:00:00Z",
        "data_class": "orders_read_sanitized",
    }
    if tool_id in {
        "orders.catering_today",
        "orders.catering_tomorrow",
        "orders.catering_week",
        "orders.catering_next_30_days",
    }:
        return {
            **base,
            "window": "today",
            "count": 1,
            "by_store": {"tomball": 1},
            "orders": [{"external_order_id": "TO-TODAY"}],
        }
    payloads = {
        "orders.catering_by_status": {**base, "by_status": {"approved": 1}},
        "orders.catering_by_store": {**base, "by_store": {"tomball": 1}},
        "orders.catering_count": {
            **base,
            "today_count": 1,
            "tomorrow_count": 1,
            "next_7_days_count": 2,
            "next_30_days_count": 2,
            "total_count": 2,
        },
        "orders.catering_driver_assignment_summary": {
            **base,
            "job_count": 1,
            "by_status": {"completed": 1},
        },
        "orders.catering_fees_summary": {
            **base,
            "delivery_fee_total": 25,
            "tip_total": 20,
            "commission_total": 10,
            "service_fee_total": 5,
            "processing_fee_total": 2.5,
        },
        "orders.catering_item_mix": {
            **base,
            "top_items": [{"label": "fajita_pack", "qty": 2}],
        },
        "orders.catering_late_risk": {
            **base,
            "count": 1,
            "orders": [{"external_order_id": "TO-TODAY"}],
        },
        "orders.catering_live_tracking": {
            **base,
            "count": 1,
            "active_count": 1,
            "by_status": {"en_route": 1},
        },
        "orders.catering_needs_driver": {
            **base,
            "count": 1,
            "orders": [{"external_order_id": "TO-TOMORROW"}],
        },
        "orders.catering_order_items_safe": {
            **base,
            "found": True,
            "order": {"external_order_id": "TO-TODAY"},
            "item_count": 1,
            "items": [{"qty": 2, "label": "fajita_pack"}],
        },
        "orders.catering_order_lookup": {
            **base,
            "found": True,
            "order": {
                "external_order_id": "TO-TODAY",
                "store": "tomball",
                "delivery_date": "2026-06-06",
                "deliver_at": "9:30 AM",
                "status": "approved",
                "headcount": 25,
            },
        },
        "orders.catering_payout_safe_summary": {
            **base,
            "potential_payout_total": 45,
            "paid_payout_total": 35,
            "tip_total": 20,
            "verified_miles_total": 12.5,
        },
        "orders.catering_pdf_status": {
            **base,
            "processing_rows": 1,
            "pdf_detail_rows": 1,
            "with_pdf_source": 1,
            "parse_error_count": 0,
            "by_processing_status": {"completed": 1},
        },
        "orders.catering_returning_customers_aggregate": {
            **base,
            "returning_customer_count": 1,
            "returning_order_count": 2,
        },
        "orders.catering_tracking_missing": {
            **base,
            "count": 1,
            "by_store": {"tomball": 1},
        },
        "orders.catering_uuid_status": {
            **base,
            "with_tracking_uuid": 1,
            "missing_tracking_uuid": 1,
            "active_tracking_count": 1,
        },
        "orders.in_house_quote_lookup": {
            **base,
            "found": True,
            "quote": {
                "quote_id": 7,
                "store": "tomball",
                "status": "sent",
                "event_date": "2026-06-10",
                "guest_count": 20,
                "subtotal": 200,
            },
        },
        "orders.in_house_quotes_summary": {
            **base,
            "quote_count": 1,
            "subtotal_total": 200,
            "by_status": {"sent": 1},
        },
    }
    return payloads[tool_id]


def _wave1_schedule_runtime_payload(tool_id: str) -> dict:
    base = {
        "ok": True,
        "generated_at": "2026-06-06T12:00:00Z",
        "data_class": "schedule_read_sanitized",
    }
    if tool_id == "schedule.store_today":
        return {
            **base,
            "shift_count": 2,
            "assigned_shift_count": 1,
            "open_shift_count": 1,
            "total_hours": 10.5,
            "by_store": {"tomball": 2},
            "shifts": [
                {
                    "start_at": "2026-06-06T09:00:00",
                    "position_name": "Server",
                    "employee_name": "Tomball Server",
                }
            ],
        }
    if tool_id in {"schedule.store_week", "schedule.view"}:
        return {
            **base,
            "schedule_count": 1,
            "shift_count": 3,
            "assigned_shift_count": 2,
            "open_shift_count": 1,
            "total_hours": 18,
            "by_store": {"tomball": 3},
        }
    payloads = {
        "schedule.alarm_pending_summary": {
            **base,
            "pending_count": 2,
            "overdue_count": 1,
            "by_channel": {"sms": 2},
        },
        "schedule.availability_conflicts": {
            **base,
            "conflict_count": 1,
            "by_type": {"unavailability_block": 1},
        },
        "schedule.open_shifts": {
            **base,
            "count": 1,
            "by_store": {"tomball": 1},
            "shifts": [
                {
                    "start_at": "2026-06-07T16:00:00",
                    "position_name": "Cook",
                    "employee_name": None,
                }
            ],
        },
        "schedule.shift_acceptance_summary": {
            **base,
            "assigned_shift_count": 3,
            "response_count": 2,
            "pending_count": 1,
            "by_response": {"accepted": 1, "declined": 1},
        },
        "schedule.shift_offer_summary": {
            **base,
            "offer_count": 2,
            "by_status": {"open": 1, "taken": 1},
            "restricted_count": 2,
        },
        "schedule.shift_swap_summary": {
            **base,
            "swap_count": 1,
            "by_status": {"proposed": 1},
        },
        "schedule.time_off_pending": {
            **base,
            "pending_count": 1,
        },
        "schedule.unavailability_blocks": {
            **base,
            "block_count": 1,
        },
    }
    return payloads[tool_id]


@pytest.mark.parametrize("tool_id", WAVE1_ORDER_READ_TOOL_IDS)
def test_runtime_formats_and_verifies_every_wave1_order_read_tool(tool_id, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("approved order read tool must not call model")),
    )
    principal = _principal("partner")
    principal["kind"] = "partner"
    payload = _wave1_order_runtime_payload(tool_id)

    data = runtime._approved_tool_answer(
        "wave1 order read test",
        "",
        principal,
        [_available_tool(tool_id)],
        {tool_id: payload},
        routed_tool_id=tool_id,
    )

    assert data is not None
    assert data["queued"] is False
    assert data["tool_id"] == tool_id
    assert data["storage"] == "operational_tool"
    assert data["answer"].strip()
    assert runtime._tool_answer_verified(tool_id, payload, data["answer"]) is True


@pytest.mark.parametrize("tool_id", WAVE1_SCHEDULE_READ_TOOL_IDS)
def test_runtime_formats_and_verifies_every_wave1_schedule_read_tool(tool_id, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("approved schedule read tool must not call model")),
    )
    principal = _principal("partner")
    principal["kind"] = "partner"
    payload = _wave1_schedule_runtime_payload(tool_id)

    data = runtime._approved_tool_answer(
        "wave1 schedule read test",
        "",
        principal,
        [_available_tool(tool_id)],
        {tool_id: payload},
        routed_tool_id=tool_id,
    )

    assert data is not None
    assert data["queued"] is False
    assert data["tool_id"] == tool_id
    assert data["storage"] == "operational_tool"
    assert data["answer"].strip()
    assert runtime._tool_answer_verified(tool_id, payload, data["answer"]) is True


def test_runtime_token_gate_and_blocked_question_save(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(runtime, "_gemini_review_notice", lambda *_: (None, None))

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        payload = {
            "question": "Show me customer phone numbers for catering orders",
            "principal": _principal("gm"),
            "tools": [],
            "source": "test",
        }

        try:
            _post(port, payload)
            assert False, "expected token gate to reject missing token"
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

        res = _post(port, payload, token)
        assert res.status == 200
        data = json.loads(res.read().decode("utf-8"))

        assert data["ok"] is True
        assert data["queued"] is True
        assert data["storage"] == "ck"
        assert data["answer"] == "I do not have the approved Cenas data tool for that yet, so I saved it for Sam review."
        assert data["reason"] == "sensitive_or_operational_question_needs_approved_tool"

        import sqlite3

        con = sqlite3.connect(db_path)
        counts = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in [
                "assistant_question",
                "assistant_principal_snapshot",
                "assistant_review_decision",
                "assistant_model_audit",
                "assistant_delivery_attempt",
            ]
        }
        question = con.execute(
            "SELECT status, scope_role, scope_store_key, risk_level FROM assistant_question"
        ).fetchone()
        con.close()

        assert counts == {
            "assistant_question": 1,
            "assistant_principal_snapshot": 1,
            "assistant_review_decision": 1,
            "assistant_model_audit": 1,
            "assistant_delivery_attempt": 1,
        }
        assert question == ("needs_review", "gm", "dos", "blocked")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_operator_catering_count_with_approved_tool(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("approved tool answer must not call model")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "How many caterings do we have today?",
                "principal": principal,
                "tools": [_available_tool("orders.store_summary")],
                "tool_data": {
                    "orders.store_summary": {
                        "generated_at": "2026-06-05T17:00:00Z",
                        "total_orders": 12,
                        "today_orders": 3,
                        "upcoming_orders": 8,
                        "needs_driver_orders": 2,
                        "live_tracking_orders": 1,
                        "by_store": {"copperfield": 2, "tomball": 1},
                    }
                },
                "routed_tool_id": "orders.store_summary",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert res.status == 200
        assert data["ok"] is True
        assert data["queued"] is False
        assert data["storage"] == "operational_tool"
        assert data["tool_id"] == "orders.store_summary"
        assert "3 caterings today" in data["answer"]
        assert "2 still need driver attention" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_records_learning_tool_route_after_repeated_success(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("verified tool route must not call model")),
    )

    principal = _principal("partner")
    principal["kind"] = "partner"
    principal["is_owner_operator"] = False
    payload = {
        "question": "How many caterings do we have today?",
        "principal": principal,
        "tools": [_available_tool("orders.store_summary")],
        "tool_data": {
            "orders.store_summary": {
                "generated_at": "2026-06-05T17:00:00Z",
                "total_orders": 12,
                "today_orders": 3,
                "upcoming_orders": 8,
                "needs_driver_orders": 0,
                "live_tracking_orders": 1,
                "by_store": {"copperfield": 2, "tomball": 1},
            }
        },
        "routed_tool_id": "orders.store_summary",
        "route_path": "deterministic",
        "source": "test",
    }

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        route_states = []
        for _ in range(3):
            res = _post(port, payload, token)
            data = json.loads(res.read().decode("utf-8"))
            assert data["queued"] is False
            assert data["tool_id"] == "orders.store_summary"
            route_states.append(data["route_cache"])

        assert [state["verification_count"] for state in route_states] == [1, 2, 3]
        assert [state["status"] for state in route_states] == ["learning", "learning", "learning"]

        import sqlite3

        con = sqlite3.connect(db_path)
        row = con.execute(
            """
            SELECT tool_id, route_kind, status, verification_count,
                   required_verifications, route_args_redacted,
                   answer_hash, payload_hash
              FROM assistant_verified_tool_route
            """
        ).fetchone()
        event_count = con.execute(
            "SELECT COUNT(*) FROM assistant_route_event WHERE route_path = 'deterministic'"
        ).fetchone()[0]
        con.close()

        assert row[0] == "orders.store_summary"
        assert row[1] == "order_summary"
        assert row[2] == "learning"
        assert row[3] == 3
        assert row[4] == 3
        assert "current_view" in row[5]
        assert row[6] and "3 caterings" not in row[6]
        assert row[7] and "today_orders" not in row[7]
        assert event_count == 3
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_nightly_auto_verify_promotes_aged_learning_route(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    principal = _principal("partner")
    principal["kind"] = "partner"
    approved = {
        "tool_id": "orders.store_summary",
        "answer": "You have 3 caterings today and 8 upcoming orders.",
    }
    tool_data = {
        "orders.store_summary": {
            "today_orders": 3,
            "upcoming_orders": 8,
            "needs_driver_orders": 0,
            "live_tracking_orders": 1,
        }
    }

    for _ in range(3):
        state = runtime._record_tool_route_verification(
            "How many caterings do we have today?",
            "",
            principal,
            approved,
            tool_data,
            "deterministic",
            {"classifier": {"enabled": False}},
        )
        assert state["status"] == "learning"

    old = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    import sqlite3

    con = sqlite3.connect(db_path)
    con.execute("UPDATE assistant_verified_tool_route SET first_seen_at = ?", (old,))
    con.commit()
    con.close()

    result = runtime._auto_verify_tool_routes(min_age_days=7)

    con = sqlite3.connect(db_path)
    status = con.execute("SELECT status FROM assistant_verified_tool_route").fetchone()[0]
    event_type = con.execute(
        "SELECT event_type FROM assistant_route_event WHERE route_path = 'nightly_auto_verify'"
    ).fetchone()[0]
    con.close()

    assert result["promoted"] == 1
    assert status == "verified"
    assert event_type == "auto_verify"


def test_runtime_answers_operator_toast_sales_with_approved_tool(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("approved tool answer must not call model")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "what are Toast sales today?",
                "principal": principal,
                "tools": [_available_tool("toast.sales_summary")],
                "tool_data": {
                    "toast.sales_summary": {
                        "generated_at": "2026-06-05T17:00:00Z",
                        "label": "Today",
                        "scope_note": "2 locations included.",
                        "sales": {
                            "net": 1234.56,
                            "gross": 1300.00,
                            "discount": 10.00,
                            "void": 0.00,
                            "refund": 5.00,
                            "avg_order": 48.41,
                            "sales_per_labor_hour": 111.11,
                            "orders": 26,
                            "guests": 42,
                        },
                        "labor": {
                            "hours": 11.11,
                            "cost": 444.44,
                            "ratio_pct": 36.0,
                            "by_job": [],
                        },
                        "menu": {},
                    }
                },
                "routed_tool_id": "toast.sales_summary",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["storage"] == "toast_analytics_tool"
        assert data["tool_id"] == "toast.sales_summary"
        assert "net sales are $1,234.56" in data["answer"]
        assert "2 locations included" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_operator_toast_table_activity_with_approved_tool(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("approved tool answer must not call model")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "what was the most recent talbe opened in tomball",
                "principal": principal,
                "tools": [_available_tool("toast.table_activity")],
                "tool_data": {
                    "toast.table_activity": {
                        "generated_at": "2026-06-05T23:22:00Z",
                        "location": "tomball",
                        "location_label": "Tomball",
                        "latest": {
                            "location": "tomball",
                            "location_label": "Tomball",
                            "table_name": "106",
                            "opened_at_local": "2026-06-05 6:20 PM CT",
                            "server_name": "Maria Garcia",
                            "table_config_available": True,
                        },
                    }
                },
                "routed_tool_id": "toast.table_activity",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["storage"] == "toast_table_activity_tool"
        assert data["tool_id"] == "toast.table_activity"
        assert "table 106" in data["answer"]
        assert "6:20 PM CT" in data["answer"]
        assert "waiter/server was Maria Garcia" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_operator_toast_webhook_activity_with_approved_tool(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("webhook tool answer must not call model")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "what live Toast webhook events came in today?",
                "principal": principal,
                "tools": [_available_tool("toast.webhook_activity")],
                "tool_data": {
                    "toast.webhook_activity": {
                        "ok": True,
                        "generated_at": "2026-06-06T17:00:00Z",
                        "data_class": "toast_webhook_activity_sanitized",
                        "scope": {"store_key": None, "business_date": "20260606"},
                        "counts": {
                            "events": 10,
                            "orders": 4,
                            "checks": 4,
                            "selections": 9,
                            "payments": 2,
                            "employee_facts": 12,
                        },
                        "recent_last_hour_events": 3,
                        "fact_types_for_scope": [{"fact_type": "item_added", "count": 7}],
                        "latest_orders": [{
                            "table_name": "{'guid': '626c6c44-b022-4475-ab37-43d61379b488', 'entityType': 'Table'}",
                            "server_name": "Maria Garcia",
                            "selection_count": 3,
                            "payment_count": 1,
                            "modified_date": "2026-06-06T16:59:00Z",
                        }],
                        "raw_payloads_included": False,
                    }
                },
                "routed_tool_id": "toast.webhook_activity",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["storage"] == "toast_webhook_activity_tool"
        assert data["tool_id"] == "toast.webhook_activity"
        assert "Toast webhook database is connected" in data["answer"]
        assert "10 webhook events" in data["answer"]
        assert "626c6c44-b022-4475-ab37-43d61379b488" not in data["answer"]
        assert "entityType" not in data["answer"]
        assert "table {" not in data["answer"]
        assert "Raw Toast webhook JSON is not included" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_queues_toast_webhook_when_routed_payload_is_error(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_toast_webhook_activity_payload",
        lambda *_: (_ for _ in ()).throw(AssertionError("runtime must not refetch webhook payload")),
    )
    monkeypatch.setattr(runtime, "_gemini_review_notice", lambda *_: (None, None))

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "what live Toast webhook events came in today?",
                "principal": principal,
                "tools": [_available_tool("toast.webhook_activity")],
                "tool_data": {
                    "toast.webhook_activity": {
                        "ok": False,
                        "error": "toast_webhook_db_missing",
                    }
                },
                "routed_tool_id": "toast.webhook_activity",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is True
        assert data["reason"] == "data_question_needs_approved_tool"
        assert data.get("tool_id") != "toast.webhook_activity"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_operator_toast_employee_profile_with_approved_tool(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("employee profile tool answer must not call model")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "show employee 4 Toast profile facts",
                "principal": principal,
                "tools": [_available_tool("toast.employee_profiles")],
                "tool_data": {
                    "toast.employee_profiles": {
                        "ok": True,
                        "generated_at": "2026-06-06T17:00:00Z",
                        "data_class": "toast_employee_profiles_sanitized",
                        "scope": "employee",
                        "employee": {"cena_employee_id": 4, "name": "Natalie Allen"},
                        "personal_db": {
                            "exists": True,
                            "toast_fact_count": 24,
                            "related_orders": 4,
                            "related_checks": 4,
                            "related_selections": 8,
                            "related_payments": 2,
                            "metadata": {"generated_at": "2026-06-06T16:59:00Z"},
                            "fact_type_counts": [{"fact_type": "item_added", "count": 8}],
                            "latest_facts": [{
                                "fact_type": "item_added",
                                "occurred_at": "2026-06-06T16:59:00Z",
                                "summary": {
                                    "name": "Smoke Item",
                                    "table": "{'guid': '626c6c44-b022-4475-ab37-43d61379b488'}",
                                },
                            }],
                        },
                        "raw_payloads_included": False,
                    }
                },
                "routed_tool_id": "toast.employee_profiles",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["storage"] == "toast_employee_profiles_tool"
        assert data["tool_id"] == "toast.employee_profiles"
        assert "Natalie Allen (employee 4)" in data["answer"]
        assert "24 facts" in data["answer"]
        assert "626c6c44-b022-4475-ab37-43d61379b488" not in data["answer"]
        assert "table {" not in data["answer"]
        assert "Raw webhook JSON is not included" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_queues_toast_employee_profiles_when_routed_payload_is_error(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_toast_employee_profiles_payload",
        lambda *_: (_ for _ in ()).throw(AssertionError("runtime must not refetch employee profile payload")),
    )
    monkeypatch.setattr(runtime, "_gemini_review_notice", lambda *_: (None, None))

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "show Toast employee profile databases",
                "principal": principal,
                "tools": [_available_tool("toast.employee_profiles")],
                "tool_data": {
                    "toast.employee_profiles": {
                        "ok": False,
                        "error": "toast_webhook_db_missing",
                    }
                },
                "routed_tool_id": "toast.employee_profiles",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is True
        assert data["reason"] == "data_question_needs_approved_tool"
        assert data.get("tool_id") != "toast.employee_profiles"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_does_not_use_toast_payload_when_tool_unavailable(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(runtime, "_gemini_review_notice", lambda *_: (None, None))

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "what live Toast webhook events came in today?",
                "principal": principal,
                "tools": [{"tool_id": "toast.webhook_activity", "available": False, "deny_reason": "needs_sam_review"}],
                "tool_data": {
                    "toast.webhook_activity": {
                        "ok": True,
                        "counts": {"events": 9999},
                        "raw_payloads_included": False,
                    }
                },
                "routed_tool_id": "toast.webhook_activity",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is True
        assert data["storage"] == "ck"
        assert data.get("tool_id") != "toast.webhook_activity"
        assert "9999" not in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_table_waiter_followup_with_previous_question(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("table follow-up must use tool answer")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        res = _post(
            port,
            {
                "question": "who was the waiter",
                "previous_question": "who opened the last table and what time",
                "principal": principal,
                "tools": [_available_tool("toast.table_activity")],
                "tool_data": {
                    "toast.table_activity": {
                        "generated_at": "2026-06-06T00:55:00Z",
                        "location": "tomball",
                        "location_label": "Tomball",
                        "latest": {
                            "location": "tomball",
                            "location_label": "Tomball",
                            "table_name": "311",
                            "opened_at_local": "2026-06-05 7:54 PM CT",
                            "server_name": "Maria Garcia",
                            "table_config_available": True,
                        },
                    }
                },
                "routed_tool_id": "toast.table_activity",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["tool_id"] == "toast.table_activity"
        assert "table 311" in data["answer"]
        assert "7:54 PM CT" in data["answer"]
        assert "waiter/server was Maria Garcia" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_queues_stale_table_payload_without_waiter(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(runtime, "_gemini_review_notice", lambda *_: (None, None))
    monkeypatch.setattr(
        runtime,
        "_toast_table_activity_payload",
        lambda *_, **__: (_ for _ in ()).throw(AssertionError("runtime must not refetch table payload")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        res = _post(
            port,
            {
                "question": "who was the waiter",
                "previous_question": "who opened the last table and what time",
                "principal": principal,
                "tools": [_available_tool("toast.table_activity")],
                "tool_data": {
                    "toast.table_activity": {
                        "generated_at": "2026-06-06T01:04:00Z",
                        "location": "all",
                        "location_label": "all locations",
                        "latest": {
                            "table_name": "104",
                            "opened_at_local": "2026-06-05 8:05 PM CT",
                            "table_config_available": True,
                        },
                    }
                },
                "routed_tool_id": "toast.table_activity",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is True
        assert data["reason"] == "data_question_needs_approved_tool"
        assert data.get("tool_id") != "toast.table_activity"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_anchors_table_waiter_followup_to_previous_answer(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("anchored table follow-up must not call model")),
    )
    monkeypatch.setattr(
        runtime,
        "_toast_table_activity_payload",
        lambda *_, **__: (_ for _ in ()).throw(AssertionError("anchored table follow-up must not refetch")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        res = _post(
            port,
            {
                "question": "who was the waiter",
                "previous_question": "who opened the last table and what time",
                "previous_answer": (
                    "The most recent all locations in-store table open I see is "
                    "table R2, opened at 2026-06-05 8:07 PM CT. "
                    "The waiter/server was Alexa Rodriguez."
                ),
                "principal": principal,
                "tools": [_available_tool("toast.table_activity")],
                "tool_data": {},
                "routed_tool_id": "toast.table_activity",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["tool_id"] == "toast.table_activity"
        assert data["storage"] == "toast_table_activity_context"
        assert data["answer"] == (
            "The waiter/server was Alexa Rodriguez for table R2, "
            "opened at 2026-06-05 8:07 PM CT."
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_formats_routed_last_night_table_payload_with_waiter(monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    principal = _principal("partner")
    principal["kind"] = "partner"
    tools = [_available_tool("toast.table_activity")]
    monkeypatch.setattr(
        runtime,
        "_toast_table_activity_payload",
        lambda *_, **__: (_ for _ in ()).throw(AssertionError("runtime must not fetch table payload")),
    )

    data = runtime._approved_tool_answer(
        "who opened the last table last night?",
        "",
        principal,
        tools,
        {
            "toast.table_activity": {
                "generated_at": "2026-06-06T01:05:00Z",
                "location": "all",
                "business_date": runtime._toast_table_business_date_from_question("last night"),
                "location_label": "all locations",
                "latest": {
                    "table_name": "82",
                    "opened_at_local": "2026-06-05 7:33 PM CT",
                    "opened_by_name": "Yadira Flores",
                    "server_name": "Yadira Flores",
                    "employee_lookup_available": True,
                    "table_config_available": True,
                },
            }
        },
        routed_tool_id="toast.table_activity",
    )

    assert data is not None
    assert data["queued"] is False
    assert data["tool_id"] == "toast.table_activity"
    assert "table 82" in data["answer"]
    assert "2026-06-05 7:33 PM CT" in data["answer"]
    assert "opened by Yadira Flores" in data["answer"]


def test_runtime_queues_server_only_payload_for_opened_by_question(monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    principal = _principal("partner")
    principal["kind"] = "partner"
    tools = [_available_tool("toast.table_activity")]
    monkeypatch.setattr(
        runtime,
        "_toast_table_activity_payload",
        lambda *_, **__: (_ for _ in ()).throw(AssertionError("runtime must not refresh table payload")),
    )

    data = runtime._approved_tool_answer(
        "who opened the last table?",
        "",
        principal,
        tools,
        {
            "toast.table_activity": {
                "business_date": runtime._today_ct().strftime("%Y%m%d"),
                "location_label": "all locations",
                "latest": {
                    "table_name": "82",
                    "opened_at_local": "2026-06-05 7:33 PM CT",
                    "server_name": "Maria Garcia",
                    "employee_lookup_available": True,
                    "table_config_available": True,
                },
            }
        },
        routed_tool_id="toast.table_activity",
    )

    assert data is None


def test_runtime_bare_waiter_question_routes_to_table_tool(monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("waiter question must use table tool")),
    )
    principal = _principal("partner")
    principal["kind"] = "partner"
    tools = [_available_tool("toast.table_activity")]

    data = runtime._approved_tool_answer(
        "who was the waiter?",
        "",
        principal,
        tools,
        {
            "toast.table_activity": {
                "business_date": runtime._today_ct().strftime("%Y%m%d"),
                "location_label": "all locations",
                "latest": {
                    "table_name": "311",
                    "opened_at_local": "2026-06-05 7:54 PM CT",
                    "server_name": "Maria Garcia",
                    "employee_lookup_available": True,
                    "table_config_available": True,
                },
            }
        },
        routed_tool_id="toast.table_activity",
    )

    assert data is not None
    assert data["tool_id"] == "toast.table_activity"
    assert "waiter/server was Maria Garcia" in data["answer"]


def test_runtime_answers_operator_catering_followup_with_previous_question(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("follow-up tool answer must not call model")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "what baout earlier this morning?",
                "previous_question": "how amny caterings do we have today?",
                "principal": principal,
                "tools": [_available_tool("orders.store_summary")],
                "tool_data": {
                    "orders.store_summary": {
                        "today": "2026-06-05",
                        "today_orders": 3,
                        "upcoming_orders": 8,
                        "needs_driver_orders": 0,
                        "live_tracking_orders": 0,
                        "today_time_windows": {
                            "morning": 2,
                            "afternoon": 1,
                            "evening": 0,
                            "earlier_today": 2,
                            "unknown_time": 0,
                        },
                        "today_time_windows_by_store": {
                            "morning": {"copperfield": 1, "tomball": 1}
                        },
                        "today_by_store": {"copperfield": 2, "tomball": 1},
                    }
                },
                "routed_tool_id": "orders.store_summary",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert res.status == 200
        assert data["queued"] is False
        assert data["storage"] == "operational_tool"
        assert data["tool_id"] == "orders.store_summary"
        assert "earlier this morning (2026-06-05)" in data["answer"]
        assert "2 caterings" in data["answer"]
        assert "copperfield: 1" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_operator_driver_summary_with_approved_tool(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(runtime, "_gemini_review_notice", lambda *_: (None, None))

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "How many active drivers do we have?",
                "principal": principal,
                "tools": [_available_tool("drivers.store_summary")],
                "tool_data": {
                    "drivers.store_summary": {
                        "total_drivers": 5,
                        "active_drivers": 5,
                        "drivers_on_shift": 3,
                        "drivers_on_active_orders": 2,
                        "average_score": 100.0,
                        "by_store": {"copperfield": 5},
                    }
                },
                "routed_tool_id": "drivers.store_summary",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["storage"] == "operational_tool"
        assert data["tool_id"] == "drivers.store_summary"
        assert "5 drivers" in data["answer"]
        assert "Average current score is 100.0" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_owner_tool_matcher_covers_live_sweep_phrases():
    from scripts import assistant_ck_runtime as runtime

    principal = _principal("partner")
    principal["kind"] = "partner"
    principal["is_owner_operator"] = True
    tools = [
        _available_tool("orders.store_summary"),
        _available_tool("drivers.store_summary"),
        _available_tool("labor.store_aggregate"),
    ]
    tool_data = {
        "orders.store_summary": {
            "today_orders": 4,
            "upcoming_orders": 12,
            "needs_driver_orders": 17,
            "live_tracking_orders": 2,
        },
        "drivers.store_summary": {
            "total_drivers": 45,
            "active_drivers": 25,
            "drivers_on_shift": 0,
            "drivers_on_active_orders": 3,
            "average_score": 100.0,
        },
        "labor.store_aggregate": {
            "total_employees": 119,
            "active_employees": 95,
            "published_shifts": 2323,
            "open_shifts": 0,
        },
    }

    cases = {
        "caterings today?": "orders.store_summary",
        "what caterings are today": "orders.store_summary",
        "how many active tracking links": "orders.store_summary",
        "what is the store split for caterings today": "orders.store_summary",
        "do we have caterings today": "orders.store_summary",
        "today order totals": "orders.store_summary",
        "orders needing driver attention": "orders.store_summary",
        "current drivers by location": "drivers.store_summary",
        "do we have drivers on shift": "drivers.store_summary",
        "show me the driver aggregate": "drivers.store_summary",
        "driver coverage today": "drivers.store_summary",
        "current staffing summary": "labor.store_aggregate",
    }

    for question, expected_tool in cases.items():
        data = runtime._approved_tool_answer(
            question,
            "",
            principal,
            tools,
            tool_data,
            routed_tool_id=expected_tool,
        )
        assert data is not None, question
        assert data["queued"] is False
        assert data["storage"] == "operational_tool"
        assert data["tool_id"] == expected_tool


def test_runtime_formats_registry_routed_tool_without_re_matching():
    from scripts import assistant_ck_runtime as runtime

    principal = _principal("partner")
    principal["kind"] = "partner"
    tools = [
        _available_tool("orders.store_summary"),
        _available_tool("drivers.store_summary"),
    ]
    tool_data = {
        "orders.store_summary": {
            "today_orders": 99,
            "upcoming_orders": 99,
        },
        "drivers.store_summary": {
            "total_drivers": 5,
            "active_drivers": 4,
            "drivers_on_shift": 3,
            "drivers_on_active_orders": 2,
            "average_score": 100.0,
        },
    }

    data = runtime._approved_tool_answer(
        "how many caterings today?",
        "",
        principal,
        tools,
        tool_data,
        routed_tool_id="drivers.store_summary",
    )

    assert data is not None
    assert data["queued"] is False
    assert data["tool_id"] == "drivers.store_summary"
    assert "5 drivers" in data["answer"]
    assert "99" not in data["answer"]


def test_runtime_requires_explicit_registry_route_for_tool_data():
    from scripts import assistant_ck_runtime as runtime

    principal = _principal("partner")
    principal["kind"] = "partner"
    principal["is_owner_operator"] = True
    tools = [_available_tool("orders.store_summary")]
    tool_data = {
        "orders.store_summary": {
            "today_orders": 4,
            "upcoming_orders": 12,
            "needs_driver_orders": 0,
            "live_tracking_orders": 2,
        },
    }

    data = runtime._approved_tool_answer("caterings today?", "", principal, tools, tool_data)

    assert data is None


def test_runtime_owner_identity_uses_authenticated_session_context(monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("session context answer must not call model")),
    )
    principal = _principal("partner")
    principal["kind"] = "partner"
    principal["is_owner_operator"] = True

    data = runtime._approved_tool_answer("i am sam.", "", principal, [], {})

    assert data is not None
    assert data["queued"] is False
    assert data["storage"] == "session_context"
    assert "already marked as an owner-operator session" in data["answer"]


def test_runtime_partner_level_uses_approved_tool_without_owner_flag(monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    principal = _principal("partner")
    principal["kind"] = "partner"
    principal["is_owner_operator"] = False
    tools = [_available_tool("orders.store_summary")]
    tool_data = {
        "orders.store_summary": {
            "today_orders": 4,
            "upcoming_orders": 12,
            "needs_driver_orders": 0,
            "live_tracking_orders": 2,
        },
    }

    data = runtime._approved_tool_answer(
        "caterings today?",
        "",
        principal,
        tools,
        tool_data,
        routed_tool_id="orders.store_summary",
    )

    assert data is not None
    assert data["queued"] is False
    assert data["storage"] == "operational_tool"
    assert data["tool_id"] == "orders.store_summary"


def test_runtime_answers_wave1_catering_today_with_explicit_route(monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    principal = _principal("partner")
    principal["kind"] = "partner"
    tools = [_available_tool("orders.catering_today")]
    tool_data = {
        "orders.catering_today": {
            "ok": True,
            "count": 2,
            "window": "today",
            "by_store": {"tomball": 1, "copperfield": 1},
            "orders": [
                {"external_order_id": "TO-1"},
                {"external_order_id": "TO-2"},
            ],
            "generated_at": "2026-06-06T12:00:00Z",
        },
    }

    data = runtime._approved_tool_answer(
        "what caterings are today?",
        "",
        principal,
        tools,
        tool_data,
        routed_tool_id="orders.catering_today",
    )

    assert data is not None
    assert data["queued"] is False
    assert data["tool_id"] == "orders.catering_today"
    assert "2 caterings" in data["answer"]
    assert "TO-1" in data["answer"]


def test_runtime_answers_wave1_order_items_with_explicit_route(monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    principal = _principal("partner")
    principal["kind"] = "partner"
    tools = [_available_tool("orders.catering_order_items_safe")]
    tool_data = {
        "orders.catering_order_items_safe": {
            "ok": True,
            "found": True,
            "order": {"external_order_id": "TO-ITEM"},
            "item_count": 2,
            "items": [
                {"qty": 2, "label": "fajita_pack"},
                {"qty": 1, "label": "queso"},
            ],
            "generated_at": "2026-06-06T12:00:00Z",
        },
    }

    data = runtime._approved_tool_answer(
        "what was on order TO-ITEM?",
        "",
        principal,
        tools,
        tool_data,
        routed_tool_id="orders.catering_order_items_safe",
    )

    assert data is not None
    assert data["queued"] is False
    assert data["tool_id"] == "orders.catering_order_items_safe"
    assert "TO-ITEM" in data["answer"]
    assert "fajita_pack" in data["answer"]


def test_runtime_wave1_order_routes_are_verifiable():
    from scripts import assistant_ck_runtime as runtime

    payload = {
        "ok": True,
        "count": 1,
        "window": "today",
        "orders": [{"external_order_id": "TO-1"}],
    }

    assert "orders.catering_today" in runtime._VERIFIED_ROUTE_TOOL_IDS
    route_kind, route_args = runtime._route_args("orders.catering_today", "what caterings are today")
    assert route_kind == "catering_today"
    assert route_args["tool"] == "orders.catering_today"
    assert runtime._tool_answer_verified(
        "orders.catering_today",
        payload,
        "There is 1 catering in the today view.",
    )


def test_runtime_wave1_schedule_routes_are_verifiable():
    from scripts import assistant_ck_runtime as runtime

    payload = {
        "ok": True,
        "shift_count": 1,
        "assigned_shift_count": 1,
        "open_shift_count": 0,
        "total_hours": 6,
    }

    assert "schedule.store_today" in runtime._VERIFIED_ROUTE_TOOL_IDS
    route_kind, route_args = runtime._route_args("schedule.store_today", "what is today's schedule")
    assert route_kind == "store_today"
    assert route_args["tool"] == "schedule.store_today"
    assert route_args["window"] == "today"
    assert runtime._tool_answer_verified(
        "schedule.store_today",
        payload,
        "Today's schedule has 1 shift: 1 assigned, 0 open, 6 hours.",
    )


def test_runtime_partner_identity_does_not_claim_owner_operator(monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("session context answer must not call model")),
    )
    principal = _principal("partner")
    principal["kind"] = "partner"
    principal["is_owner_operator"] = False

    data = runtime._approved_tool_answer("i am sam.", "", principal, [], {})

    assert data is not None
    assert data["queued"] is False
    assert data["storage"] == "session_context"
    assert "partner-level" in data["answer"]
    assert "owner-operator" not in data["answer"]


def test_runtime_tool_discovery_reports_partner_catalog(monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("tool discovery must not call model")),
    )
    principal = _principal("partner")
    principal["kind"] = "partner"
    principal["is_owner_operator"] = False
    tools = [
        _available_tool("orders.store_summary"),
        {
            "tool_id": "read_file",
            "label": "Read file",
            "available": True,
            "implementation_status": "catalog_only",
        },
    ]

    data = runtime._approved_tool_answer("what tools are available?", "", principal, tools, {})

    assert data is not None
    assert data["queued"] is False
    assert data["storage"] == "tool_catalog"
    assert data["tool_id"] == "assistant.tool_discovery"
    assert "2 active Cenas AI catalog tools" in data["answer"]
    assert "1 are wired" in data["answer"]
    assert "1 are partner catalog entries" in data["answer"]


def test_runtime_answers_operator_labor_summary_with_approved_tool(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("partner")
        principal["kind"] = "partner"
        principal["is_owner_operator"] = True
        res = _post(
            port,
            {
                "question": "Give me a labor and employee summary",
                "principal": principal,
                "tools": [_available_tool("labor.store_aggregate")],
                "tool_data": {
                    "labor.store_aggregate": {
                        "total_employees": 95,
                        "active_employees": 91,
                        "published_shifts": 22,
                        "open_shifts": 4,
                        "last30_cached_hours": 1234.5,
                        "today_attendance_statuses": {"clocked-in": 12, "late": 1},
                    }
                },
                "routed_tool_id": "labor.store_aggregate",
                "route_path": "deterministic",
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert data["queued"] is False
        assert data["storage"] == "operational_tool"
        assert data["tool_id"] == "labor.store_aggregate"
        assert "95 employees" in data["answer"]
        assert "1234.5 hours" in data["answer"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_missing_permission_keeps_permission_wording(tmp_path, monkeypatch):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(runtime, "_gemini_review_notice", lambda *_: (None, None))

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        principal = _principal("expo")
        principal["can_ask_personal"] = False
        principal["can_ask_operational"] = False
        principal["permissions"] = []
        res = _post(
            port,
            {
                "question": "How do I move around this page?",
                "principal": principal,
                "tools": [],
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert res.status == 200
        assert data["queued"] is True
        assert data["answer"].startswith("I can't safely answer")
        assert data["reason"] == "missing_ai_permission"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_answers_general_question_from_ck_model(monkeypatch, tmp_path):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda question, principal: ("Use the Orders tab to review catering requests.", "gemini-2.5-flash"),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        res = _post(
            port,
            {
                "question": "How do I move around this page?",
                "principal": _principal("gm"),
                "tools": [],
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert res.status == 200
        assert data == {
            "ok": True,
            "answer": "Use the Orders tab to review catering requests.",
            "queued": False,
            "model": "gemini-2.5-flash",
            "storage": "ck_runtime",
            "route_path": "general",
            "routed_tool_id": None,
        }
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)


def test_runtime_queues_when_gemini_model_errors(monkeypatch, tmp_path):
    from scripts import assistant_ck_runtime as runtime

    db_path = tmp_path / "assistant_review.sqlite"
    token = "runtime-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_RUNTIME_TOKEN", token)
    monkeypatch.setattr(
        runtime,
        "_gemini_answer",
        lambda question, principal: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), runtime.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        res = _post(
            port,
            {
                "question": "How do I move around this page?",
                "principal": _principal("gm"),
                "tools": [],
                "source": "test",
            },
            token,
        )
        data = json.loads(res.read().decode("utf-8"))

        assert res.status == 200
        assert data["ok"] is True
        assert data["queued"] is True
        assert data["storage"] == "ck"
        assert data["reason"] == "model_unavailable_or_no_answer"
        assert data["answer"] == "I saved that for Sam review. The assistant model is not available right now."
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_RUNTIME_TOKEN", None)
