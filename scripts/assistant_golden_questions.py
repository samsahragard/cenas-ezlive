"""Golden-question harness for the Cenas in-app assistant runtime.

Runs locally against the CK runtime answer contract with explicit approved
read-only tools and sanitized payloads. It does not call production and does
not call Gemini; any fallback is written only to a throwaway local review DB.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from scripts import assistant_ck_runtime as runtime


def _principal() -> dict[str, Any]:
    return {
        "kind": "partner",
        "role": "partner",
        "principal_id": 99,
        "display_name": "Golden Partner",
        "store_slugs": ["tomball", "copperfield"],
        "current_store": "tomball",
        "path": "/partner/assistant",
        "permissions": ["*"],
        "can_ask_personal": True,
        "can_ask_operational": True,
        "is_owner_operator": False,
    }


def _available_tool(tool_id: str) -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "label": tool_id,
        "available": True,
        "status": "active",
        "deny_reason": None,
        "read_write_class": "read_only",
    }


def _today_business_date() -> str:
    return runtime._today_ct().strftime("%Y%m%d")


GOLDEN_QUESTIONS: list[dict[str, Any]] = [
    {
        "id": "caterings_today_all",
        "question": "how many caterings today",
        "tool_id": "orders.catering_today",
        "expected_route_args": {"store": "all_accessible", "window": "current_view"},
        "answer_terms": ["catering order", "today", "store split"],
        "tool_payload": {
            "ok": True,
            "generated_at": "2026-06-08T12:00:00Z",
            "data_class": "orders_read_sanitized",
            "window": "today",
            "count": 3,
            "by_store": {"tomball": 2, "copperfield": 1},
            "orders": [{"external_order_id": "TO-GOLDEN"}, {"external_order_id": "CF-GOLDEN"}],
        },
    },
    {
        "id": "caterings_today_tomball",
        "question": "Tomball caterings today",
        "tool_id": "orders.catering_today",
        "expected_route_args": {"store": "tomball", "window": "current_view"},
        "answer_terms": ["catering order", "today", "tomball"],
        "tool_payload": {
            "ok": True,
            "generated_at": "2026-06-08T12:00:00Z",
            "data_class": "orders_read_sanitized",
            "window": "today",
            "count": 2,
            "by_store": {"tomball": 2},
            "orders": [{"external_order_id": "TO-GOLDEN"}],
        },
    },
    {
        "id": "caterings_today_copperfield",
        "question": "Copperfield caterings today",
        "tool_id": "orders.catering_today",
        "expected_route_args": {"store": "copperfield", "window": "current_view"},
        "answer_terms": ["catering order", "today", "copperfield"],
        "tool_payload": {
            "ok": True,
            "generated_at": "2026-06-08T12:00:00Z",
            "data_class": "orders_read_sanitized",
            "window": "today",
            "count": 1,
            "by_store": {"copperfield": 1},
            "orders": [{"external_order_id": "CF-GOLDEN"}],
        },
    },
    {
        "id": "earlier_this_morning",
        "question": "what about earlier this morning",
        "previous_question": "how many caterings today",
        "previous_answer": "There are 3 catering orders in the today view.",
        "tool_id": "orders.store_summary",
        "expected_route_args": {"store": "all_accessible", "window": "morning"},
        "answer_terms": ["earlier this morning", "catering order", "store split"],
        "tool_payload": {
            "ok": True,
            "generated_at": "2026-06-08T12:00:00Z",
            "today": "2026-06-08",
            "today_orders": 3,
            "upcoming_orders": 5,
            "needs_driver_orders": 0,
            "live_tracking_orders": 2,
            "active_tracking_orders": 1,
            "today_by_store": {"tomball": 2, "copperfield": 1},
            "today_time_windows": {"morning": 1},
            "today_time_windows_by_store": {"morning": {"tomball": 1, "copperfield": 0}},
        },
    },
    {
        "id": "active_drivers",
        "question": "how many drivers active",
        "tool_id": "drivers.store_summary",
        "expected_route_args": {"scope": "current_view"},
        "answer_terms": ["driver", "active", "current view"],
        "tool_payload": {
            "ok": True,
            "generated_at": "2026-06-08T12:00:00Z",
            "data_class": "driver_aggregate_sanitized",
            "total_drivers": 9,
            "active_drivers": 7,
            "drivers_on_shift": 3,
            "drivers_on_active_orders": 2,
            "by_store": {"tomball": 4, "copperfield": 5},
        },
    },
    {
        "id": "active_employees",
        "question": "how many employees active",
        "tool_id": "labor.store_aggregate",
        "expected_route_args": {"scope": "current_view"},
        "answer_terms": ["labor summary", "employee", "active"],
        "tool_payload": {
            "ok": True,
            "generated_at": "2026-06-08T12:00:00Z",
            "data_class": "labor_aggregate_sanitized",
            "total_employees": 42,
            "active_employees": 37,
            "published_shifts": 18,
            "open_shifts": 2,
            "last30_cached_hours": 1234.5,
            "today_attendance_statuses": {"present": 12},
        },
    },
    {
        "id": "todays_net_sales",
        "question": "today's net sales",
        "tool_id": "toast.sales_summary",
        "expected_route_args": {"period": "today"},
        "answer_terms": ["toast analytics", "net sales", "gross sales"],
        "tool_payload": {
            "ok": True,
            "generated_at": "2026-06-08T12:00:00Z",
            "data_class": "toast_analytics_summary",
            "label": "Today",
            "period": "today",
            "date_range": {"start": "2026-06-08", "end": "2026-06-08"},
            "sales": {
                "orders": 42,
                "guests": 96,
                "net": 3210.12,
                "gross": 3500.00,
                "avg_order": 76.43,
            },
            "labor": {"hours": 28.5, "cost": 711.25, "ratio_pct": 22.2},
            "scope_note": "approved partner-visible Toast Analytics stores only.",
        },
    },
    {
        "id": "latest_table_tomball",
        "question": "most recent table opened in Tomball",
        "tool_id": "toast.table_activity",
        "expected_route_args": {"location": "tomball"},
        "answer_terms": ["tomball", "in-store table open", "table"],
        "tool_payload": {
            "ok": True,
            "generated_at": "2026-06-08T12:00:00Z",
            "data_class": "toast_table_activity_sanitized",
            "location": "tomball",
            "location_label": "Tomball",
            "business_date": _today_business_date(),
            "latest": {
                "table_name": "106",
                "opened_at_local": "2026-06-08 8:15 AM CT",
                "opened_by_name": "Golden Server",
                "server_name": "Golden Server",
                "table_config_available": True,
                "employee_lookup_available": True,
            },
        },
    },
]


def _case_payload(case: dict[str, Any]) -> dict[str, Any]:
    tool_id = case["tool_id"]
    route_kind, route_args = runtime._route_args(tool_id, case["question"])
    return {
        "question": case["question"],
        "previous_question": case.get("previous_question", ""),
        "previous_answer": case.get("previous_answer", ""),
        "principal": _principal(),
        "tools": [_available_tool(tool_id)],
        "tool_data": {tool_id: case["tool_payload"]},
        "routed_tool_id": tool_id,
        "route_path": "deterministic",
        "route_meta": {
            "tool_id": tool_id,
            "route_kind": route_kind,
            "route_args": route_args,
            "source": "golden_questions",
        },
        "source": "golden_questions",
    }


def _case_result(case: dict[str, Any]) -> dict[str, Any]:
    payload = _case_payload(case)
    data, status = runtime._answer(payload)
    failures: list[str] = []
    answer = str(data.get("answer") or "")
    answer_lc = answer.casefold()
    route_args = payload["route_meta"]["route_args"]

    if status != 200:
        failures.append(f"expected HTTP 200, got {status}")
    if data.get("ok") is not True:
        failures.append("response ok was not true")
    if data.get("queued") is not False:
        failures.append("response queued instead of answering live")
    if data.get("route_path") == "review":
        failures.append("route_path fell back to review")
    if data.get("tool_id") != case["tool_id"]:
        failures.append(f"tool_id {data.get('tool_id')!r} != {case['tool_id']!r}")
    if data.get("routed_tool_id") != case["tool_id"]:
        failures.append(f"routed_tool_id {data.get('routed_tool_id')!r} != {case['tool_id']!r}")
    if "sam review" in answer_lc:
        failures.append("answer contains Sam review fallback")
    for key, expected in (case.get("expected_route_args") or {}).items():
        if route_args.get(key) != expected:
            failures.append(f"route arg {key!r} {route_args.get(key)!r} != {expected!r}")
    for term in case.get("answer_terms") or []:
        if str(term).casefold() not in answer_lc:
            failures.append(f"answer missing term {term!r}")
    if not runtime._tool_answer_verified(case["tool_id"], case["tool_payload"], answer):
        failures.append("runtime verifier rejected the answer shape")

    return {
        "id": case["id"],
        "question": case["question"],
        "tool_id": data.get("tool_id"),
        "routed_tool_id": data.get("routed_tool_id"),
        "route_args": route_args,
        "queued": data.get("queued"),
        "answer": answer,
        "failures": failures,
    }


def run_golden_questions(review_db: str | Path | None = None) -> list[dict[str, Any]]:
    old_db = os.getenv("ASSISTANT_REVIEW_DB")
    old_answer = runtime._gemini_answer
    old_notice = runtime._gemini_review_notice
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if review_db is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="assistant-golden-")
        review_db = Path(temp_dir.name) / "assistant_review.sqlite"
    os.environ["ASSISTANT_REVIEW_DB"] = str(review_db)
    runtime._gemini_answer = lambda *_args, **_kwargs: (None, None)
    runtime._gemini_review_notice = lambda *_args, **_kwargs: (None, None)
    try:
        return [_case_result(case) for case in GOLDEN_QUESTIONS]
    finally:
        runtime._gemini_answer = old_answer
        runtime._gemini_review_notice = old_notice
        if old_db is None:
            os.environ.pop("ASSISTANT_REVIEW_DB", None)
        else:
            os.environ["ASSISTANT_REVIEW_DB"] = old_db
        if temp_dir is not None:
            temp_dir.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local Cenas assistant golden questions.")
    parser.add_argument("--review-db", help="Local throwaway assistant_review.sqlite path.")
    parser.add_argument("--json", action="store_true", help="Print full JSON result details.")
    args = parser.parse_args()

    results = run_golden_questions(args.review_db)
    failures = [result for result in results if result["failures"]]
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print(f"assistant golden questions: {len(results) - len(failures)}/{len(results)} passed")
        for result in failures:
            print(f"- {result['id']}: {'; '.join(result['failures'])}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
