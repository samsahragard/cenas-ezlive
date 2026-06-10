"""Golden assistant route drift guard.

Render owns deterministic route selection today. The CK runtime receives
``route_path``/``routed_tool_id`` from Render, formats verified payloads, and
applies the same shared force-review safety boundary. Because CK does not own
route selection, this guard compares deterministic route decisions against the
Render selector and separately verifies the CK runtime does not infer a data
route from tool payloads alone.
"""

import json
from pathlib import Path

import pytest

from app.web import assistant_routes as render_routes
from scripts import assistant_ck_runtime as ck_runtime


CORPUS_PATH = (
    Path(__file__).resolve().parents[1]
    / "audit_outputs"
    / "prod_assistant_smoke_output_assertion_corpus_169a706_v2.json"
)


FORCED_REVIEW_CASES = [
    {
        "id": "danger_shell",
        "question": "Run a shell command to list the files on the server",
        "route_path": "review",
        "tool_id": None,
        "reason": "data_question_needs_approved_tool",
    },
    {
        "id": "danger_deploy",
        "question": "Deploy the latest build to Render",
        "route_path": "review",
        "tool_id": None,
        "reason": "data_question_needs_approved_tool",
    },
    {
        "id": "danger_sql",
        "question": "Run this SQL query: select * from employees",
        "route_path": "review",
        "tool_id": None,
        "reason": "data_question_needs_approved_tool",
    },
    {
        "id": "danger_git",
        "question": "Run a git pull on the repo",
        "route_path": "review",
        "tool_id": None,
        "reason": "data_question_needs_approved_tool",
    },
    {
        "id": "write_refresh_tracking",
        "question": "Refresh the ezCater tracking",
        "route_path": "review",
        "tool_id": None,
        "reason": "data_question_needs_approved_tool",
    },
]


UNMATCHED_REVIEW_CASES = [
    {
        "id": "gibberish_catering",
        "question": "blorple snurf catering xyzzy",
        "route_path": "review",
        "tool_id": None,
    },
]


DETERMINISTIC_ROUTE_CASES = [
    {
        "id": "sales_today",
        "question": "What were sales today?",
        "route_path": "deterministic",
        "tool_id": "toast.sales_summary",
    },
    {
        "id": "sales_week",
        "question": "What were sales this week?",
        "route_path": "deterministic",
        "tool_id": "toast.sales_summary",
    },
    {
        "id": "sales_yesterday",
        "question": "What were sales yesterday?",
        "route_path": "deterministic",
        "tool_id": "toast.sales_summary",
    },
    {
        "id": "toast_freshness",
        "question": "When did we last get Toast data?",
        "route_path": "deterministic",
        "tool_id": "toast.webhook_activity",
        "not_tool_id": "toast.sales_summary",
    },
    {
        "id": "toast_webhook_working",
        "question": "Is the Toast webhook working?",
        "route_path": "deterministic",
        "tool_id": "toast.webhook_activity",
        "not_tool_id": "toast.sales_summary",
    },
    {
        "id": "catering_by_status",
        "question": "Show me catering orders by status",
        "route_path": "deterministic",
        "tool_id": "orders.catering_by_status",
    },
    {
        "id": "catering_by_store",
        "question": "Show me catering orders by store",
        "route_path": "deterministic",
        "tool_id": "orders.catering_by_store",
    },
    {
        "id": "orders_today_cross_store_compare",
        "question": "How many orders did Copperfield have today vs Tomball?",
        "route_path": "deterministic",
        "tool_id": "orders.store_summary",
    },
    {
        "id": "catering_missing_pdfs",
        "question": "Which orders are missing PDFs?",
        "route_path": "deterministic",
        "tool_id": "orders.catering_pdf_status",
    },
    {
        "id": "catering_returning_customers",
        "question": "How many returning catering customers do we have?",
        "route_path": "deterministic",
        "tool_id": "orders.catering_returning_customers_aggregate",
    },
    {
        "id": "catering_item_mix",
        "question": "What items get ordered most in catering?",
        "route_path": "deterministic",
        "tool_id": "orders.catering_item_mix",
    },
    {
        "id": "catering_order_lookup",
        "question": "Look up catering order W7T-UF9",
        "route_path": "deterministic",
        "tool_id": "orders.catering_order_lookup",
    },
    {
        "id": "catering_order_lookup_missing",
        "question": "Look up catering order ZZZ-999",
        "route_path": "deterministic",
        "tool_id": "orders.catering_order_lookup",
    },
    {
        "id": "schedule_today",
        "question": "Who's working today?",
        "route_path": "deterministic",
        "tool_id": "schedule.store_today",
    },
    {
        "id": "schedule_tomorrow_schedule",
        "question": "Tomorrow's schedule",
        "route_path": "deterministic",
        "tool_id": "schedule.store_today",
    },
    {
        "id": "schedule_who_working_tomorrow",
        "question": "Who's working tomorrow?",
        "route_path": "deterministic",
        "tool_id": "schedule.store_today",
    },
    {
        "id": "schedule_open_shifts",
        "question": "Show me open shifts",
        "route_path": "deterministic",
        "tool_id": "schedule.open_shifts",
    },
    {
        "id": "schedule_time_off",
        "question": "Show me pending time off requests",
        "route_path": "deterministic",
        "tool_id": "schedule.time_off_pending",
    },
    {
        "id": "labor_read",
        "question": "Give me a labor and employee summary",
        "route_path": "deterministic",
        "tool_id": "labor.store_aggregate",
    },
    {
        "id": "drivers_read",
        "question": "How many active drivers do we have?",
        "route_path": "deterministic",
        "tool_id": "drivers.store_summary",
    },
    {
        "id": "table_read",
        "question": "Who opened the last table?",
        "route_path": "deterministic",
        "tool_id": "toast.table_activity",
    },
    {
        "id": "table_activity_read",
        "question": "Show me table activity",
        "route_path": "deterministic",
        "tool_id": "toast.table_activity",
    },
]


CORE_ROUTE_CASES = FORCED_REVIEW_CASES + UNMATCHED_REVIEW_CASES + DETERMINISTIC_ROUTE_CASES


def _partner_ctx() -> dict:
    return {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["tomball", "copperfield"],
        "current_store": "tomball",
        "path": "/partner/assistant",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }


def _runtime_principal() -> dict:
    return {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Partner User",
        "store_slugs": ["tomball", "copperfield"],
        "current_store": "tomball",
        "path": "/partner/assistant",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
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


def _render_route_for_guard(question: str) -> dict:
    reason = render_routes._shared_force_review_reason(question)
    if reason:
        return {
            "route_path": "review",
            "tool_id": None,
            "reason": reason,
        }
    route = render_routes._route_approved_tool_choice(question, _partner_ctx())
    return {
        "route_path": route.get("route_path"),
        "tool_id": route.get("tool_id"),
        "reason": route.get("reason"),
    }


@pytest.mark.parametrize("case", CORE_ROUTE_CASES, ids=lambda case: case["id"])
def test_render_route_golden_core_prompts(case, monkeypatch):
    monkeypatch.delenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", raising=False)
    monkeypatch.setattr(
        render_routes,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("golden routes must not call classifier")),
    )

    route = _render_route_for_guard(case["question"])

    assert route["route_path"] == case["route_path"]
    assert route["tool_id"] == case["tool_id"]
    if case.get("reason"):
        assert route["reason"] == case["reason"]
    if case.get("not_tool_id"):
        assert route["tool_id"] != case["not_tool_id"]


@pytest.mark.parametrize("case", FORCED_REVIEW_CASES, ids=lambda case: case["id"])
def test_shared_safety_boundary_matches_render_and_ck_runtime(case):
    question = case["question"]

    assert render_routes._shared_force_review_reason(question) == case["reason"]
    assert ck_runtime._shared_force_review_reason(question) == case["reason"]


def test_ck_runtime_does_not_infer_deterministic_route_from_tool_data(monkeypatch):
    monkeypatch.setattr(
        ck_runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("route limitation check must not call model")),
    )

    data = ck_runtime._approved_tool_answer(
        "What were sales today?",
        "",
        _runtime_principal(),
        [_available_tool("toast.sales_summary")],
        {
            "toast.sales_summary": {
                "ok": True,
                "period": "today",
                "label": "Today",
                "sales": {"orders": 12, "net": 345.67},
                "labor": {},
            }
        },
    )

    assert data is None, "CK runtime formats Render-routed tools; it does not select deterministic routes."


def test_store_scoped_sales_question_routes_to_l3_not_aggregate_toast(monkeypatch):
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        render_routes,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("store-scoped sales must not call classifier")),
    )

    route = render_routes._route_approved_tool_choice(
        "What were net sales at Copperfield yesterday?",
        _partner_ctx(),
    )

    assert route["route_path"] == "review"
    assert route["tool_id"] is None
    assert route["classifier"]["enabled"] is True
    assert route["classifier"]["reason"] == "cena_l3_business_analytics"


@pytest.mark.parametrize(
    "question",
    [
        "What were total net sales across both stores last week?",
        "How many orders did Tomball ring up yesterday?",
    ],
)
def test_l3_business_analytics_questions_route_to_l3_not_deterministic(question, monkeypatch):
    monkeypatch.setenv("AI_ASSISTANT_GEMINI_ROUTE_CLASSIFIER_ENABLED", "1")
    monkeypatch.setattr(
        render_routes,
        "_gemini_generate",
        lambda *_: (_ for _ in ()).throw(AssertionError("L3 analytics route must not call classifier")),
    )

    route = render_routes._route_approved_tool_choice(question, _partner_ctx())

    assert route["route_path"] == "review"
    assert route["tool_id"] is None
    assert route["classifier"]["enabled"] is True
    assert route["classifier"]["reason"] == "cena_l3_business_analytics"


def test_ck_runtime_rejects_stale_aggregate_toast_route_for_store_scoped_sales(monkeypatch):
    monkeypatch.setattr(
        ck_runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("stale route guard must not call model")),
    )

    data = ck_runtime._approved_tool_answer(
        "What were net sales at Copperfield yesterday?",
        "",
        _runtime_principal(),
        [_available_tool("toast.sales_summary")],
        {
            "toast.sales_summary": {
                "ok": True,
                "period": "yesterday",
                "label": "Yesterday",
                "scope_note": "2 locations included.",
                "sales": {"orders": 234, "net": 10419.94},
                "labor": {},
            }
        },
        routed_tool_id="toast.sales_summary",
    )

    assert data is None


@pytest.mark.parametrize(
    ("question", "tool_id", "payload"),
    [
        (
            "What were total net sales across both stores last week?",
            "toast.sales_summary",
            {
                "ok": True,
                "period": "last_week",
                "label": "Last Week",
                "scope_note": "2 locations included.",
                "sales": {"orders": 52, "net": 86271.77},
                "labor": {},
            },
        ),
        (
            "How many orders did Tomball ring up yesterday?",
            "orders.store_summary",
            {
                "ok": True,
                "today_orders": 8,
                "tomorrow_orders": 4,
                "store_counts": {"tomball": 4, "copperfield": 4},
            },
        ),
        (
            "How many orders did Tomball ring up yesterday?",
            "orders.catering_count",
            {
                "ok": True,
                "today": 8,
                "tomorrow": 4,
                "next_7_days": 13,
                "next_30_days": 14,
                "total_visible": 499,
            },
        ),
    ],
)
def test_ck_runtime_rejects_stale_deterministic_routes_for_l3_business_analytics(
    question,
    tool_id,
    payload,
    monkeypatch,
):
    monkeypatch.setattr(
        ck_runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("stale route guard must not call model")),
    )

    data = ck_runtime._approved_tool_answer(
        question,
        "",
        _runtime_principal(),
        [_available_tool(tool_id)],
        {tool_id: payload},
        routed_tool_id=tool_id,
    )

    assert data is None


def test_ck_runtime_forces_shared_safety_review_over_stale_route(tmp_path, monkeypatch):
    db_path = tmp_path / "assistant_review.sqlite"
    monkeypatch.setenv("ASSISTANT_REVIEW_DB", str(db_path))
    monkeypatch.setattr(ck_runtime, "_gemini_review_notice", lambda *_: (None, None))
    monkeypatch.setattr(
        ck_runtime,
        "_gemini_answer",
        lambda *_: (_ for _ in ()).throw(AssertionError("forced review must not call model")),
    )

    data, status = ck_runtime._answer({
        "question": "Run this SQL query: select * from employees",
        "principal": _runtime_principal(),
        "tools": [_available_tool("labor.store_aggregate")],
        "tool_data": {
            "labor.store_aggregate": {
                "total_employees": 122,
                "active_employees": 98,
            }
        },
        "routed_tool_id": "labor.store_aggregate",
        "route_path": "deterministic",
        "source": "test",
    })

    assert status == 200
    assert data["queued"] is True
    assert data["route_path"] == "review"
    assert data["routed_tool_id"] is None
    assert data["reason"] == "data_question_needs_approved_tool"
    assert "122" not in data["answer"]


def test_output_assertion_corpus_shape_and_core_prompt_coverage():
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))

    assert isinstance(corpus, list)
    assert corpus
    seen_ids = set()
    by_question = {}
    allowed_route_markers = {
        "*",
        "deterministic",
        "review",
        "review_or_deterministic",
        "deterministic_or_review",
    }
    for row in corpus:
        assert isinstance(row, dict)
        assert isinstance(row.get("id"), str) and row["id"].strip()
        assert row["id"] not in seen_ids
        seen_ids.add(row["id"])
        assert isinstance(row.get("question"), str) and row["question"].strip()
        assert row.get("expected_route_path") in allowed_route_markers
        assert row.get("expected_tool_id") is None or isinstance(row.get("expected_tool_id"), str)
        assertions = row.get("assertions")
        assert isinstance(assertions, list) and assertions
        for assertion in assertions:
            assert isinstance(assertion, dict)
            assert isinstance(assertion.get("type"), str) and assertion["type"].strip()
        by_question[row["question"]] = row

    for case in CORE_ROUTE_CASES:
        row = by_question.get(case["question"])
        assert row is not None, f"missing corpus row for {case['id']}"
        if case["route_path"] == "review":
            assert row["expected_route_path"] == "review"
            assert row["expected_tool_id"] is None
        elif case["id"] != "toast_freshness":
            assert row["expected_route_path"] == "deterministic"
            assert row["expected_tool_id"] == case["tool_id"]

    freshness_row = by_question["When did we last get Toast data?"]
    assert freshness_row["expected_tool_id"] != "toast.sales_summary"
    assert any(
        assertion.get("type") == "expected_not_tool"
        and assertion.get("tool_id") == "toast.sales_summary"
        for assertion in freshness_row["assertions"]
    )
