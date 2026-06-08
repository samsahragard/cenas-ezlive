import json
import os
import socket
import threading
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest
from flask import Flask
from sqlalchemy.orm import sessionmaker

from app.models import (
    AttendanceShift,
    Driver,
    DriverAssignmentJob,
    Employee,
    EmployeeAvailability,
    EmployeeStoreAssignment,
    EmployeeUnavailabilityBlock,
    EzcaterOrderDetails,
    InHouseCateringQuote,
    Order,
    OrderItem,
    PerfPeriodCache,
    Position,
    ProcessingJob,
    ProcessingOrder,
    SamChatMessage,
    SamChatSession,
    Schedule,
    Shift,
    ShiftAcceptance,
    ShiftAlarm,
    ShiftOffer,
    ShiftSwap,
    TimeOffRequest,
)
from app.services.assistant_handlers import orders as order_handlers
from app.services.assistant_handlers import schedule as schedule_handlers
from app.services.assistant_tool_inventory import (
    PARTNER_TOOL_IDS,
    is_excluded_non_routable,
)
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
    assert tools["orders.store_summary"]["available"] is True
    assert tools["orders.store_summary"]["status"] == "active"
    assert tools["orders.catering_today"]["available"] is True
    assert tools["orders.catering_today"]["status"] == "active"
    assert tools["drivers.store_summary"]["available"] is True
    assert tools["labor.store_aggregate"]["available"] is True


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
    assert tools["orders.catering_today"]["available"] is True
    assert tools["orders.catering_today"]["status"] == "active"
    assert tools["drivers.store_summary"]["available"] is True
    assert tools["toast.sales_summary"]["available"] is True
    assert tools["toast.sales_summary"]["status"] == "active"
    assert tools["toast.table_activity"]["available"] is True
    assert tools["toast.table_activity"]["status"] == "active"
    assert tools["toast.webhook_activity"]["available"] is True
    assert tools["toast.webhook_activity"]["status"] == "active"
    assert tools["toast.employee_profiles"]["available"] is True
    assert tools["toast.employee_profiles"]["status"] == "active"


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
    assert tools["orders.catering_today"]["available"] is True
    assert tools["drivers.store_summary"]["available"] is True
    assert tools["labor.store_aggregate"]["available"] is True
    assert tools["toast.sales_summary"]["available"] is True
    assert tools["toast.table_activity"]["available"] is True
    assert tools["toast.webhook_activity"]["available"] is True
    assert tools["toast.employee_profiles"]["available"] is True
    assert tools["employee.my_profile"]["available"] is False
    assert tools["employee.my_profile.read"]["available"] is True
    assert tools["finance.pnl_summary"]["available"] is True
    assert tools["orders.assign_driver"]["available"] is True
    assert "read_file" not in tools
    assert "render_env_set" not in tools
    assert "sql_query" not in tools
    assert "dev.assistant_tool_catalog_snapshot" not in tools
    assert sum(1 for tool in tools.values() if tool["available"]) < len(PARTNER_TOOL_IDS)


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

    assert "read_file" not in tools
    assert tools["finance.pnl_summary"]["available"] is False
    assert "dev.assistant_tool_catalog_snapshot" not in tools
    assert tools["toast.webhook_activity"]["available"] is False
    assert tools["toast.employee_profiles"]["available"] is True


def test_excluded_partner_tools_are_not_routable_for_partner_level():
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "permissions": ["*"],
        "is_owner_operator": False,
    }

    tools = {tool["tool_id"]: tool for tool in ar._tool_catalog_for(ctx)}
    excluded_ids = [tool_id for tool_id in PARTNER_TOOL_IDS if is_excluded_non_routable(tool_id)]

    assert excluded_ids
    assert not (set(excluded_ids) & set(tools))
    assert ar._route_approved_tool_id("please run git status", ctx) is None


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
        asked_at="2026-06-06T01:02:03Z",
    )

    session_row = (
        db_session.query(SamChatSession)
        .filter(SamChatSession.title == "Cenas AI Review: Sam")
        .one()
    )
    message = (
        db_session.query(SamChatMessage)
        .filter(SamChatMessage.session_id == session_row.id)
        .one()
    )
    assert message.role == "system"
    assert message.model == "assistant-review-mirror"
    assert message.content.startswith("CENAS_ASSISTANT_REVIEW_V2\n")
    payload = json.loads(message.content.split("\n", 1)[1])
    assert payload["kind"] == "cenas.assistant_mirror"
    assert payload["version"] == 2
    assert payload["asked_at"] == "2026-06-06T01:02:03Z"
    assert payload["actor"]["display_name"] == "Sam"
    assert payload["actor"]["principal_id"] == 1
    assert payload["actor"]["principal_type"] == "partner"
    assert payload["actor"]["role"] == "partner"
    assert payload["actor"]["owner_operator"] is True
    assert payload["permissions"]["summary"] == "*"
    assert payload["scope"]["current_store"] == "tomball"
    assert payload["scope"]["store_slugs"] == ["tomball", "copperfield"]
    assert payload["turn"]["question"] == "who opened the last table and what time"
    assert payload["turn"]["previous"] == {
        "question": "what was the last table opened",
        "answer": "Table 311 was opened at 7:54 PM CT.",
    }
    assert payload["turn"]["answer"] == "Table 311 was opened at 7:54 PM CT by Test Waiter."
    assert payload["result"]["status"] == "answered"
    assert payload["result"]["http_status"] == 200
    assert payload["result"]["ok"] is True
    assert payload["result"]["queued"] is False
    assert payload["tool"]["id"] == "toast.table_activity"
    assert payload["tool"]["model"] == "gemini-2.5-flash"

    ar._mirror_assistant_turn_to_cena_chat(
        ctx,
        "who was the waiter",
        {"ok": False, "error": "assistant_unavailable"},
        503,
    )

    assert (
        db_session.query(SamChatSession)
        .filter(SamChatSession.title == "Cenas AI Review: Sam")
        .count()
    ) == 1
    assert (
        db_session.query(SamChatMessage)
        .filter(SamChatMessage.session_id == session_row.id)
        .count()
    ) == 2
    unavailable_message = (
        db_session.query(SamChatMessage)
        .filter(SamChatMessage.session_id == session_row.id)
        .order_by(SamChatMessage.id.desc())
        .first()
    )
    unavailable_payload = json.loads(unavailable_message.content.split("\n", 1)[1])
    assert unavailable_payload["result"]["status"] == "unavailable"
    assert unavailable_payload["result"]["error"] == "assistant_unavailable"
    assert unavailable_payload["turn"]["answer"] == "I saved that for Sam review. The assistant model is not available right now."

    other_ctx = dict(ctx)
    other_ctx["principal_id"] = 2
    other_ctx["display_name"] = "Javier Cruz"
    ar._mirror_assistant_turn_to_cena_chat(
        other_ctx,
        "how many caterings today",
        {"ok": True, "answer": "One catering.", "queued": False},
        200,
    )
    assert (
        db_session.query(SamChatSession)
        .filter(SamChatSession.title == "Cenas AI Review: Javier Cruz")
        .count()
    ) == 1


def test_assistant_review_payload_redacts_raw_response_and_marks_queue():
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 1,
        "display_name": "Sam",
        "store_slugs": ["tomball"],
        "current_store": "tomball",
        "path": "/partner/",
        "permissions": ["ai.ask_claude"],
        "is_owner_operator": False,
        "can_ask_personal": True,
        "can_ask_operational": True,
    }

    payload = ar._assistant_review_payload(
        ctx,
        "what was on the ticket token=abc123SECRET",
        {
            "ok": True,
            "answer": "I saved that for Sam review.",
            "queued": True,
            "queue_id": 42,
            "ck_question_id": "ck-123",
            "reason": "needs_review",
            "storage": "assistant_review",
            "review_notice_model": "gemini-2.5-flash",
            "route_path": "review",
            "routed_tool_id": "toast.table_activity",
            "tool_id": "toast.table_activity",
            "route_meta": {
                "latency_ms": 12,
                "classifier": {
                    "enabled": False,
                    "token_cost_usd": 0.0,
                },
            },
            "debug": "token=abc123SECRET",
        },
        200,
        asked_at="2026-06-06T02:03:04Z",
    )

    assert payload["result"]["status"] == "queued"
    assert payload["result"]["queue_id"] == 42
    assert payload["result"]["ck_question_id"] == "ck-123"
    assert payload["result"]["reason"] == "needs_review"
    assert payload["tool"]["storage"] == "assistant_review"
    assert payload["tool"]["model"] == "gemini-2.5-flash"
    assert payload["tool"]["route_path"] == "review"
    assert payload["tool"]["routed_tool_id"] == "toast.table_activity"
    assert payload["tool"]["final_tool_id"] == "toast.table_activity"
    assert payload["telemetry"]["route_latency_ms"] == 12
    assert payload["telemetry"]["classifier_token_cost_usd"] == 0.0
    assert "abc123SECRET" not in payload["turn"]["question"]
    assert "abc123SECRET" not in payload["raw_response"]
    assert "[REDACTED]" in payload["raw_response"]


WAVE1_ORDER_TOOL_CASES = [
    {
        "tool_id": "orders.catering_by_status",
        "handler": "orders_catering_by_status",
        "hit": "catering order status split",
        "near_miss": "what is the order of operations status",
    },
    {
        "tool_id": "orders.catering_by_store",
        "handler": "orders_catering_by_store",
        "hit": "catering orders by store",
        "near_miss": "store split for dining room sections",
    },
    {
        "tool_id": "orders.catering_count",
        "handler": "orders_catering_count",
        "hit": "how many catering orders are visible",
        "near_miss": "how many chairs are in the dining room",
    },
    {
        "tool_id": "orders.catering_driver_assignment_summary",
        "handler": "orders_catering_driver_assignment_summary",
        "hit": "catering driver assignment jobs",
        "near_miss": "assignment jobs for the cleaning checklist",
    },
    {
        "tool_id": "orders.catering_fees_summary",
        "handler": "orders_catering_fees_summary",
        "hit": "catering fees summary",
        "near_miss": "bank fees summary from accounting",
    },
    {
        "tool_id": "orders.catering_item_mix",
        "handler": "orders_catering_item_mix",
        "hit": "catering item mix",
        "near_miss": "item mix for kitchen inventory",
    },
    {
        "tool_id": "orders.catering_late_risk",
        "handler": "orders_catering_late_risk",
        "hit": "which catering orders are late risk",
        "near_miss": "is the staff meeting running late",
    },
    {
        "tool_id": "orders.catering_live_tracking",
        "handler": "orders_catering_live_tracking",
        "hit": "which catering orders have live tracking links",
        "near_miss": "tracking links for a package shipment",
    },
    {
        "tool_id": "orders.catering_needs_driver",
        "handler": "orders_catering_needs_driver",
        "hit": "which orders still need a driver",
        "near_miss": "does the office computer need a device installed",
    },
    {
        "tool_id": "orders.catering_next_30_days",
        "handler": "orders_catering_next_30_days",
        "hit": "what caterings are in the next 30 days",
        "near_miss": "what are the next 30 days of weather",
    },
    {
        "tool_id": "orders.catering_order_items_safe",
        "handler": "orders_catering_order_items_safe",
        "hit": "what was on order TO-TODAY",
        "near_miss": "what was on the prep clipboard",
    },
    {
        "tool_id": "orders.catering_order_lookup",
        "handler": "orders_catering_order_lookup",
        "hit": "show order details for TO-TODAY",
        "near_miss": "show details for the staff memo",
    },
    {
        "tool_id": "orders.catering_payout_safe_summary",
        "handler": "orders_catering_payout_safe_summary",
        "hit": "catering payout summary",
        "near_miss": "pay out the cash drawer summary",
    },
    {
        "tool_id": "orders.catering_pdf_status",
        "handler": "orders_catering_pdf_status",
        "hit": "catering pdf uploaded summary",
        "near_miss": "pdf upload status for legal documents",
    },
    {
        "tool_id": "orders.catering_returning_customers_aggregate",
        "handler": "orders_catering_returning_customers_aggregate",
        "hit": "catering returning customers aggregate",
        "near_miss": "returning employees aggregate",
    },
    {
        "tool_id": "orders.catering_today",
        "handler": "orders_catering_today",
        "hit": "what caterings are today",
        "near_miss": "what is today's manager note",
    },
    {
        "tool_id": "orders.catering_tomorrow",
        "handler": "orders_catering_tomorrow",
        "hit": "what catering orders are tomorrow",
        "near_miss": "what is tomorrow's weather",
    },
    {
        "tool_id": "orders.catering_tracking_missing",
        "handler": "orders_catering_tracking_missing",
        "hit": "any orders missing tracking links",
        "near_miss": "missing tracking for office supplies",
    },
    {
        "tool_id": "orders.catering_uuid_status",
        "handler": "orders_catering_uuid_status",
        "hit": "catering tracking id coverage",
        "near_miss": "uuid status for a software deploy",
    },
    {
        "tool_id": "orders.catering_week",
        "handler": "orders_catering_week",
        "hit": "what catering orders are this week",
        "near_miss": "what is this week's cleaning schedule",
    },
    {
        "tool_id": "orders.in_house_quote_lookup",
        "handler": "orders_in_house_quote_lookup",
        "hit": "show in-house quote details",
        "near_miss": "show the house rules details",
    },
    {
        "tool_id": "orders.in_house_quotes_summary",
        "handler": "orders_in_house_quotes_summary",
        "hit": "in-house quotes status summary",
        "near_miss": "house status summary for maintenance",
    },
]


def _wave1_partner_ctx(*, permissions=None, stores=None) -> dict:
    return {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": list(stores or ["tomball", "copperfield"]),
        "current_store": (stores or ["tomball"])[0],
        "path": "/partner/catering",
        "permissions": list(permissions or ["*"]),
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }


def _wave1_staff_ctx() -> dict:
    return {
        "kind": "staff",
        "role": "gm",
        "principal_id": 7,
        "display_name": "Store GM",
        "store_slugs": ["tomball"],
        "current_store": "tomball",
        "path": "/tomball/manager",
        "permissions": ["ai.ask_claude", "orders.view"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }


def _wave1_staff_ctx_without_store_scope() -> dict:
    ctx = _wave1_staff_ctx()
    ctx["store_slugs"] = []
    ctx["current_store"] = None
    return ctx


def _seed_wave1_order_fixture(db_session) -> None:
    today = date.today()
    now = datetime.utcnow()
    processing_job = ProcessingJob(status="completed", pdf_count=1, success_count=1)
    tomball_today = Order(
        external_order_id="TO-TODAY",
        delivery_date=today.isoformat(),
        deliver_at="9:30 AM",
        delivery_window_start=datetime(today.year, today.month, today.day, 0, 1),
        origin_store_id="tomball",
        status="approved",
        delivery_tracking_id="track-live",
        ezcater_status_key="en_route",
        assigned_driver="Jolie Driver",
        customer_phone="713-555-1212",
        delivery_address="123 Private St",
        client="Repeat Co",
        headcount=25,
        total_amount=250.0,
        delivery_fee=25.0,
        tip_amount=20.0,
        potential_payout=45.0,
        paid_payout=35.0,
        pay_verified_miles=12.5,
    )
    tomball_tomorrow = Order(
        external_order_id="TO-TOMORROW",
        delivery_date=(today + timedelta(days=1)).isoformat(),
        deliver_at="1:00 PM",
        delivery_window_start=datetime(today.year, today.month, today.day, 13, 0) + timedelta(days=1),
        origin_store_id="tomball",
        status="new",
        customer_phone="713-555-3434",
        delivery_address="789 Private Ln",
        client="Repeat Co",
        headcount=12,
        total_amount=125.0,
        delivery_fee=15.0,
        tip_amount=10.0,
        potential_payout=25.0,
        paid_payout=0.0,
        pay_verified_miles=4.0,
    )
    copperfield_today = Order(
        external_order_id="CF-TODAY",
        delivery_date=today.isoformat(),
        deliver_at="12:30 PM",
        origin_store_id="copperfield",
        status="approved",
        delivery_tracking_id="track-hidden",
        ezcater_status_key="en_route",
        assigned_driver="Hidden Driver",
        customer_phone="713-555-9999",
        delivery_address="456 Hidden Ave",
        client="Secret Co",
        total_amount=300.0,
        delivery_fee=30.0,
        tip_amount=30.0,
    )
    db_session.add_all([processing_job, tomball_today, tomball_tomorrow, copperfield_today])
    db_session.flush()
    db_session.add_all([
        OrderItem(order_id=tomball_today.id, raw_alias="Fajita Pack", item_key="fajita_pack", qty=2),
        OrderItem(order_id=tomball_tomorrow.id, raw_alias="Queso Tray", item_key="queso_tray", qty=1),
        OrderItem(order_id=copperfield_today.id, raw_alias="Taco Pack", item_key="taco_pack", qty=3),
        EzcaterOrderDetails(
            external_order_id="TO-TODAY",
            commission_cents=1000,
            service_fee_cents=500,
            processing_fee_cents=250,
            source_pdf_path=r"C:\private\TO-TODAY.pdf",
            source_pdf_sha256="a" * 64,
            gate_code="SECRET-GATE",
            day_of_contact_name="Private Contact",
            day_of_contact_phone="713-555-7777",
        ),
        EzcaterOrderDetails(
            external_order_id="CF-TODAY",
            commission_cents=2000,
            service_fee_cents=1000,
            processing_fee_cents=500,
            source_pdf_path=r"C:\private\CF-TODAY.pdf",
            source_pdf_sha256="b" * 64,
        ),
        ProcessingOrder(
            processing_job_id=processing_job.id,
            order_id=tomball_today.id,
            external_order_id="TO-TODAY",
            status="completed",
        ),
        ProcessingOrder(
            processing_job_id=processing_job.id,
            order_id=copperfield_today.id,
            external_order_id="CF-TODAY",
            status="completed",
        ),
        DriverAssignmentJob(
            job_id="job-to-today",
            order_id="TO-TODAY",
            current_driver="Old Driver",
            new_driver="Jolie Driver",
            status="completed",
            retry_count=1,
            updated_at=now,
        ),
        DriverAssignmentJob(
            job_id="job-cf-today",
            order_id="CF-TODAY",
            current_driver="Hidden Driver",
            new_driver="Hidden New Driver",
            status="completed",
            retry_count=1,
            updated_at=now,
        ),
        InHouseCateringQuote(
            store_scope="tomball",
            customer_name="Private Quote Customer",
            customer_email="private@example.com",
            customer_phone="713-555-5656",
            event_address="555 Quote Address",
            event_date=datetime(today.year, today.month, today.day) + timedelta(days=5),
            guest_count=20,
            items_json=json.dumps([{"slug": "fajita_pack", "qty": 2}]),
            subtotal=200.0,
            status="sent",
            email_sent_at=now,
        ),
        InHouseCateringQuote(
            store_scope="copperfield",
            customer_name="Hidden Quote Customer",
            customer_email="hidden@example.com",
            customer_phone="713-555-8888",
            event_address="999 Hidden Quote Address",
            guest_count=30,
            subtotal=300.0,
            status="draft",
        ),
    ])
    db_session.commit()


@pytest.mark.parametrize("case", WAVE1_ORDER_TOOL_CASES, ids=lambda case: case["tool_id"])
def test_wave1_order_matchers_route_each_read_tool_and_reject_near_miss(case, monkeypatch):
    ctx = _wave1_partner_ctx(stores=["tomball"])
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        ar,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("deterministic order route must not call classifier")),
    )

    route = ar._route_approved_tool_choice(case["hit"], ctx)
    near_route = ar._deterministic_route_tool_id(case["near_miss"], ctx)

    assert route["tool_id"] == case["tool_id"]
    assert route["route_path"] == "deterministic"
    assert route["classifier"]["reason"] == "not_used"
    assert ar._TOOL_MATCHERS[case["handler"]](case["near_miss"]) is False
    assert near_route != case["tool_id"]


def test_smoke_catering_specific_prompts_do_not_fall_back_to_generic_order_routes(monkeypatch):
    ctx = _wave1_partner_ctx(stores=["tomball"])
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        ar,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("specific order route must not call classifier")),
    )

    cases = {
        "Show me catering orders by status": "orders.catering_by_status",
        "Which orders are missing PDFs?": "orders.catering_pdf_status",
    }

    for question, expected_tool in cases.items():
        route = ar._route_approved_tool_choice(question, ctx)
        assert route["tool_id"] == expected_tool
        assert route["route_path"] == "deterministic"


@pytest.mark.parametrize(
    ("question", "expected_token"),
    [
        ("Look up catering order W7T-UF9", "W7T-UF9"),
        ("show ezCater xgc-t07 details", "xgc-t07"),
        ("ticket C8V-PEG status", "C8V-PEG"),
        ("quote id 0GU-0W3", "0GU-0W3"),
        ("show catering order TO-TODAY", "TO-TODAY"),
    ],
)
def test_order_lookup_token_prefers_real_ezcater_ids_and_skips_structural_words(question, expected_token):
    assert order_handlers._lookup_token(question) == expected_token


@pytest.mark.parametrize(
    "question",
    [
        "catering order status",
        "order id",
        "order number",
        "order no",
        "ezcater ticket",
        "catering quote number",
    ],
)
def test_order_lookup_token_does_not_return_keyword_stop_words(question):
    assert order_handlers._lookup_token(question) is None


@pytest.mark.parametrize(
    ("question", "expected_tool"),
    [
        ("any tex-mex catering orders today", "orders.catering_today"),
        ("show mon-fri catering orders this week", "orders.catering_week"),
    ],
)
def test_hyphenated_menu_or_day_phrases_do_not_route_to_order_lookup(monkeypatch, question, expected_tool):
    ctx = _wave1_partner_ctx(stores=["tomball"])
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        ar,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("hyphenated non-id route must not call classifier")),
    )

    route = ar._route_approved_tool_choice(question, ctx)

    assert route["tool_id"] == expected_tool
    assert route["route_path"] == "deterministic"
    assert ar._TOOL_MATCHERS["orders_catering_order_lookup"](question) is False


@pytest.mark.parametrize(
    "question",
    [
        "what items get ordered most in catering",
        "most ordered",
        "ordered most",
        "most popular",
        "popular items",
        "best selling",
        "top selling",
    ],
)
def test_catering_item_aggregate_prompts_route_to_item_mix_not_order_items(monkeypatch, question):
    ctx = _wave1_partner_ctx(stores=["tomball"])
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        ar,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("aggregate item route must not call classifier")),
    )

    route = ar._route_approved_tool_choice(question, ctx)

    assert route["tool_id"] == "orders.catering_item_mix"
    assert route["route_path"] == "deterministic"
    assert ar._TOOL_MATCHERS["orders_catering_order_items_safe"](question) is False


@pytest.mark.parametrize(
    "question",
    [
        "what items are on order W7T-UF9",
        "on order XGC-T07 what food was included",
        "order C8V-PEG items",
    ],
)
def test_order_items_requires_explicit_order_reference(monkeypatch, question):
    ctx = _wave1_partner_ctx(stores=["tomball"])
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        ar,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("explicit order item route must not call classifier")),
    )

    route = ar._route_approved_tool_choice(question, ctx)

    assert route["tool_id"] == "orders.catering_order_items_safe"
    assert route["route_path"] == "deterministic"


@pytest.mark.parametrize(
    "question",
    [
        "how many returning catering customers do we have",
        "how many repeat ezcater customers do we have",
        "count returning high value catering customers",
    ],
)
def test_returning_customer_prompts_route_to_returning_aggregate_before_count(monkeypatch, question):
    ctx = _wave1_partner_ctx(stores=["tomball"])
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        ar,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("returning customer route must not call classifier")),
    )

    route = ar._route_approved_tool_choice(question, ctx)

    assert route["tool_id"] == "orders.catering_returning_customers_aggregate"
    assert route["route_path"] == "deterministic"
    assert ar._TOOL_MATCHERS["orders_catering_count"](question) is False


@pytest.mark.parametrize("case", WAVE1_ORDER_TOOL_CASES, ids=lambda case: case["tool_id"])
def test_wave1_order_handlers_return_fixture_payload_for_every_read_tool(db_session, monkeypatch, case):
    _seed_wave1_order_fixture(db_session)
    monkeypatch.setattr(order_handlers, "SessionLocal", lambda: db_session)

    payload = order_handlers.ORDER_TOOL_HANDLERS[case["handler"]](
        case["hit"],
        _wave1_partner_ctx(stores=["tomball"]),
    )
    encoded = json.dumps(payload, sort_keys=True).lower()

    assert payload["ok"] is True
    assert payload["tool_id"] == case["tool_id"]
    assert payload["data_class"] == "orders_read_sanitized"
    assert "cf-today" not in encoded
    assert "copperfield" not in encoded
    assert "taco_pack" not in encoded
    assert "hidden" not in encoded
    assert "713-555" not in encoded
    assert "private st" not in encoded
    assert "private@example.com" not in encoded
    assert "secret-gate" not in encoded
    assert "private contact" not in encoded


@pytest.mark.parametrize("case", WAVE1_ORDER_TOOL_CASES, ids=lambda case: case["tool_id"])
def test_wave1_order_payloads_are_denied_without_orders_permission(case, monkeypatch):
    ctx = _wave1_partner_ctx(permissions=["ai.ask_claude"], stores=["tomball"])
    monkeypatch.setitem(
        order_handlers.ORDER_TOOL_HANDLERS,
        case["handler"],
        lambda *_: (_ for _ in ()).throw(AssertionError("denied order tool must not execute")),
    )

    tools = {tool["tool_id"]: tool for tool in ar._tool_catalog_for(ctx)}

    assert tools[case["tool_id"]]["available"] is False
    assert tools[case["tool_id"]]["deny_reason"] == "missing_permission"
    assert ar._approved_tool_data(case["hit"], ctx) == {}


@pytest.mark.parametrize("case", WAVE1_ORDER_TOOL_CASES, ids=lambda case: case["tool_id"])
def test_wave1_order_handlers_respect_staff_store_scope_for_every_read_tool(db_session, monkeypatch, case):
    _seed_wave1_order_fixture(db_session)
    monkeypatch.setattr(order_handlers, "SessionLocal", lambda: db_session)

    payload = order_handlers.ORDER_TOOL_HANDLERS[case["handler"]](
        case["hit"],
        _wave1_staff_ctx(),
    )
    encoded = json.dumps(payload, sort_keys=True).lower()

    assert payload["ok"] is True
    assert payload["tool_id"] == case["tool_id"]
    assert "cf-today" not in encoded
    assert "copperfield" not in encoded
    assert "taco_pack" not in encoded
    assert "hidden" not in encoded


@pytest.mark.parametrize("case", WAVE1_ORDER_TOOL_CASES, ids=lambda case: case["tool_id"])
def test_wave1_order_handlers_fail_closed_without_staff_store_scope(db_session, monkeypatch, case):
    _seed_wave1_order_fixture(db_session)
    monkeypatch.setattr(order_handlers, "SessionLocal", lambda: db_session)

    payload = order_handlers.ORDER_TOOL_HANDLERS[case["handler"]](
        case["hit"],
        _wave1_staff_ctx_without_store_scope(),
    )
    data_only = dict(payload)
    data_only["question"] = ""
    data_only["searched_token"] = ""
    encoded = json.dumps(data_only, sort_keys=True).lower()

    assert payload["ok"] is True
    assert payload["tool_id"] == case["tool_id"]
    assert "to-today" not in encoded
    assert "to-tomorrow" not in encoded
    assert "cf-today" not in encoded
    assert "tomball" not in encoded
    assert "copperfield" not in encoded
    assert "fajita_pack" not in encoded
    assert "taco_pack" not in encoded
    assert "hidden" not in encoded


WAVE1_SCHEDULE_TOOL_CASES = [
    {
        "tool_id": "schedule.alarm_pending_summary",
        "handler": "schedule_alarm_pending_summary",
        "hit": "schedule pending alarms",
        "near_miss": "pending equipment checks",
    },
    {
        "tool_id": "schedule.availability_conflicts",
        "handler": "schedule_availability_conflicts",
        "hit": "schedule availability conflicts",
        "near_miss": "vendor availability report",
    },
    {
        "tool_id": "schedule.open_shifts",
        "handler": "schedule_open_shifts",
        "hit": "schedule open shifts",
        "near_miss": "open catering orders",
    },
    {
        "tool_id": "schedule.shift_acceptance_summary",
        "handler": "schedule_shift_acceptance_summary",
        "hit": "schedule shift acceptance summary",
        "near_miss": "acceptance letter summary",
    },
    {
        "tool_id": "schedule.shift_offer_summary",
        "handler": "schedule_shift_offer_summary",
        "hit": "shift offer summary",
        "near_miss": "vendor offer summary",
    },
    {
        "tool_id": "schedule.shift_swap_summary",
        "handler": "schedule_shift_swap_summary",
        "hit": "shift swap summary",
        "near_miss": "swap the payment card",
    },
    {
        "tool_id": "schedule.store_today",
        "handler": "schedule_store_today",
        "hit": "today's schedule shifts",
        "near_miss": "today's sales report",
    },
    {
        "tool_id": "schedule.store_week",
        "handler": "schedule_store_week",
        "hit": "this week schedule",
        "near_miss": "this week's cleaning checklist",
    },
    {
        "tool_id": "schedule.time_off_pending",
        "handler": "schedule_time_off_pending",
        "hit": "pending time-off requests",
        "near_miss": "pending legal requests",
    },
    {
        "tool_id": "schedule.unavailability_blocks",
        "handler": "schedule_unavailability_blocks",
        "hit": "schedule unavailability blocks",
        "near_miss": "website unavailable",
    },
    {
        "tool_id": "schedule.view",
        "handler": "schedule_view",
        "hit": "show current schedule",
        "near_miss": "show current menu",
    },
]


def _wave1_schedule_staff_ctx() -> dict:
    ctx = _wave1_staff_ctx()
    ctx["permissions"] = ["ai.ask_claude", "schedule.view"]
    ctx["path"] = "/tomball/schedules-v2"
    return ctx


def _wave1_schedule_staff_ctx_without_store_scope() -> dict:
    ctx = _wave1_schedule_staff_ctx()
    ctx["store_slugs"] = []
    ctx["current_store"] = None
    return ctx


def _seed_wave1_schedule_fixture(db_session) -> None:
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    tomball_server = Employee(
        full_name="Tomball Server",
        phone="713-555-1001",
        email="tomball-private@example.com",
        active=True,
    )
    tomball_cook = Employee(
        full_name="Tomball Cook",
        phone="713-555-1002",
        email="cook-private@example.com",
        active=True,
    )
    copperfield_server = Employee(
        full_name="Hidden CF Server",
        phone="713-555-9001",
        email="hidden-private@example.com",
        active=True,
    )
    db_session.add_all([tomball_server, tomball_cook, copperfield_server])
    db_session.flush()
    server = Position(name="Server", store_key=None)
    cook = Position(name="Cook", store_key=None)
    db_session.add_all([server, cook])
    db_session.flush()
    tomball_schedule = Schedule(store_key="tomball", week_start=week_start, status="published")
    copperfield_schedule = Schedule(store_key="copperfield", week_start=week_start, status="published")
    db_session.add_all([tomball_schedule, copperfield_schedule])
    db_session.flush()
    tomball_today = Shift(
        schedule_id=tomball_schedule.id,
        employee_id=tomball_server.id,
        position_id=server.id,
        start_at=datetime(today.year, today.month, today.day, 9, 0),
        end_at=datetime(today.year, today.month, today.day, 15, 0),
        break_minutes=30,
        status="assigned",
        notes="private manager note",
    )
    tomball_open = Shift(
        schedule_id=tomball_schedule.id,
        employee_id=None,
        position_id=cook.id,
        start_at=datetime(today.year, today.month, today.day, 16, 0) + timedelta(days=1),
        end_at=datetime(today.year, today.month, today.day, 21, 0) + timedelta(days=1),
        status="open",
    )
    tomball_swap = Shift(
        schedule_id=tomball_schedule.id,
        employee_id=tomball_cook.id,
        position_id=cook.id,
        start_at=datetime(today.year, today.month, today.day, 10, 0) + timedelta(days=2),
        end_at=datetime(today.year, today.month, today.day, 16, 0) + timedelta(days=2),
        status="assigned",
    )
    copperfield_today = Shift(
        schedule_id=copperfield_schedule.id,
        employee_id=copperfield_server.id,
        position_id=server.id,
        start_at=datetime(today.year, today.month, today.day, 11, 0),
        end_at=datetime(today.year, today.month, today.day, 17, 0),
        status="assigned",
    )
    db_session.add_all([tomball_today, tomball_open, tomball_swap, copperfield_today])
    db_session.flush()
    db_session.add_all([
        EmployeeStoreAssignment(employee_id=tomball_server.id, store_key="tomball"),
        EmployeeStoreAssignment(employee_id=tomball_cook.id, store_key="tomball"),
        EmployeeStoreAssignment(employee_id=copperfield_server.id, store_key="copperfield"),
        ShiftAcceptance(shift_id=tomball_today.id, employee_id=tomball_server.id, response="accepted"),
        ShiftAcceptance(
            shift_id=copperfield_today.id,
            employee_id=copperfield_server.id,
            response="declined",
            reason="hidden decline reason",
        ),
        ShiftAlarm(
            shift_id=tomball_today.id,
            employee_id=tomball_server.id,
            alarm_time=datetime.utcnow() - timedelta(minutes=5),
            channel="sms",
            status="pending",
        ),
        ShiftAlarm(
            shift_id=copperfield_today.id,
            employee_id=copperfield_server.id,
            alarm_time=datetime.utcnow(),
            channel="email",
            status="pending",
        ),
        TimeOffRequest(
            employee_id=tomball_server.id,
            start_date=today + timedelta(days=3),
            end_date=today + timedelta(days=4),
            reason="private time off reason",
            status="pending",
        ),
        TimeOffRequest(
            employee_id=copperfield_server.id,
            start_date=today + timedelta(days=5),
            end_date=today + timedelta(days=5),
            reason="hidden time off reason",
            status="pending",
        ),
        EmployeeAvailability(
            employee_id=tomball_server.id,
            day_of_week=today.weekday(),
            start_minute=600,
            end_minute=660,
        ),
        EmployeeUnavailabilityBlock(
            employee_id=tomball_server.id,
            start_at=datetime(today.year, today.month, today.day, 8, 30),
            end_at=datetime(today.year, today.month, today.day, 9, 30),
            reason="private unavailability reason",
        ),
        EmployeeUnavailabilityBlock(
            employee_id=copperfield_server.id,
            start_at=datetime(today.year, today.month, today.day, 10, 30),
            end_at=datetime(today.year, today.month, today.day, 11, 30),
            reason="hidden unavailability reason",
        ),
        ShiftOffer(
            shift_id=tomball_today.id,
            offered_by_employee_id=tomball_server.id,
            status="open",
            restricted=True,
            expires_at=datetime.utcnow() + timedelta(days=1),
        ),
        ShiftOffer(
            shift_id=copperfield_today.id,
            offered_by_employee_id=copperfield_server.id,
            status="taken",
            restricted=True,
            expires_at=datetime.utcnow() + timedelta(days=1),
        ),
        ShiftSwap(
            from_shift_id=tomball_today.id,
            to_shift_id=tomball_swap.id,
            from_employee_id=tomball_server.id,
            to_employee_id=tomball_cook.id,
            status="proposed",
            expires_at=datetime.utcnow() + timedelta(days=1),
        ),
        ShiftSwap(
            from_shift_id=copperfield_today.id,
            to_shift_id=copperfield_today.id,
            from_employee_id=copperfield_server.id,
            to_employee_id=copperfield_server.id,
            status="accepted",
            expires_at=datetime.utcnow() + timedelta(days=1),
        ),
    ])
    db_session.commit()


@pytest.mark.parametrize("case", WAVE1_SCHEDULE_TOOL_CASES, ids=lambda case: case["tool_id"])
def test_wave1_schedule_matchers_route_each_read_tool_and_reject_near_miss(case, monkeypatch):
    ctx = _wave1_partner_ctx(stores=["tomball"])
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        ar,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("deterministic schedule route must not call classifier")),
    )

    route = ar._route_approved_tool_choice(case["hit"], ctx)
    near_route = ar._deterministic_route_tool_id(case["near_miss"], ctx)

    assert route["tool_id"] == case["tool_id"]
    assert route["route_path"] == "deterministic"
    assert route["classifier"]["reason"] == "not_used"
    assert ar._TOOL_MATCHERS[case["handler"]](case["near_miss"]) is False
    assert near_route != case["tool_id"]


def test_smoke_who_working_today_routes_to_schedule_not_orders(monkeypatch):
    ctx = _wave1_partner_ctx(stores=["tomball"])
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        ar,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("schedule route must not call classifier")),
    )

    route = ar._route_approved_tool_choice("Who's working today?", ctx)

    assert route["tool_id"] == "schedule.store_today"
    assert route["route_path"] == "deterministic"


@pytest.mark.parametrize(
    "question",
    [
        "What's tomorrow's schedule?",
        "Tomorrow's schedule",
        "Who works tomorrow?",
        "Who's working tomorrow?",
    ],
)
def test_smoke_tomorrow_schedule_routes_to_local_date_schedule(monkeypatch, question):
    ctx = _wave1_partner_ctx(stores=["tomball"])
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        ar,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("schedule route must not call classifier")),
    )

    route = ar._route_approved_tool_choice(question, ctx)

    assert route["tool_id"] == "schedule.store_today"
    assert route["route_path"] == "deterministic"


@pytest.mark.parametrize(
    "question",
    [
        "What's tomorrow's schedule?",
        "Tomorrow's schedule",
        "Who works tomorrow?",
        "Who's working tomorrow?",
    ],
)
def test_tomorrow_schedule_approved_payload_uses_tomorrow_local_date(db_session, monkeypatch, question):
    _seed_wave1_schedule_fixture(db_session)
    monkeypatch.setattr(schedule_handlers, "SessionLocal", lambda: db_session)
    ctx = _wave1_partner_ctx(stores=["tomball"])
    tomorrow = schedule_handlers._today_local() + timedelta(days=1)

    tool_id, payload, route = ar._approved_tool_package(question, ctx)

    assert route["route_path"] == "deterministic"
    assert tool_id == "schedule.store_today"
    assert set(payload) == {"schedule.store_today"}
    schedule_payload = payload["schedule.store_today"]
    assert schedule_payload["date"] == tomorrow.isoformat()
    assert schedule_payload["window_label"] == "tomorrow_local_date_visible_allowed_schedule_stores"
    assert schedule_payload["shift_count"] == 1
    assert schedule_payload["by_store"] == {"tomball": 1}


@pytest.mark.parametrize("case", WAVE1_SCHEDULE_TOOL_CASES, ids=lambda case: case["tool_id"])
def test_wave1_schedule_handlers_return_fixture_payload_for_every_read_tool(db_session, monkeypatch, case):
    _seed_wave1_schedule_fixture(db_session)
    monkeypatch.setattr(schedule_handlers, "SessionLocal", lambda: db_session)

    payload = schedule_handlers.SCHEDULE_TOOL_HANDLERS[case["handler"]](
        case["hit"],
        _wave1_partner_ctx(stores=["tomball"]),
    )
    encoded = json.dumps(payload, sort_keys=True).lower()

    assert payload["ok"] is True
    assert payload["tool_id"] == case["tool_id"]
    assert payload["data_class"] == "schedule_read_sanitized"
    assert "hidden" not in encoded
    assert "copperfield" not in encoded
    assert "713-555" not in encoded
    assert "example.com" not in encoded
    assert "employee_id" not in encoded
    assert "schedule_id" not in encoded
    assert "shift_id" not in encoded
    assert "request_id" not in encoded
    assert "block_id" not in encoded
    assert "offer_id" not in encoded
    assert "swap_id" not in encoded
    assert "private manager note" not in encoded
    assert "private time off reason" not in encoded
    assert "private unavailability reason" not in encoded
    assert "hidden decline reason" not in encoded


def test_wave1_schedule_handlers_label_answer_windows(db_session, monkeypatch):
    _seed_wave1_schedule_fixture(db_session)
    monkeypatch.setattr(schedule_handlers, "SessionLocal", lambda: db_session)
    ctx = _wave1_partner_ctx(stores=["tomball"])

    today_payload = schedule_handlers.schedule_store_today("who is working today?", ctx)
    tomorrow_payload = schedule_handlers.schedule_store_today("tomorrow's schedule", ctx)
    week_payload = schedule_handlers.schedule_store_week("show this week", ctx)
    open_payload = schedule_handlers.schedule_open_shifts("open shifts", ctx)
    tomorrow = schedule_handlers._today_local() + timedelta(days=1)

    assert today_payload["window_label"] == "local_date_visible_allowed_schedule_stores"
    assert today_payload["date"] == schedule_handlers._today_local().isoformat()
    assert tomorrow_payload["window_label"] == "tomorrow_local_date_visible_allowed_schedule_stores"
    assert tomorrow_payload["date"] == tomorrow.isoformat()
    assert tomorrow_payload["shift_count"] == 1
    assert tomorrow_payload["open_shift_count"] == 1
    assert week_payload["window_label"] == "current_week_visible_allowed_schedule_stores"
    assert week_payload["week_start"] == schedule_handlers._week_start().isoformat()
    assert week_payload["published_schedule_count"] == 1
    assert week_payload["draft_schedule_count"] == 0
    assert open_payload["window_label"] == "remaining_today_forward_current_view"
    assert open_payload["as_of_date"] == schedule_handlers._today_local().isoformat()
    assert open_payload["by_schedule_status"] == {"published": 1}


def test_wave1_schedule_payloads_include_zero_visible_store_rows(db_session, monkeypatch):
    _seed_wave1_schedule_fixture(db_session)
    monkeypatch.setattr(schedule_handlers, "SessionLocal", lambda: db_session)
    ctx = _wave1_partner_ctx(stores=["tomball", "copperfield"])

    tomorrow_payload = schedule_handlers.schedule_store_today("tomorrow's schedule", ctx)
    open_payload = schedule_handlers.schedule_open_shifts("open shifts", ctx)

    assert tomorrow_payload["visible_store_keys"] == ["copperfield", "tomball"]
    assert tomorrow_payload["by_store"] == {"copperfield": 0, "tomball": 1}
    assert tomorrow_payload["stores_without_visible_rows"] == ["copperfield"]
    assert open_payload["visible_store_keys"] == ["copperfield", "tomball"]
    assert open_payload["by_store"] == {"copperfield": 0, "tomball": 1}
    assert open_payload["stores_without_visible_rows"] == ["copperfield"]


def test_week_open_count_and_open_shifts_are_different_labeled_windows(db_session, monkeypatch):
    fixed_today = date(2026, 6, 7)
    monkeypatch.setattr(schedule_handlers, "_today_local", lambda: fixed_today)
    _seed_wave1_schedule_fixture(db_session)
    monkeypatch.setattr(schedule_handlers, "SessionLocal", lambda: db_session)
    tomball_schedule = db_session.query(Schedule).filter_by(store_key="tomball").one()
    db_session.add_all([
        Shift(
            schedule_id=tomball_schedule.id,
            employee_id=None,
            start_at=datetime(2026, 6, 6, 10, 0),
            end_at=datetime(2026, 6, 6, 15, 0),
            status="open",
        ),
        Shift(
            schedule_id=tomball_schedule.id,
            employee_id=None,
            start_at=datetime(2026, 6, 6, 16, 0),
            end_at=datetime(2026, 6, 6, 21, 0),
            status="open",
        ),
    ])
    db_session.commit()
    ctx = _wave1_partner_ctx(stores=["tomball"])

    week_payload = schedule_handlers.schedule_store_week("show this week", ctx)
    open_payload = schedule_handlers.schedule_open_shifts("open shifts", ctx)

    assert week_payload["window_label"] == "current_week_visible_allowed_schedule_stores"
    assert open_payload["window_label"] == "remaining_today_forward_current_view"
    assert week_payload["week_start"] == "2026-06-01"
    assert open_payload["as_of_date"] == "2026-06-07"
    assert week_payload["open_shift_count"] == 2
    assert open_payload["count"] == 1


@pytest.mark.parametrize("case", WAVE1_SCHEDULE_TOOL_CASES, ids=lambda case: case["tool_id"])
def test_wave1_schedule_payloads_are_denied_without_schedule_permission(case, monkeypatch):
    ctx = _wave1_partner_ctx(permissions=["ai.ask_claude"], stores=["tomball"])
    monkeypatch.setitem(
        schedule_handlers.SCHEDULE_TOOL_HANDLERS,
        case["handler"],
        lambda *_: (_ for _ in ()).throw(AssertionError("denied schedule tool must not execute")),
    )

    tools = {tool["tool_id"]: tool for tool in ar._tool_catalog_for(ctx)}

    assert tools[case["tool_id"]]["available"] is False
    assert tools[case["tool_id"]]["deny_reason"] == "missing_permission"
    assert ar._approved_tool_data(case["hit"], ctx) == {}


@pytest.mark.parametrize("case", WAVE1_SCHEDULE_TOOL_CASES, ids=lambda case: case["tool_id"])
def test_wave1_schedule_handlers_respect_staff_store_scope_for_every_read_tool(db_session, monkeypatch, case):
    _seed_wave1_schedule_fixture(db_session)
    monkeypatch.setattr(schedule_handlers, "SessionLocal", lambda: db_session)

    payload = schedule_handlers.SCHEDULE_TOOL_HANDLERS[case["handler"]](
        case["hit"],
        _wave1_schedule_staff_ctx(),
    )
    encoded = json.dumps(payload, sort_keys=True).lower()

    assert payload["ok"] is True
    assert payload["tool_id"] == case["tool_id"]
    assert "hidden" not in encoded
    assert "copperfield" not in encoded
    assert "713-555" not in encoded


@pytest.mark.parametrize("case", WAVE1_SCHEDULE_TOOL_CASES, ids=lambda case: case["tool_id"])
def test_wave1_schedule_handlers_fail_closed_without_staff_store_scope(db_session, monkeypatch, case):
    _seed_wave1_schedule_fixture(db_session)
    monkeypatch.setattr(schedule_handlers, "SessionLocal", lambda: db_session)

    payload = schedule_handlers.SCHEDULE_TOOL_HANDLERS[case["handler"]](
        case["hit"],
        _wave1_schedule_staff_ctx_without_store_scope(),
    )
    data_only = dict(payload)
    data_only["question"] = ""
    encoded = json.dumps(data_only, sort_keys=True).lower()

    assert payload["ok"] is True
    assert payload["tool_id"] == case["tool_id"]
    assert "tomball server" not in encoded
    assert "tomball cook" not in encoded
    assert "hidden" not in encoded
    assert "tomball" not in encoded
    assert "copperfield" not in encoded
    assert "713-555" not in encoded


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
    monkeypatch.setattr(order_handlers, "SessionLocal", lambda: db_session)

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

    payload = ar._approved_tool_data("Give me the order summary", ctx)
    encoded = json.dumps(payload, sort_keys=True).lower()

    summary = payload["orders.store_summary"]
    assert summary["total_orders"] == 2
    assert summary["today_orders"] == 1
    assert summary["upcoming_orders"] == 2
    assert summary["today_time_windows"]["morning"] == 1
    assert summary["today_time_windows_by_store"]["morning"]["copperfield"] == 1
    assert summary["needs_driver_orders"] == 1
    assert summary["live_tracking_orders"] == 1
    assert "drivers.store_summary" not in payload
    assert "labor.store_aggregate" not in payload
    assert "713-555" not in encoded
    assert "private" not in encoded
    assert "secret co" not in encoded


def test_order_items_tool_payload_is_sanitized_and_store_scoped(db_session, monkeypatch):
    today = date.today()
    tomball_order = Order(
        external_order_id="TO-ITEM",
        delivery_date=today.isoformat(),
        deliver_at="11:30 AM",
        origin_store_id="tomball",
        status="approved",
        customer_phone="713-555-1212",
        delivery_address="123 Private St",
        client="Private Customer",
    )
    copperfield_order = Order(
        external_order_id="CF-ITEM",
        delivery_date=today.isoformat(),
        deliver_at="12:30 PM",
        origin_store_id="copperfield",
        status="approved",
        customer_phone="713-555-9999",
        delivery_address="456 Hidden Ave",
        client="Secret Co",
    )
    db_session.add_all([tomball_order, copperfield_order])
    db_session.flush()
    db_session.add_all([
        OrderItem(order_id=tomball_order.id, raw_alias="Fajita Pack", item_key="fajita_pack", qty=2),
        OrderItem(order_id=copperfield_order.id, raw_alias="Taco Pack", item_key="taco_pack", qty=1),
    ])
    db_session.commit()
    monkeypatch.setattr(order_handlers, "SessionLocal", lambda: db_session)

    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["tomball"],
        "current_store": "tomball",
        "path": "/partner/catering",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }

    payload = ar._approved_tool_data("what was on order TO-ITEM", ctx)
    encoded = json.dumps(payload, sort_keys=True).lower()

    assert "orders.catering_order_items_safe" in payload
    assert payload["orders.catering_order_items_safe"]["order"]["external_order_id"] == "TO-ITEM"
    assert payload["orders.catering_order_items_safe"]["items"][0]["label"] == "fajita_pack"
    assert "cf-item" not in encoded
    assert "713-555" not in encoded
    assert "private customer" not in encoded
    assert "hidden ave" not in encoded


def test_catering_order_lookup_missing_token_returns_found_false_without_latest_fallback(db_session, monkeypatch):
    _seed_wave1_order_fixture(db_session)
    monkeypatch.setattr(order_handlers, "SessionLocal", lambda: db_session)

    payload = ar._approved_tool_data("Look up catering order ZZZ-999", _wave1_partner_ctx(stores=["tomball"]))

    data = payload["orders.catering_order_lookup"]
    encoded = json.dumps(data, sort_keys=True).lower()
    assert data["found"] is False
    assert data["searched_token"] == "ZZZ-999"
    assert "order" not in data
    assert "to-today" not in encoded
    assert "to-tomorrow" not in encoded
    assert "cf-today" not in encoded


def test_catering_order_items_missing_token_returns_found_false_without_latest_fallback(db_session, monkeypatch):
    _seed_wave1_order_fixture(db_session)
    monkeypatch.setattr(order_handlers, "SessionLocal", lambda: db_session)

    payload = ar._approved_tool_data("What items are on order ZZZ-999?", _wave1_partner_ctx(stores=["tomball"]))

    data = payload["orders.catering_order_items_safe"]
    encoded = json.dumps(data, sort_keys=True).lower()
    assert data["found"] is False
    assert data["searched_token"] == "ZZZ-999"
    assert "order" not in data
    assert "items" not in data
    assert "to-today" not in encoded
    assert "to-tomorrow" not in encoded
    assert "cf-today" not in encoded


def test_catering_order_lookup_without_token_still_falls_back_to_latest_visible_order(db_session, monkeypatch):
    _seed_wave1_order_fixture(db_session)
    monkeypatch.setattr(order_handlers, "SessionLocal", lambda: db_session)

    payload = order_handlers.catering_order_lookup("show me the latest catering order", _wave1_partner_ctx(stores=["tomball"]))

    assert payload["found"] is True
    assert payload["searched_token"] is None
    assert payload["order"]["external_order_id"] == "TO-TOMORROW"


def test_order_handler_respects_staff_store_scope_directly(db_session, monkeypatch):
    today = date.today()
    db_session.add_all([
        Order(
            external_order_id="TB-TODAY",
            delivery_date=today.isoformat(),
            origin_store_id="tomball",
            status="approved",
        ),
        Order(
            external_order_id="CF-TODAY",
            delivery_date=today.isoformat(),
            origin_store_id="copperfield",
            status="approved",
        ),
    ])
    db_session.commit()
    monkeypatch.setattr(order_handlers, "SessionLocal", lambda: db_session)

    payload = order_handlers.catering_today(
        "caterings today",
        {
            "kind": "staff",
            "role": "gm",
            "store_slugs": ["tomball"],
            "current_store": "tomball",
            "permissions": ["ai.ask_claude", "orders.view"],
            "is_owner_operator": False,
        },
    )

    assert payload["count"] == 1
    assert payload["by_store"] == {"tomball": 1}
    assert payload["orders"][0]["external_order_id"] == "TB-TODAY"


@pytest.mark.parametrize(
    ("question", "expected_store", "expected_order", "blocked_order"),
    [
        ("Tomball caterings today", "tomball", "TB-TODAY", "CF-TODAY"),
        ("Copperfield caterings today", "copperfield", "CF-TODAY", "TB-TODAY"),
    ],
)
def test_order_handler_owner_operator_named_store_filters_raw_store_ids(
    db_session,
    monkeypatch,
    question,
    expected_store,
    expected_order,
    blocked_order,
):
    today = date.today()
    db_session.add_all([
        Order(
            external_order_id="TB-TODAY",
            delivery_date=today.isoformat(),
            origin_store_id="store_2",
            status="approved",
        ),
        Order(
            external_order_id="CF-TODAY",
            delivery_date=today.isoformat(),
            origin_store_id="store_1",
            status="approved",
        ),
    ])
    db_session.commit()
    monkeypatch.setattr(order_handlers, "SessionLocal", lambda: db_session)

    payload = order_handlers.catering_today(
        question,
        {
            "kind": "partner",
            "role": "partner",
            "store_slugs": ["tomball", "copperfield"],
            "current_store": None,
            "permissions": ["*"],
            "is_owner_operator": True,
        },
    )
    encoded = json.dumps(payload, sort_keys=True).lower()

    assert payload["count"] == 1
    assert payload["by_store"] == {expected_store: 1}
    assert payload["orders"][0]["external_order_id"] == expected_order
    assert blocked_order.casefold() not in encoded
    assert "store_1" not in encoded
    assert "store_2" not in encoded


def test_order_payload_suppressed_without_orders_permission(monkeypatch):
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["tomball"],
        "current_store": "tomball",
        "path": "/partner/catering",
        "permissions": ["ai.ask_claude"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }
    monkeypatch.setitem(
        order_handlers.ORDER_TOOL_HANDLERS,
        "orders_catering_today",
        lambda *_: (_ for _ in ()).throw(AssertionError("denied order tool must not execute")),
    )

    tools = {tool["tool_id"]: tool for tool in ar._tool_catalog_for(ctx)}

    assert tools["orders.catering_today"]["available"] is False
    assert tools["orders.catering_today"]["deny_reason"] == "missing_permission"
    assert ar._approved_tool_data("what caterings are today", ctx) == {}


def test_in_house_quote_summary_redacts_contact_details(db_session, monkeypatch):
    db_session.add(
        InHouseCateringQuote(
            store_scope="tomball",
            customer_name="Private Customer",
            customer_email="private@example.com",
            customer_phone="713-555-1212",
            event_address="123 Private St",
            guest_count=25,
            subtotal=250.0,
            status="sent",
        )
    )
    db_session.commit()
    monkeypatch.setattr(order_handlers, "SessionLocal", lambda: db_session)

    payload = order_handlers.in_house_quotes_summary(
        "in-house quote summary",
        {
            "kind": "partner",
            "role": "partner",
            "store_slugs": ["tomball"],
            "current_store": "tomball",
            "permissions": ["*"],
            "is_owner_operator": False,
        },
    )
    encoded = json.dumps(payload, sort_keys=True).lower()

    assert payload["quote_count"] == 1
    assert payload["recent_quotes"][0]["subtotal"] == 250.0
    assert "private@example.com" not in encoded
    assert "713-555" not in encoded
    assert "private customer" not in encoded
    assert "private st" not in encoded


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

    yesterday_payload = ar._approved_tool_data("what were Toast sales yesterday?", ctx)

    assert yesterday_payload["toast.sales_summary"]["period"] == "yesterday"


def test_labor_store_aggregate_payload_labels_metric_scopes(db_session, monkeypatch):
    today = date.today()
    now = datetime.utcnow()
    employee = Employee(full_name="Labor Scope Employee", active=True)
    db_session.add(employee)
    db_session.flush()
    schedule = Schedule(store_key="tomball", week_start=today, status="published")
    db_session.add(schedule)
    db_session.flush()
    db_session.add_all([
        EmployeeStoreAssignment(employee_id=employee.id, store_key="tomball"),
        Shift(
            schedule_id=schedule.id,
            employee_id=employee.id,
            start_at=now,
            end_at=now + timedelta(hours=6),
            status="assigned",
        ),
        Shift(
            schedule_id=schedule.id,
            employee_id=None,
            start_at=now + timedelta(days=1),
            end_at=now + timedelta(days=1, hours=5),
            status="open",
        ),
        PerfPeriodCache(
            cena_employee_id=employee.id,
            period="last30",
            store_key="tomball",
            total_hours=80.0,
        ),
    ])
    db_session.commit()
    monkeypatch.setattr(ar, "SessionLocal", lambda: db_session)

    payload = ar._labor_store_aggregate(_wave1_partner_ctx(stores=["tomball"]))

    assert payload["employee_count_scope"] == "all_allowed_employee_store_assignments"
    assert payload["schedule_shift_scope"] == "all_allowed_historical_published_schedules"
    assert payload["last30_cached_hours_scope"] == "last_30_perf_period_cache_rows"
    assert payload["published_shifts"] == 1
    assert payload["open_shifts"] == 1
    assert payload["last30_cached_hours"] == 80.0


def test_operator_toast_data_freshness_routes_to_webhook_not_sales(monkeypatch):
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
    monkeypatch.setattr(
        ar,
        "_toast_webhook_activity_tool_payload",
        lambda question: (_ for _ in ()).throw(AssertionError("webhook is CK-runtime passthrough")),
    )
    monkeypatch.setattr(
        ar,
        "_toast_sales_summary_tool_payload",
        lambda period: (_ for _ in ()).throw(AssertionError("freshness prompt must not build sales")),
    )

    tool_id, payload, route = ar._approved_tool_package("When did we last get data from Toast?", ctx)

    assert tool_id == "toast.webhook_activity"
    assert route["tool_id"] == "toast.webhook_activity"
    assert payload == {}
    assert "toast.sales_summary" not in payload


def test_toast_data_freshness_never_falls_back_to_sales_when_webhook_unavailable(monkeypatch):
    ctx = _wave1_partner_ctx()
    monkeypatch.setattr(
        ar,
        "_available_implemented_tools",
        lambda _ctx: {"toast.sales_summary": {"tool_id": "toast.sales_summary"}},
    )

    route = ar._route_approved_tool_choice("When did we last get data from Toast?", ctx)

    assert route["tool_id"] is None
    assert route["route_path"] == "review"


def test_unsupported_toast_sales_scope_does_not_route_to_today_summary():
    ctx = _wave1_partner_ctx()

    for question in (
        "What were sales on 2026-06-01?",
        "Give me revenue for June 1",
    ):
        route = ar._route_approved_tool_choice(question, ctx)
        assert route["tool_id"] is None
        assert route["route_path"] == "review"


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


def test_operator_toast_table_activity_payload_handles_bare_waiter_question(monkeypatch):
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
    monkeypatch.setattr(
        ar,
        "_toast_table_activity_tool_payload",
        lambda location, business_date=None: seen.update({
            "location": location,
            "business_date": business_date,
        }) or {"location": location, "business_date": business_date, "latest": None},
    )

    payload = ar._approved_tool_data("who was the waiter?", ctx)

    assert "toast.table_activity" in payload
    assert seen["location"] is None


def test_operator_toast_webhook_activity_routes_as_runtime_passthrough(monkeypatch):
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
    monkeypatch.setattr(
        ar,
        "_toast_webhook_activity_tool_payload",
        lambda question: (_ for _ in ()).throw(AssertionError("webhook is CK-runtime passthrough")),
    )

    tool_id, payload, route = ar._approved_tool_package("what live Toast webhook events came in today?", ctx)

    assert tool_id == "toast.webhook_activity"
    assert route["tool_id"] == "toast.webhook_activity"
    assert payload == {}
    assert "toast.employee_profiles" not in payload


def test_operator_toast_employee_profiles_payload(monkeypatch):
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
    monkeypatch.setattr(
        ar,
        "_toast_employee_profiles_tool_payload",
        lambda question: {"data_class": "toast_employee_profiles_sanitized", "question": question},
    )

    payload = ar._approved_tool_data("show employee 4 Toast profile facts", ctx)

    assert payload["toast.employee_profiles"]["data_class"] == "toast_employee_profiles_sanitized"


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
    monkeypatch.setattr(
        ar,
        "_toast_sales_summary_tool_payload",
        lambda period: {"period": period, "sales": {"net": 123.45, "orders": 3}},
    )

    payload = ar._approved_tool_data("what are Toast sales today?", ctx)

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


def test_ask_no_route_falls_back_to_gemini_general_path(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(ar.assistant_bp)

    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/partner/today",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }
    monkeypatch.setattr(ar, "_principal_context", lambda: ctx)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("AI_ASSISTANT_CK_RUNTIME_URL", raising=False)
    monkeypatch.delenv("ASSISTANT_RUNTIME_URL", raising=False)
    monkeypatch.setattr(ar, "_mirror_assistant_turn_to_cena_chat", lambda *args: None)
    monkeypatch.setattr(ar, "_queue_for_review", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("registry miss should not queue a general question")))
    monkeypatch.setattr(ar, "_gemini_answer", lambda question, _ctx: ("General answer", "gemini-test"))

    question = "hello there"
    assert ar._route_approved_tool_id(question, ctx) is None

    res = app.test_client().post("/assistant/ask", json={"question": question})
    data = res.get_json()

    assert res.status_code == 200
    assert data == {
        "ok": True,
        "answer": "General answer",
        "queued": False,
        "model": "gemini-test",
        "route_path": "general",
        "routed_tool_id": None,
    }


def test_deterministic_matcher_scan_uses_registry_priority():
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/partner/today",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }

    rows = ar._scan_deterministic_matchers(
        [
            "who opened the last table?",
            "show me table activity",
            "how many caterings today?",
            "blorple snurf catering xyzzy",
            "hello there",
        ],
        ctx,
    )

    assert [row["tool_id"] for row in rows] == [
        "toast.table_activity",
        "toast.table_activity",
        "orders.catering_today",
        None,
        None,
    ]
    assert rows[-1]["route_path"] == "review"


def test_generic_order_summary_does_not_route_bare_catering_gibberish():
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/partner/today",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }

    question = "blorple snurf catering xyzzy"

    assert ar._TOOL_MATCHERS["orders_store_summary"](question) is False
    assert ar._deterministic_route_tool_id(question, ctx) is None


def test_today_cross_store_order_compare_routes_to_summary_not_window_tool(monkeypatch):
    ctx = _wave1_partner_ctx(stores=["tomball", "copperfield"])
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        ar,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("cross-store order compare must not call classifier")),
    )

    question = "How many orders did Copperfield have today vs Tomball?"
    route = ar._route_approved_tool_choice(question, ctx)

    assert route["tool_id"] == "orders.store_summary"
    assert route["route_path"] == "deterministic"
    assert ar._TOOL_MATCHERS["orders_catering_today"](question) is False
    assert ar._TOOL_MATCHERS["orders_catering_count"](question) is False


def test_wave1_order_matchers_route_specific_tools_and_near_miss(monkeypatch):
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/partner/today",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        ar,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("deterministic order route must not call classifier")),
    )

    assert ar._route_approved_tool_choice("what caterings are today", ctx)["tool_id"] == "orders.catering_today"
    assert ar._route_approved_tool_choice("catering status split", ctx)["tool_id"] == "orders.catering_by_status"
    assert ar._route_approved_tool_choice("what was on order TO-1234", ctx)["tool_id"] == "orders.catering_order_items_safe"
    assert ar._deterministic_route_tool_id("what is the order of operations status", ctx) is None


def test_classifier_fallback_is_disabled_by_default(monkeypatch):
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/partner/today",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }
    monkeypatch.delenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", raising=False)
    monkeypatch.setattr(ar, "_gemini_generate", lambda *_: (_ for _ in ()).throw(AssertionError("classifier is off")))

    route = ar._route_approved_tool_choice("please use the alias", ctx)

    assert route["tool_id"] is None
    assert route["route_path"] == "review"
    assert route["classifier"]["enabled"] is False


def test_classifier_fallback_validates_alias_to_available_canonical_tool(monkeypatch):
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/partner/today",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(ar, "_gemini_generate", lambda *_: ('{"tool_id":"toast_live_tables"}', "gemini-test"))

    route = ar._route_approved_tool_choice("use the alias", ctx)

    assert route["tool_id"] == "toast.table_activity"
    assert route["route_path"] == "classifier"
    assert route["classifier"]["raw_tool_id"] == "toast_live_tables"


def test_classifier_fallback_rejects_excluded_tool_ids(monkeypatch):
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/partner/today",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(ar, "_gemini_generate", lambda *_: ('{"tool_id":"read_file"}', "gemini-test"))

    route = ar._route_approved_tool_choice("please open a local file", ctx)

    assert route["tool_id"] is None
    assert route["route_path"] == "review"
    assert route["classifier"]["reason"] == "not_allowed"


def test_yesterday_sales_routes_to_toast_summary_instead_of_review():
    ctx = _wave1_partner_ctx(stores=["tomball"])

    route = ar._route_approved_tool_choice("What were sales yesterday?", ctx)

    assert route["tool_id"] == "toast.sales_summary"
    assert route["route_path"] == "deterministic"


def test_runtime_passthrough_tools_get_explicit_routed_ids():
    ctx = {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["partner"],
        "current_store": None,
        "path": "/partner/today",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": True,
    }

    cases = [
        ("what tools are available?", "assistant.tool_discovery"),
        ("i am Sam.", "assistant.session_context"),
    ]

    for question, expected_tool_id in cases:
        tool_id, payload, route = ar._approved_tool_package(question, ctx)

        assert tool_id == expected_tool_id
        assert payload == {}
        assert route["tool_id"] == expected_tool_id
        assert route["route_path"] == "deterministic"


def test_render_proxy_sends_registry_route_to_ck_runtime(monkeypatch):
    class RuntimeHandler:
        seen = {}

    from http.server import BaseHTTPRequestHandler

    class RuntimeServer(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            RuntimeHandler.seen = json.loads(self.rfile.read(length).decode("utf-8"))
            payload = json.dumps({
                "ok": True,
                "answer": "CK-local answer",
                "queued": False,
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
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["dos"],
        "current_store": "dos",
        "path": "/partner/today",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }
    monkeypatch.setattr(ar, "_principal_context", lambda: ctx)
    monkeypatch.setattr(ar, "_mirror_assistant_turn_to_cena_chat", lambda *args: None)
    monkeypatch.setattr(
        ar,
        "_approved_tool_package",
        lambda question, _ctx: (
            "drivers.store_summary",
            {"drivers.store_summary": {"total_drivers": 5}},
            {
                "tool_id": "drivers.store_summary",
                "route_path": "deterministic",
                "latency_ms": 1,
                "classifier": {"enabled": False, "reason": "not_used"},
            },
        ),
    )
    monkeypatch.setenv("RENDER", "1")
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "1")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr(ar, "_gemini_answer", lambda *_: (_ for _ in ()).throw(AssertionError("Render must not call Gemini directly")))

    try:
        res = app.test_client().post(
            "/assistant/ask",
            json={
                "question": "how many drivers today?",
                "previous_question": "driver coverage yesterday",
            },
        )
        data = res.get_json()

        assert res.status_code == 200
        assert data["answer"] == "CK-local answer"
        assert RuntimeHandler.seen["routed_tool_id"] == "drivers.store_summary"
        assert RuntimeHandler.seen["route_path"] == "deterministic"
        assert RuntimeHandler.seen["route_meta"]["latency_ms"] == 1
        assert RuntimeHandler.seen["tool_data"] == {"drivers.store_summary": {"total_drivers": 5}}
        assert RuntimeHandler.seen["previous_question"] == "driver coverage yesterday"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        for name in [
            "RENDER",
            "AI_ASSISTANT_ENABLED",
            "AI_ASSISTANT_CK_RUNTIME_URL",
            "AI_ASSISTANT_CK_RUNTIME_TOKEN",
        ]:
            os.environ.pop(name, None)


def test_render_proxy_forces_exclude_prompt_to_review_despite_previous_context(monkeypatch):
    class RuntimeHandler:
        seen = {}

    from http.server import BaseHTTPRequestHandler

    class RuntimeServer(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            RuntimeHandler.seen = json.loads(self.rfile.read(length).decode("utf-8"))
            payload = json.dumps({
                "ok": True,
                "answer": "queued",
                "queued": True,
                "storage": "ck",
                "route_path": "review",
                "routed_tool_id": None,
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

    ctx = _wave1_partner_ctx(stores=["tomball"])
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr(
        ar,
        "_approved_tool_package",
        lambda *_: (_ for _ in ()).throw(AssertionError("excluded prompt must not route tools")),
    )

    try:
        runtime_response = ar._post_to_ck_runtime(
            "Deploy the latest build to Render",
            ctx,
            "who opened the last table and what time",
            "The most recent table open was table 42 and the waiter/server was Rubi Lira.",
        )

        assert runtime_response is not None
        assert RuntimeHandler.seen["question"] == "Deploy the latest build to Render"
        assert RuntimeHandler.seen["route_path"] == "review"
        assert "routed_tool_id" not in RuntimeHandler.seen
        assert RuntimeHandler.seen["tool_data"] == {}
        assert RuntimeHandler.seen["route_meta"]["reason"] == "data_question_needs_approved_tool"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        for name in [
            "AI_ASSISTANT_CK_RUNTIME_URL",
            "AI_ASSISTANT_CK_RUNTIME_TOKEN",
        ]:
            os.environ.pop(name, None)


def test_render_proxy_does_not_reattach_routed_tool_id_to_review_response(monkeypatch):
    class RuntimeHandler:
        seen = {}

    from http.server import BaseHTTPRequestHandler

    class RuntimeServer(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            RuntimeHandler.seen = json.loads(self.rfile.read(length).decode("utf-8"))
            payload = json.dumps({
                "ok": True,
                "answer": "queued",
                "queued": True,
                "storage": "ck",
                "route_path": "review",
                "reason": "data_question_needs_approved_tool",
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

    ctx = _wave1_partner_ctx(stores=["tomball"])
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr(
        ar,
        "_approved_tool_package",
        lambda *_: (
            "orders.catering_by_status",
            {"orders.catering_by_status": {"ok": True, "by_status": {"approved": 1}}},
            {"route_path": "deterministic", "tool_id": "orders.catering_by_status"},
        ),
    )

    try:
        runtime_response = ar._post_to_ck_runtime(
            "Show me catering orders by status",
            ctx,
        )

        assert runtime_response is not None
        data, status = runtime_response
        assert status == 200
        assert RuntimeHandler.seen["routed_tool_id"] == "orders.catering_by_status"
        assert data["queued"] is True
        assert data["route_path"] == "review"
        assert data["routed_tool_id"] is None
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        for name in [
            "AI_ASSISTANT_CK_RUNTIME_URL",
            "AI_ASSISTANT_CK_RUNTIME_TOKEN",
        ]:
            os.environ.pop(name, None)


def test_render_runtime_route_does_not_reuse_catering_context_for_schedule(monkeypatch):
    class RuntimeHandler:
        seen = {}

    from http.server import BaseHTTPRequestHandler

    class RuntimeServer(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            RuntimeHandler.seen = body
            payload = json.dumps({
                "ok": True,
                "answer": "CK-local schedule answer",
                "queued": False,
                "storage": "operational_tool",
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

    ctx = _wave1_partner_ctx(stores=["tomball", "copperfield"])
    monkeypatch.setattr(ar, "_principal_context", lambda: ctx)
    monkeypatch.setattr(ar, "_mirror_assistant_turn_to_cena_chat", lambda *args: None)
    monkeypatch.setattr(
        ar,
        "_approved_tool_handlers",
        lambda: {
            "orders_catering_week": lambda question, _ctx: {
                "ok": True,
                "tool_id": "orders.catering_week",
                "question": question,
                "sentinel": "orders",
            },
            "schedule_open_shifts": lambda question, _ctx: {
                "ok": True,
                "tool_id": "schedule.open_shifts",
                "question": question,
                "sentinel": "schedule",
            },
        },
    )
    monkeypatch.setenv("RENDER", "1")
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "1")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setenv("ASSISTANT_REVIEW_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr(
        ar,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("Render must not call Gemini directly")),
    )

    try:
        res = app.test_client().post(
            "/assistant/ask",
            json={
                "question": "schedule open shifts",
                "previous_question": "what catering orders are this week",
            },
        )
        data = res.get_json()

        assert res.status_code == 200
        assert data["answer"] == "CK-local schedule answer"
        assert RuntimeHandler.seen["routed_tool_id"] == "schedule.open_shifts"
        assert RuntimeHandler.seen["route_path"] == "deterministic"
        assert RuntimeHandler.seen["tool_data"] == {
            "schedule.open_shifts": {
                "ok": True,
                "tool_id": "schedule.open_shifts",
                "question": "schedule open shifts",
                "sentinel": "schedule",
            }
        }
        assert RuntimeHandler.seen["previous_question"] == "what catering orders are this week"
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
        (
            mirror_ctx,
            mirror_question,
            mirror_data,
            mirror_status,
            mirror_previous,
            mirror_prev_answer,
            mirror_asked_at,
        ) = mirror_calls[0]
        assert mirror_ctx["role"] == "gm"
        assert mirror_question == "what baout earlier this morning?"
        assert mirror_data["answer"] == "CK-local answer"
        assert mirror_status == 200
        assert mirror_previous == "How many caterings do we have today?"
        assert mirror_prev_answer == ""
        assert mirror_asked_at.endswith("Z")
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


def test_render_catering_time_followup_packages_store_summary_for_runtime(monkeypatch):
    class RuntimeHandler:
        seen = {}

    from http.server import BaseHTTPRequestHandler

    class RuntimeServer(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            RuntimeHandler.seen = body
            payload = json.dumps({
                "ok": True,
                "answer": "For earlier this morning, there were 2 catering orders. Store split: tomball: 1; copperfield: 1.",
                "queued": False,
                "model": "gemini-2.5-flash",
                "storage": "operational_tool",
                "tool_id": body.get("routed_tool_id"),
                "routed_tool_id": body.get("routed_tool_id"),
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

    ctx = _wave1_partner_ctx(stores=["tomball", "copperfield"])
    monkeypatch.setattr(ar, "_principal_context", lambda: ctx)
    monkeypatch.setattr(ar, "_mirror_assistant_turn_to_cena_chat", lambda *args: None)
    monkeypatch.setattr(
        ar,
        "_approved_tool_handlers",
        lambda: {
            "orders_store_summary": lambda question, _ctx: {
                "ok": True,
                "tool_id": "orders.store_summary",
                "question": question,
                "today": "2026-06-08",
                "today_orders": 3,
                "upcoming_orders": 5,
                "needs_driver_orders": 0,
                "live_tracking_orders": 0,
                "active_tracking_orders": 0,
                "today_by_store": {"tomball": 2, "copperfield": 1},
                "today_time_windows": {"morning": 2},
                "today_time_windows_by_store": {"morning": {"tomball": 1, "copperfield": 1}},
            },
        },
    )
    monkeypatch.setenv("RENDER", "1")
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "1")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("AI_ASSISTANT_CK_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setenv("ASSISTANT_REVIEW_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr(
        ar,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("Render must not call Gemini directly")),
    )

    try:
        res = app.test_client().post(
            "/assistant/ask",
            json={
                "question": "what about earlier this morning",
                "previous_question": "how many caterings today",
                "previous_answer": "There are 3 catering orders in the today view.",
            },
        )
        data = res.get_json()

        assert res.status_code == 200
        assert data["queued"] is False
        assert data["tool_id"] == "orders.store_summary"
        assert RuntimeHandler.seen["routed_tool_id"] == "orders.store_summary"
        assert RuntimeHandler.seen["route_path"] == "deterministic"
        assert RuntimeHandler.seen["tool_data"]["orders.store_summary"]["today_time_windows"]["morning"] == 2
        assert "how many caterings today" in RuntimeHandler.seen["tool_data"]["orders.store_summary"]["question"]
        assert "earlier this morning" in RuntimeHandler.seen["tool_data"]["orders.store_summary"]["question"]
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
