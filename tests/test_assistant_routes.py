import json
import os
import socket
import threading
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer

from flask import Flask
from sqlalchemy.orm import sessionmaker

from app.models import (
    AttendanceShift,
    Driver,
    Employee,
    EmployeeStoreAssignment,
    Order,
    PerfPeriodCache,
    SamChatMessage,
    SamChatSession,
    Schedule,
    Shift,
)
from app.services.assistant_tool_inventory import PARTNER_TOOL_IDS
from app.web import assistant_routes as ar


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_tool_catalog_only_general_help_active_for_operational_role():
    ctx = {
        "kind": "staff",
        "role": "gm",
        "permissions": [
            "ai.ask_claude",
            "ai.ask_claude_personal",
            "orders.view",
            "drivers.view_roster",
            "labor.view_store_summary",
        ],
    }

    tools = {tool["tool_id"]: tool for tool in ar._tool_catalog_for(ctx)}

    assert tools["assistant.general_help"]["available"] is True
    assert tools["orders.store_summary"]["available"] is False
    assert tools["orders.store_summary"]["deny_reason"] == "needs_sam_review"
    assert tools["drivers.store_summary"]["available"] is False
    assert tools["labor.store_aggregate"]["available"] is False


def test_tool_catalog_respects_missing_permission():
    ctx = {
        "kind": "staff",
        "role": "expo",
        "permissions": ["ai.ask_claude_personal"],
    }

    tools = {tool["tool_id"]: tool for tool in ar._tool_catalog_for(ctx)}

    assert tools["assistant.general_help"]["available"] is True
    assert tools["orders.store_summary"]["available"] is False
    assert tools["orders.store_summary"]["deny_reason"] == "missing_permission"


def test_tool_catalog_activates_operator_tools_for_sam_or_masood(monkeypatch):
    monkeypatch.setenv("AI_ASSISTANT_OPERATOR_USER_IDS", "1, 2")
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 2,
        "display_name": "Masood Sahragard",
        "permissions": ["*"],
        "is_owner_operator": True,
    }

    tools = {tool["tool_id"]: tool for tool in ar._tool_catalog_for(ctx)}

    assert tools["assistant.general_help"]["available"] is True
    assert tools["orders.store_summary"]["available"] is True
    assert tools["orders.store_summary"]["status"] == "active"
    assert tools["orders.store_summary"]["deny_reason"] is None
    assert tools["drivers.store_summary"]["available"] is True
    assert tools["toast.sales_summary"]["available"] is True
    assert tools["toast.sales_summary"]["status"] == "active"
    assert tools["toast.table_activity"]["available"] is True
    assert tools["toast.table_activity"]["status"] == "active"


def test_tool_catalog_activates_approved_tools_for_partner_level():
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "permissions": ["*"],
        "is_owner_operator": False,
    }

    tools = {tool["tool_id"]: tool for tool in ar._tool_catalog_for(ctx)}

    assert tools["assistant.general_help"]["available"] is True
    assert tools["orders.store_summary"]["available"] is True
    assert tools["orders.store_summary"]["status"] == "active"
    assert tools["drivers.store_summary"]["available"] is True
    assert tools["labor.store_aggregate"]["available"] is True
    assert tools["toast.sales_summary"]["available"] is True
    assert tools["toast.table_activity"]["available"] is True
    assert tools["employee.my_profile"]["available"] is False
    assert tools["employee.my_profile.read"]["available"] is True
    assert tools["read_file"]["available"] is True
    assert tools["render_env_set"]["available"] is True
    assert tools["sql_query"]["available"] is True
    assert tools["finance.pnl_summary"]["available"] is True
    assert tools["orders.assign_driver"]["available"] is True
    assert tools["dev.assistant_tool_catalog_snapshot"]["available"] is True
    assert tools["read_file"]["implementation_status"] == "catalog_only"
    assert sum(1 for tool in tools.values() if tool["available"]) >= len(PARTNER_TOOL_IDS)


def test_partner_catalog_only_tools_do_not_activate_for_staff():
    ctx = {
        "kind": "staff",
        "role": "gm",
        "permissions": [
            "ai.ask_claude",
            "ai.ask_claude_personal",
            "orders.view",
            "drivers.view_roster",
            "labor.view_store_summary",
        ],
    }

    tools = {tool["tool_id"]: tool for tool in ar._tool_catalog_for(ctx)}

    assert tools["read_file"]["available"] is False
    assert tools["read_file"]["deny_reason"] == "session_type_not_allowed"
    assert tools["finance.pnl_summary"]["available"] is False
    assert tools["dev.assistant_tool_catalog_snapshot"]["available"] is False


def test_assistant_turn_mirror_writes_cena_review_chat(db_session, monkeypatch):
    test_session_factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(ar, "SessionLocal", test_session_factory)
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 1,
        "display_name": "Sam",
        "store_slugs": ["tomball", "copperfield"],
        "current_store": "tomball",
        "path": "/partner/",
        "permissions": ["*"],
        "is_owner_operator": True,
        "can_ask_personal": True,
        "can_ask_operational": True,
    }

    ar._mirror_assistant_turn_to_cena_chat(
        ctx,
        "who opened the last table and what time",
        {
            "ok": True,
            "answer": "Table 311 was opened at 7:54 PM CT by Test Waiter.",
            "queued": False,
            "model": "gemini-2.5-flash",
            "tool_id": "toast.table_activity",
        },
        200,
        previous_question="what was the last table opened",
        previous_answer="Table 311 was opened at 7:54 PM CT.",
    )

    session_row = (
        db_session.query(SamChatSession)
        .filter(SamChatSession.title == "Cenas AI Review")
        .one()
    )
    message = (
        db_session.query(SamChatMessage)
        .filter(SamChatMessage.session_id == session_row.id)
        .one()
    )
    assert message.role == "system"
    assert message.model == "assistant-review-mirror"
    assert "Cenas AI assistant review" in message.content
    assert "Name: Sam" in message.content
    assert "Role: partner" in message.content
    assert "permissions: *" in message.content
    assert "Question:\nwho opened the last table and what time" in message.content
    assert "Table 311 was opened at 7:54 PM CT by Test Waiter." in message.content

    ar._mirror_assistant_turn_to_cena_chat(
        ctx,
        "who was the waiter",
        {"ok": False, "error": "assistant_unavailable"},
        503,
    )

    assert (
        db_session.query(SamChatSession)
        .filter(SamChatSession.title == "Cenas AI Review")
        .count()
    ) == 1
    assert (
        db_session.query(SamChatMessage)
        .filter(SamChatMessage.session_id == session_row.id)
        .count()
    ) == 2


def test_operator_order_summary_tool_payload_is_sanitized(db_session, monkeypatch):
    today = date.today()
    now = datetime.utcnow()
    db_session.add_all([
        Order(
            external_order_id="TO-1",
            delivery_date=today.isoformat(),
            delivery_window_start=datetime(today.year, today.month, today.day, 9, 30),
            origin_store_id="copperfield",
            status="approved",
            delivery_tracking_id="track-1",
            ezcater_status_key="en_route",
            customer_phone="713-555-1212",
            delivery_address="123 Private St",
            client="Private Customer",
        ),
        Order(
            external_order_id="TO-2",
            delivery_date=(today + timedelta(days=1)).isoformat(),
            origin_store_id="tomball",
            status="new",
            customer_phone="713-555-9999",
            delivery_address="456 Hidden Ave",
            client="Secret Co",
        ),
        Driver(
            name="TD Test",
            location="copperfield",
            active=True,
            status="active",
            current_score=100,
            home_store_id="copperfield",
        ),
        Employee(
            id=101,
            full_name="Yadira Reference",
            active=True,
        ),
        EmployeeStoreAssignment(employee_id=101, store_key="copperfield"),
        Schedule(id=201, store_key="copperfield", week_start=today, status="published"),
        Shift(
            schedule_id=201,
            employee_id=101,
            start_at=now,
            end_at=now + timedelta(hours=6),
            status="assigned",
        ),
        AttendanceShift(
            store_scope="copperfield",
            entry_date=today,
            employee_name="Yadira Reference",
            section="foh",
            status="clocked-in",
        ),
        PerfPeriodCache(
            cena_employee_id=101,
            period="last30",
            store_key="copperfield",
            total_hours=40.0,
        ),
    ])
    db_session.commit()
    monkeypatch.setattr(ar, "SessionLocal", lambda: db_session)

    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 1,
        "display_name": "Sam Sahragard",
        "store_slugs": ["tomball", "copperfield"],
        "current_store": None,
        "path": "/partner/catering",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": True,
    }

    payload = ar._approved_tool_data("How many caterings today?", ctx)
    encoded = json.dumps(payload, sort_keys=True).lower()

    summary = payload["orders.store_summary"]
    assert summary["total_orders"] == 2
    assert summary["today_orders"] == 1
    assert summary["upcoming_orders"] == 2
    assert summary["today_time_windows"]["morning"] == 1
    assert summary["today_time_windows_by_store"]["morning"]["copperfield"] == 1
    assert summary["needs_driver_orders"] == 1
    assert summary["live_tracking_orders"] == 1
    assert payload["drivers.store_summary"]["total_drivers"] == 1
    assert payload["drivers.store_summary"]["average_score"] == 100.0
    assert payload["labor.store_aggregate"]["active_employees"] == 1
    assert payload["labor.store_aggregate"]["published_shifts"] == 1
    assert payload["labor.store_aggregate"]["last30_cached_hours"] == 40.0
    assert "713-555" not in encoded
    assert "private" not in encoded
    assert "secret co" not in encoded


def test_operator_toast_summary_tool_payload_is_sanitized(monkeypatch):
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 1,
        "display_name": "Sam Sahragard",
        "store_slugs": ["tomball", "copperfield"],
        "current_store": None,
        "path": "/partner/",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": True,
    }
    monkeypatch.setattr(ar, "_orders_store_summary", lambda ctx: {"total_orders": 0})
    monkeypatch.setattr(ar, "_drivers_store_summary", lambda ctx: {"total_drivers": 0})
    monkeypatch.setattr(ar, "_labor_store_aggregate", lambda ctx: {"total_employees": 0})
    monkeypatch.setattr(
        ar,
        "_toast_sales_summary_tool_payload",
        lambda period: {
            "period": period,
            "label": "Today",
            "scope_note": "2 locations included.",
            "sales": {"net": 123.45, "orders": 3},
            "labor": {"hours": 4.5, "cost": 67.89},
            "menu": {},
        },
    )

    payload = ar._approved_tool_data("what are Toast sales today?", ctx)

    assert payload["toast.sales_summary"]["period"] == "today"
    assert payload["toast.sales_summary"]["sales"] == {"net": 123.45, "orders": 3}


def test_operator_toast_table_activity_payload_handles_typo(monkeypatch):
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 1,
        "display_name": "Sam Sahragard",
        "store_slugs": ["tomball", "copperfield"],
        "current_store": None,
        "path": "/partner/",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": True,
    }
    monkeypatch.setattr(ar, "_orders_store_summary", lambda ctx: {"total_orders": 0})
    monkeypatch.setattr(ar, "_drivers_store_summary", lambda ctx: {"total_drivers": 0})
    monkeypatch.setattr(ar, "_labor_store_aggregate", lambda ctx: {"total_employees": 0})
    monkeypatch.setattr(
        ar,
        "_toast_table_activity_tool_payload",
        lambda location, business_date=None: {
            "location": location,
            "business_date": business_date,
            "latest": {
                "location_label": "Tomball",
                "table_name": "106",
                "opened_at_local": "2026-06-05 6:20 PM CT",
            },
        },
    )

    payload = ar._approved_tool_data("what was the most recent talbe opened in tomball", ctx)

    assert payload["toast.table_activity"]["location"] == "tomball"
    assert payload["toast.table_activity"]["latest"]["table_name"] == "106"
    assert "toast.sales_summary" not in payload


def test_operator_toast_table_activity_payload_uses_last_night_date(monkeypatch):
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 1,
        "display_name": "Sam Sahragard",
        "store_slugs": ["tomball", "copperfield"],
        "current_store": None,
        "path": "/partner/",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": True,
    }
    seen = {}
    monkeypatch.setattr(ar, "_orders_store_summary", lambda ctx: {"total_orders": 0})
    monkeypatch.setattr(ar, "_drivers_store_summary", lambda ctx: {"total_drivers": 0})
    monkeypatch.setattr(ar, "_labor_store_aggregate", lambda ctx: {"total_employees": 0})
    monkeypatch.setattr(
        ar,
        "_toast_table_activity_tool_payload",
        lambda location, business_date=None: seen.update({
            "location": location,
            "business_date": business_date,
        }) or {"location": location, "business_date": business_date, "latest": None},
    )

    payload = ar._approved_tool_data("who opened the last table last night?", ctx)

    assert "toast.table_activity" in payload
    assert seen["location"] is None
    assert seen["business_date"] == ar._toast_table_business_date_from_question("last night")
    assert seen["business_date"] != ar._today_ct().strftime("%Y%m%d")


def test_partner_level_tool_payloads_do_not_require_owner_operator(monkeypatch):
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["tomball", "copperfield"],
        "current_store": None,
        "path": "/partner/",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }
    monkeypatch.setattr(ar, "_orders_store_summary", lambda ctx: {"total_orders": 0})
    monkeypatch.setattr(ar, "_drivers_store_summary", lambda ctx: {"total_drivers": 0})
    monkeypatch.setattr(ar, "_labor_store_aggregate", lambda ctx: {"total_employees": 0})
    monkeypatch.setattr(
        ar,
        "_toast_sales_summary_tool_payload",
        lambda period: {"period": period, "sales": {"net": 123.45, "orders": 3}},
    )

    payload = ar._approved_tool_data("what are Toast sales today?", ctx)

    assert payload["orders.store_summary"]["total_orders"] == 0
    assert payload["drivers.store_summary"]["total_drivers"] == 0
    assert payload["labor.store_aggregate"]["total_employees"] == 0
    assert payload["toast.sales_summary"]["period"] == "today"


def test_retry_outbox_record_is_redacted_and_hashed():
    row = {
        "id": "q1",
        "created_at": "2026-06-04T16:00:00Z",
        "question": "Show token=abc123SECRET and customer phone",
        "reason": "sensitive_or_operational_question_needs_approved_tool",
        "required_permission": "ai.ask_claude",
        "principal": {
            "kind": "staff",
            "role": "gm",
            "principal_id": 7,
            "display_name": "Test User",
            "store_slugs": ["dos"],
            "current_store": "dos",
            "path": "/dos/manager",
        },
    }

    record = ar._outbox_record(row)
    encoded = json.dumps(record, sort_keys=True)

    assert record["question_summary_redacted"] == "Show [REDACTED] and customer phone"
    assert "abc123SECRET" not in encoded
    assert "Test User" not in encoded
    assert record["principal_hash"]
    assert record["status"] == "needs_review"
    assert record["risk_level"] == "blocked"
    assert record["storage"] == "render_retry_outbox_redacted"


def test_store_scope_key_accepts_objects():
    class Store:
        slug = "tomball"

    assert ar._store_scope_key(Store()) == "tomball"
    assert ar._store_scope_key("dos") == "dos"
    assert ar._store_scope_key(None) is None


def test_queued_answer_distinguishes_tool_review_from_missing_permission():
    assert (
        ar._queued_answer("data_question_needs_approved_tool")
        == "I do not have the approved Cenas data tool for that yet, so I saved it for Sam review."
    )
    assert ar._queued_answer("missing_ai_permission").startswith("I can't safely answer")


def test_assistant_enabled_is_off_by_default_on_render(monkeypatch):
    monkeypatch.setenv("RENDER", "1")
    monkeypatch.delenv("AI_ASSISTANT_ENABLED", raising=False)
    monkeypatch.delenv("AI_ASSISTANT_DISABLED", raising=False)

    assert ar._assistant_enabled() is False

    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "1")
    assert ar._assistant_enabled() is True


def test_render_context_stays_disabled_without_ck_runtime(monkeypatch):
    monkeypatch.setenv("RENDER", "1")
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "1")
    monkeypatch.delenv("AI_ASSISTANT_CK_RUNTIME_URL", raising=False)
    monkeypatch.delenv("ASSISTANT_RUNTIME_URL", raising=False)
    monkeypatch.delenv("AI_ASSISTANT_ALLOW_RENDER_MODELS", raising=False)

    ctx = {
        "kind": "staff",
        "role": "gm",
        "principal_id": 7,
        "display_name": "Test User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/dos/manager",
        "permissions": ["ai.ask_claude", "ai.ask_claude_personal"],
        "can_ask_personal": True,
        "can_ask_operational": True,
    }

    assert ar._assistant_available_for_context(ctx) is False


def test_render_ask_proxies_to_ck_runtime(monkeypatch):
    class RuntimeHandler:
        seen = {}

    from http.server import BaseHTTPRequestHandler

    class RuntimeServer(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            RuntimeHandler.seen = {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
            payload = json.dumps({
                "ok": True,
                "answer": "CK-local answer",
                "queued": False,
                "model": "gemini-2.5-flash",
                "storage": "ck",
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            return

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), RuntimeServer)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(ar.assistant_bp)

    ctx = {
        "kind": "staff",
        "role": "gm",
        "principal_id": 7,
        "display_name": "Test User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/dos/manager",
        "permissions": ["ai.ask_claude", "ai.ask_claude_personal"],
        "can_ask_personal": True,
        "can_ask_operational": True,
    }
    monkeypatch.setattr(ar, "_principal_context", lambda: ctx)
    monkeypatch.setenv("RENDER", "1")
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "1")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setenv("ASSISTANT_REVIEW_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr(ar, "_gemini_answer", lambda *_: (_ for _ in ()).throw(AssertionError("Render must not call Gemini directly")))
    mirror_calls = []
    monkeypatch.setattr(ar, "_mirror_assistant_turn_to_cena_chat", lambda *args: mirror_calls.append(args))

    try:
        res = app.test_client().post(
            "/assistant/ask",
            json={
                "question": "what baout earlier this morning?",
                "previous_question": "How many caterings do we have today?",
            },
        )
        data = res.get_json()

        assert res.status_code == 200
        assert data["answer"] == "CK-local answer"
        assert RuntimeHandler.seen["path"] == "/assistant/answer"
        assert RuntimeHandler.seen["authorization"] == "Bearer runtime-token"
        assert RuntimeHandler.seen["body"]["question"] == "what baout earlier this morning?"
        assert RuntimeHandler.seen["body"]["previous_question"] == "How many caterings do we have today?"
        assert RuntimeHandler.seen["body"]["principal"]["role"] == "gm"
        assert RuntimeHandler.seen["body"]["principal"]["store_slugs"] == ["dos"]
        assert len(mirror_calls) == 1
        mirror_ctx, mirror_question, mirror_data, mirror_status, mirror_previous, mirror_prev_answer = mirror_calls[0]
        assert mirror_ctx["role"] == "gm"
        assert mirror_question == "what baout earlier this morning?"
        assert mirror_data["answer"] == "CK-local answer"
        assert mirror_status == 200
        assert mirror_previous == "How many caterings do we have today?"
        assert mirror_prev_answer == ""
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        for name in [
            "RENDER",
            "AI_ASSISTANT_ENABLED",
            "AI_ASSISTANT_CK_RUNTIME_URL",
            "AI_ASSISTANT_CK_RUNTIME_TOKEN",
            "ASSISTANT_REVIEW_TIMEOUT_SECONDS",
        ]:
            os.environ.pop(name, None)


def test_post_to_ck_review_uses_contract_path_for_base_url(tmp_path, monkeypatch):
    from scripts import assistant_review_ck_receiver as receiver

    db_path = tmp_path / "assistant_review.sqlite"
    token = "local-test-token"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setenv("ASSISTANT_REVIEW_TOKEN", token)

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), receiver.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("ASSISTANT_REVIEW_RECEIVER_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("ASSISTANT_REVIEW_RECEIVER_TOKEN", token)
        monkeypatch.setenv("ASSISTANT_REVIEW_TIMEOUT_SECONDS", "5")

        saved, ck_id = ar._post_to_ck_review({
            "id": "q-route-1",
            "created_at": "2026-06-04T18:00:00Z",
            "status": "needs_review",
            "risk_level": "blocked",
            "question": "Show customer phone and token=abc123SECRET",
            "reason": "sensitive_or_operational_question_needs_approved_tool",
            "required_permission": "ai.ask_claude",
            "role": "gm",
            "store_key": "dos",
            "model_key": "review_queue",
            "tool_name": "orders.store_summary",
            "delivery_target": "ck_assistant_review",
            "principal": {
                "kind": "staff",
                "role": "gm",
                "principal_id": 7,
                "display_name": "Test User",
                "store_slugs": ["dos"],
                "current_store": "dos",
                "path": "/dos/manager",
            },
        })

        assert saved is True
        assert ck_id == "q-route-1"

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
        con.close()

        assert counts == {
            "assistant_question": 1,
            "assistant_principal_snapshot": 1,
            "assistant_review_decision": 1,
            "assistant_model_audit": 1,
            "assistant_delivery_attempt": 1,
        }
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        os.environ.pop("ASSISTANT_REVIEW_DB", None)
        os.environ.pop("ASSISTANT_REVIEW_TOKEN", None)
        os.environ.pop("AI_ASSISTANT_CK_REVIEW_URL", None)
        os.environ.pop("AI_ASSISTANT_CK_REVIEW_TOKEN", None)
        os.environ.pop("ASSISTANT_REVIEW_RECEIVER_URL", None)
        os.environ.pop("ASSISTANT_REVIEW_RECEIVER_TOKEN", None)
        os.environ.pop("ASSISTANT_REVIEW_TIMEOUT_SECONDS", None)
