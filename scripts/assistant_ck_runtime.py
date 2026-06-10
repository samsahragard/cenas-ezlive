"""CK-local runtime for the permission-scoped in-app assistant.

Run on Mini_IT13. The web app sends the authenticated principal context and
question here; CK does model calls and durable review storage locally.

Environment:
  ASSISTANT_RUNTIME_TOKEN        required token for /assistant/answer
  ASSISTANT_REVIEW_DB            optional DB path; defaults to CK review DB
  ASSISTANT_RUNTIME_HOSTS        optional comma-separated bind hosts
  ASSISTANT_RUNTIME_HOST         optional single bind host; default 127.0.0.1
  ASSISTANT_RUNTIME_PORT         optional port; default 8782
  GEMINI_API_KEY                 optional Gemini key
  GEMINI_API_KEY_FILE            optional file path for Gemini key
  AI_ASSISTANT_GEMINI_MODEL      optional Gemini model; default gemini-2.5-flash
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from scripts import assistant_review_ck_receiver as review_receiver
except ImportError:  # pragma: no cover - allows running from scripts dir
    import assistant_review_ck_receiver as review_receiver  # type: ignore

from app.services.assistant_routing_shared import (
    DATA_TOOL_RE as _DATA_TOOL_RE,
    DEFAULT_GEMINI_MODEL as _DEFAULT_GEMINI_MODEL,
    FOLLOWUP_RE as _FOLLOWUP_RE,
    MAX_QUESTION_CHARS as _MAX_QUESTION_CHARS,
    OPERATIONAL_NOUN_RE as _OPERATIONAL_NOUN_RE,
    REVIEW_STATUS as _REVIEW_STATUS,
    SENSITIVE_RE as _SENSITIVE_RE,
    SECRET_DEFAULTS as _SECRET_DEFAULTS,
    TOAST_DATA_FRESHNESS_RE as _TOAST_DATA_FRESHNESS_RE,
    TOAST_EMPLOYEE_PROFILE_RE as _TOAST_EMPLOYEE_PROFILE_RE,
    TOAST_SALES_RE as _TOAST_SALES_RE,
    TOAST_SALES_UNSUPPORTED_SCOPE_RE as _TOAST_SALES_UNSUPPORTED_SCOPE_RE,
    TOAST_TABLE_ACTIVITY_RE as _TOAST_TABLE_ACTIVITY_RE,
    TOAST_WEBHOOK_ACTIVITY_RE as _TOAST_WEBHOOK_ACTIVITY_RE,
    contextual_followup as _shared_contextual_followup,
    force_review_reason as _shared_force_review_reason,
    has_unsupported_toast_sales_scope as _has_unsupported_toast_sales_scope,
    now_iso as _now_iso,
    provider_timeout_ms as _provider_timeout_ms,
    queued_answer as _queued_answer,
    read_secret as _read_secret,
    normalize_store_key as _normalize_store_key,
    requested_store as _requested_store,
    requested_store_list as _requested_store_list,
    resolved_question as _shared_resolved_question,
    review_reason_label as _review_reason_label,
    review_risk_level as _review_risk_level,
    stable_hash as _stable_hash,
    toast_period_from_question as _toast_period_from_question,
    toast_table_business_date_from_question as _toast_table_business_date_from_question,
    today_ct as _today_ct,
    wants_toast_data_freshness as _wants_toast_data_freshness,
    wants_toast_employee_profiles as _wants_toast_employee_profiles,
    wants_toast_sales_summary as _wants_toast_sales_summary,
    wants_toast_table_activity as _wants_toast_table_activity,
    wants_toast_webhook_activity as _wants_toast_webhook_activity,
)
from app.services.assistant_context import load_assistant_context as _load_assistant_context
from app.services.assistant_tool_inventory import iter_readonly_operational_tool_specs


log = logging.getLogger(__name__)

ANSWER_PATH = "/assistant/answer"
_MAX_REQUEST_BODY_BYTES = 1024 * 256
_TOOL_ROUTE_REQUIRED_VERIFICATIONS = 3
_READONLY_OPERATIONAL_TOOL_IDS = frozenset(
    str(spec.get("tool_id") or "")
    for spec in iter_readonly_operational_tool_specs()
    if spec.get("tool_id")
)
_VERIFIED_ROUTE_TOOL_IDS = {
    "orders.store_summary",
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
    "drivers.store_summary",
    "labor.store_aggregate",
    "toast.sales_summary",
    "toast.table_activity",
    "toast.webhook_activity",
    "toast.employee_profiles",
}
_VERIFIED_ROUTE_TOOL_IDS.update(_READONLY_OPERATIONAL_TOOL_IDS)
_TOAST_ENV_FILES = [
    r"C:\Users\sam\cena-secrets\toast_render_env.txt",
    r"C:\Users\sam\cena\.secrets\toast_render_env.txt",
]
_TOAST_ENV_NAMES = {
    "TOAST_ANALYTICS_CLIENT_ID",
    "TOAST_ANALYTICS_CLIENT_SECRET",
    "TOAST_CLIENT_ID",
    "TOAST_CLIENT_SECRET",
    "TOAST_RESTAURANT_GUID_COPPERFIELD",
    "TOAST_RESTAURANT_GUID_TOMBALL",
}
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_LABOR_RATIO_MIN_ORDERS = 10
_LABOR_RATIO_MIN_NET_SALES = 500.0
_OWNER_IDENTITY_RE = re.compile(
    r"^\s*(?:i\s+am|i'm|im|this\s+is)\s+(?:sam|masood)\b",
    re.IGNORECASE,
)


def _load_toast_env_defaults() -> None:
    if all(os.getenv(name) for name in _TOAST_ENV_NAMES):
        return
    for raw_path in _TOAST_ENV_FILES:
        path = Path(raw_path)
        try:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)\s*$", line)
                if not match:
                    continue
                name = match.group(1)
                value = match.group(2).strip()
                if name not in _TOAST_ENV_NAMES or os.getenv(name):
                    continue
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                os.environ[name] = value
        except OSError:
            continue
        if all(os.getenv(name) for name in _TOAST_ENV_NAMES):
            break


def _role(principal: dict) -> str:
    return str(principal.get("role") or "unknown")


def _can_ask_personal(principal: dict) -> bool:
    if principal.get("can_ask_personal") is True:
        return True
    role = _role(principal)
    permissions = set(principal.get("permissions") or [])
    return role in {"partner", "driver", "employee"} or bool(
        {"ai.ask_claude", "ai.ask_claude_personal"} & permissions
    )


def _can_ask_operational(principal: dict) -> bool:
    if principal.get("can_ask_operational") is True:
        return True
    role = _role(principal)
    permissions = set(principal.get("permissions") or [])
    return role == "partner" or "ai.ask_claude" in permissions


def _has_partner_tool_access(principal: dict) -> bool:
    return bool(
        principal.get("is_owner_operator")
        or _role(principal) == "partner"
        or principal.get("kind") in {"partner", "staff", "employee"}
    )


def _tool_available(tools: list[dict], tool_id: str) -> bool:
    for tool in tools:
        if isinstance(tool, dict) and tool.get("tool_id") == tool_id and tool.get("available") is True:
            return True
    return False


def _wants_tool_discovery(question: str) -> bool:
    text = str(question or "").casefold()
    return bool(
        re.search(r"\b(what|which|show|list|tell)\b", text)
        and re.search(r"\b(tools?|capabilities|available|active)\b", text)
    )


def _tool_discovery_answer(principal: dict, tools: list[dict]) -> str:
    available = [
        tool for tool in tools
        if isinstance(tool, dict) and tool.get("available") is True
    ]
    total = len(available)
    catalog_only = sum(
        1
        for tool in available
        if isinstance(tool, dict) and tool.get("implementation_status") == "catalog_only"
    )
    implemented = max(total - catalog_only, 0)
    role = _role(principal)
    sample_ids = [
        str(tool.get("tool_id"))
        for tool in available
        if tool.get("tool_id")
    ][:24]
    sample = ", ".join(sample_ids)
    answer = f"This {role} session has {total} active Cenas AI catalog {_plural(total, 'tool')}."
    if implemented or catalog_only:
        answer += (
            f" {implemented} {_count_verb(implemented, 'is', 'are')} wired to approved executable paths now; "
            f"{catalog_only} partner catalog {_plural(catalog_only, 'entry', 'entries')} "
            f"{_count_verb(catalog_only, 'is', 'are')} waiting on implementation."
        )
    if sample:
        answer += f" First tools: {sample}."
    return answer


def _wants_order_summary(question: str) -> bool:
    text = question.casefold()
    if re.search(r"\borders?\b.*\bdriver\s+attention\b", text):
        return True
    if re.search(r"\borders?\b.*\b(?:need|needs|needing)\s+(?:a\s+)?driver\b", text):
        return True
    if re.search(r"\btracking\s+links?\b", text):
        return True
    if re.search(
        r"\b(catering|caterings|order|orders|delivery|deliveries)\b",
        text,
    ) and re.search(
        r"\b(today|morning|afternoon|evening|tonight|tomorrow|yesterday|"
        r"split|by store|store split|have|current|active|totals?)\b",
        text,
    ):
        return True
    return bool(
        re.search(r"\b(how (?:many|amny)|count|total|totals|summary|report)\b", text)
        and re.search(r"\b(catering|caterings|order|orders|delivery|deliveries)\b", text)
    )


def _wants_driver_summary(question: str) -> bool:
    text = question.casefold()
    if re.search(r"\borders?\b.*\bdriver\s+attention\b", text):
        return False
    if re.search(r"\borders?\b.*\b(?:need|needs|needing)\s+(?:a\s+)?driver\b", text):
        return False
    return bool(
        re.search(
            r"\b(how many|count|total|summary|report|active|score|current|"
            r"coverage|availability|aggregate|roster|staffing|location|"
            r"on shift|active orders)\b",
            text,
        )
        and re.search(r"\b(driver|drivers)\b", text)
    )


def _wants_labor_summary(question: str) -> bool:
    text = question.casefold()
    return bool(
        re.search(r"\b(how many|count|total|summary|report|schedule|attendance|labor|employee|employees|staff|staffing|team|current)\b", text)
        and re.search(r"\b(labor|employee|employees|staff|staffing|team|schedule|attendance|shift|shifts)\b", text)
    )


def _toast_tool_authorized(principal: dict, tools: list[dict]) -> bool:
    if not _has_partner_tool_access(principal):
        return False
    return _tool_available(tools, "toast.sales_summary")


def _toast_table_tool_authorized(principal: dict, tools: list[dict]) -> bool:
    if not _has_partner_tool_access(principal):
        return False
    return _tool_available(tools, "toast.table_activity")


def _toast_webhook_tool_authorized(principal: dict, tools: list[dict]) -> bool:
    if not _has_partner_tool_access(principal):
        return False
    return _tool_available(tools, "toast.webhook_activity")


def _toast_employee_profiles_tool_authorized(principal: dict, tools: list[dict]) -> bool:
    if not _has_partner_tool_access(principal):
        return False
    return _tool_available(tools, "toast.employee_profiles")


def _toast_sales_summary_payload(period: str) -> dict:
    _load_toast_env_defaults()
    from app.services.toast_analytics_summary import analytics_summary_payload

    return analytics_summary_payload(period)


def _toast_table_activity_payload(location: str | None, business_date: str | None = None) -> dict:
    _load_toast_env_defaults()
    from app.services.toast_table_activity import latest_table_activity_payload

    return latest_table_activity_payload(location, business_date=business_date)


def _toast_webhook_activity_payload(question: str) -> dict:
    from app.services.toast_webhook_assistant import toast_webhook_activity_payload

    return toast_webhook_activity_payload(
        question,
        store_key=_requested_store(question),
        business_date=_toast_table_business_date_from_question(question),
    )


def _toast_employee_profiles_payload(question: str) -> dict:
    from app.services.toast_webhook_assistant import toast_employee_profile_payload

    return toast_employee_profile_payload(question)


def _money(value: object) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"${amount:,.2f}"


def _float_value(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _format_ymd(value: object) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _date_range_text(summary: dict) -> str:
    date_range = summary.get("date_range") or {}
    if isinstance(date_range, dict):
        start = _format_ymd(date_range.get("start"))
        end = _format_ymd(date_range.get("end"))
        if start and end:
            if start == end and str(summary.get("period") or "").strip() == "week":
                return f"{start} to {end}"
            return start if start == end else f"{start} to {end}"
        return start or end or str(date_range.get("label") or "").strip()
    return str(summary.get("date_range_label") or "").strip()


def _labor_ratio_ok(summary: dict, sales: dict, labor: dict) -> bool:
    explicit = labor.get("ratio_denominator_ok")
    if explicit is not None:
        return bool(explicit)
    guard = labor.get("ratio_guard") or {}
    if isinstance(guard, dict) and guard.get("ok") is not None:
        return bool(guard.get("ok"))
    orders = int(sales.get("orders") or 0)
    net = _float_value(sales.get("net"))
    return orders >= _LABOR_RATIO_MIN_ORDERS and net >= _LABOR_RATIO_MIN_NET_SALES


def _labor_ratio_guard_note(sales: dict, labor: dict) -> str:
    guard = labor.get("ratio_guard") or {}
    if isinstance(guard, dict):
        note = str(guard.get("note") or "").strip()
        if note:
            return note
    orders = int(sales.get("orders") or 0)
    net = _float_value(sales.get("net"))
    return (
        "this period is below the denominator guard "
        f"({orders} {_plural(orders, 'order')}, {_money(net)} net sales)"
    )


def _toast_sales_summary_answer(summary: dict) -> str:
    label = str(summary.get("label") or "Today").strip() or "Today"
    period = str(summary.get("period") or "").strip()
    scope_note = str(summary.get("scope_note") or "").strip()
    date_scope = _date_range_text(summary)
    sales = summary.get("sales") or {}
    labor = summary.get("labor") or {}

    orders = int(sales.get("orders") or 0)
    guests = int(sales.get("guests") or 0)
    net = sales.get("net") or 0
    gross = sales.get("gross") or 0
    avg_order = sales.get("avg_order") or 0
    discount = sales.get("discount") or 0
    refund = sales.get("refund") or 0
    void = sales.get("void") or 0

    heading = f"{label} Toast Analytics"
    if date_scope:
        heading += f" (date range: {date_scope})"
    answer = (
        f"{heading}: net sales are {_money(net)} on {orders} "
        f"{_plural(orders, 'order')} (avg {_money(avg_order)}). "
        f"Gross sales are {_money(gross)}"
    )
    adjustments = []
    if float(discount or 0):
        adjustments.append(f"discounts {_money(discount)}")
    if float(refund or 0):
        adjustments.append(f"refunds {_money(refund)}")
    if float(void or 0):
        adjustments.append(f"voids {_money(void)}")
    if adjustments:
        answer += ", with " + ", ".join(adjustments)
    answer += "."

    if guests:
        answer += f" Guest count is {guests}."
    if period == "week":
        date_range = summary.get("date_range") or {}
        if isinstance(date_range, dict):
            start = _format_ymd(date_range.get("start"))
            end = _format_ymd(date_range.get("end"))
            if start and end and start == end:
                answer += " Date note: This Week is Sunday-to-date, so on Sunday it matches Today."

    labor_hours = float(labor.get("hours") or 0)
    labor_cost = labor.get("cost") or 0
    labor_ratio = labor.get("ratio_pct")
    sales_per_labor_hour = sales.get("sales_per_labor_hour")
    if labor_hours or float(labor_cost or 0):
        answer += f" Labor is {labor_hours:g} hours, {_money(labor_cost)} cost"
        ratio_ok = _labor_ratio_ok(summary, sales, labor)
        if labor_ratio is not None and ratio_ok:
            answer += f" ({labor_ratio}% of sales)"
        if sales_per_labor_hour is not None:
            answer += f", {_money(sales_per_labor_hour)} sales per labor hour"
        answer += "."
        if labor_ratio is not None and not ratio_ok:
            answer += f" Labor percent is not shown because {_labor_ratio_guard_note(sales, labor)}."

    if scope_note:
        answer += f" Scope: {scope_note}"
    return answer


def _toast_table_activity_answer(summary: dict) -> str:
    return _toast_table_activity_answer_for_question(summary, "")


def _count_list(rows: list[dict], key: str = "fact_type", count_key: str = "count", limit: int = 6) -> str:
    bits = []
    for row in rows[:limit]:
        label = str(row.get(key) or "unknown").replace("_", " ")
        count_value = row.get(count_key)
        if count_value is None:
            count_value = row.get("count")
        if count_value is None:
            count_value = row.get("fact_count")
        count = int(count_value or 0)
        bits.append(f"{label}: {count}")
    return "; ".join(bits)


def _safe_answer_label(value: object, *, max_len: int = 180) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    if _UUID_RE.search(text):
        return ""
    if (
        text.startswith("{")
        or "entitytype" in lowered
        or "externalid" in lowered
        or "'guid'" in lowered
        or '"guid"' in lowered
    ):
        return ""
    return text[:max_len]


def _toast_webhook_activity_answer(summary: dict, question: str = "") -> str:
    if not isinstance(summary, dict) or not summary.get("ok"):
        return "I cannot read the Toast webhook database right now."
    counts = summary.get("counts") or {}
    scope = summary.get("scope") or {}
    store = scope.get("store_key") or "all stores"
    business_date = scope.get("business_date") or "today"
    event_count = int(counts.get("events") or 0)
    order_count = int(counts.get("orders") or 0)
    check_count = int(counts.get("checks") or 0)
    selection_count = int(counts.get("selections") or 0)
    payment_count = int(counts.get("payments") or 0)
    employee_fact_count = int(counts.get("employee_facts") or 0)
    answer = (
        "The Toast webhook database is connected. "
        f"It currently has {event_count:,} webhook {_plural(event_count, 'event')}, "
        f"{order_count:,} current {_plural(order_count, 'order')}, "
        f"{check_count:,} {_plural(check_count, 'check')}, "
        f"{selection_count:,} {_plural(selection_count, 'selection')}/items, "
        f"{payment_count:,} {_plural(payment_count, 'payment')}, and "
        f"{employee_fact_count:,} employee Toast {_plural(employee_fact_count, 'fact')}."
    )
    recent = int(summary.get("recent_last_hour_events") or 0)
    answer += f" It has accepted {recent:,} Toast webhook {_plural(recent, 'event')} in the last hour."
    fact_types = summary.get("fact_types_for_scope") or []
    if fact_types:
        answer += f" For {store} on {business_date}, employee fact types are: {_count_list(fact_types)}."
    latest_orders = summary.get("latest_orders") or []
    if latest_orders:
        latest = latest_orders[0]
        table = _safe_answer_label(latest.get("table_name"), max_len=80)
        server = _safe_answer_label(latest.get("server_name"), max_len=80)
        parts = []
        if table:
            parts.append(f"table {table}")
        if server:
            parts.append(f"server {server}")
        latest_items = int(latest.get("selection_count") or 0)
        latest_payments = int(latest.get("payment_count") or 0)
        parts.append(f"{latest_items} {_plural(latest_items, 'item')}")
        parts.append(f"{latest_payments} {_plural(latest_payments, 'payment')}")
        when = latest.get("modified_date") or latest.get("opened_date") or latest.get("closed_date")
        answer += " Latest current order snapshot: " + ", ".join(parts)
        if when:
            answer += f", updated around {when}"
        answer += "."
    if summary.get("raw_payloads_included") is False:
        answer += " Raw Toast webhook JSON is not included in this assistant payload."
    return answer


def _fact_summary_text(fact: dict) -> str:
    fact_type = str(fact.get("fact_type") or "activity").replace("_", " ")
    when = str(fact.get("occurred_at") or "").strip()
    summary = fact.get("summary") if isinstance(fact.get("summary"), dict) else {}
    details = []
    table = _safe_answer_label(summary.get("table"), max_len=80)
    name = _safe_answer_label(summary.get("name"), max_len=120)
    status = _safe_answer_label(summary.get("payment_status"), max_len=40)
    if table:
        details.append(f"table {table}")
    if name:
        details.append(name)
    if summary.get("amount") is not None:
        details.append(_money(summary.get("amount")))
    if summary.get("total_amount") is not None:
        details.append(_money(summary.get("total_amount")))
    if status:
        details.append(status)
    text = fact_type
    if when:
        text += f" at {when}"
    if details:
        text += " (" + ", ".join(details[:3]) + ")"
    return text


def _toast_employee_profiles_answer(summary: dict, question: str = "") -> str:
    if not isinstance(summary, dict) or not summary.get("ok"):
        return "I cannot read the Toast employee profile databases right now."
    if summary.get("scope") == "overview":
        central = summary.get("central_counts") or {}
        profile_db_count = int(summary.get("profile_db_count") or 0)
        employee_profiles = int(central.get("employee_profiles") or 0)
        identity_links = int(central.get("identity_links") or 0)
        employee_facts = int(central.get("employee_facts") or 0)
        answer = (
            "The Toast employee profile databases are connected. "
            f"{_there_is_are(profile_db_count)} {profile_db_count} per-employee SQLite {_plural(profile_db_count, 'file')}, "
            f"{employee_profiles} central employee {_plural(employee_profiles, 'profile')}, "
            f"{identity_links} Toast identity {_plural(identity_links, 'link')}, and "
            f"{employee_facts:,} employee Toast {_plural(employee_facts, 'fact')}."
        )
        unmatched = int(central.get("unmatched_employee_refs") or 0)
        if unmatched:
            answer += (
                f" {_there_is_are(unmatched)} {unmatched} unmatched Toast employee "
                f"{_plural(unmatched, 'reference')} waiting on mapping."
            )
        top = summary.get("top_employees_by_toast_facts") or []
        if top:
            bits = [
                f"{row.get('name')} ({int(row.get('fact_count') or 0):,})"
                for row in top[:5]
            ]
            answer += " Most active employee profiles by Toast facts: " + "; ".join(bits) + "."
        answer += " Raw webhook JSON is not copied into the employee DBs."
        return answer

    employee = summary.get("employee") or {}
    personal = summary.get("personal_db") or {}
    name = str(employee.get("name") or f"Employee {employee.get('cena_employee_id')}").strip()
    employee_id = employee.get("cena_employee_id")
    answer = f"{name}"
    if employee_id is not None:
        answer += f" (employee {employee_id})"
    if personal.get("exists"):
        toast_facts = int(personal.get("toast_fact_count") or 0)
        related_orders = int(personal.get("related_orders") or 0)
        related_checks = int(personal.get("related_checks") or 0)
        related_selections = int(personal.get("related_selections") or 0)
        related_payments = int(personal.get("related_payments") or 0)
        answer += (
            f" has a personal Toast profile DB with {toast_facts:,} {_plural(toast_facts, 'fact')}, "
            f"{related_orders:,} related {_plural(related_orders, 'order')}, "
            f"{related_checks:,} {_plural(related_checks, 'check')}, "
            f"{related_selections:,} {_plural(related_selections, 'selection')}/items, and "
            f"{related_payments:,} {_plural(related_payments, 'payment')}."
        )
        metadata = personal.get("metadata") or {}
        if metadata.get("generated_at"):
            answer += f" The DB was last materialized at {metadata['generated_at']}."
        fact_counts = personal.get("fact_type_counts") or summary.get("central_fact_type_counts") or []
        if fact_counts:
            answer += " Fact types: " + _count_list(fact_counts, count_key="fact_count") + "."
        latest = personal.get("latest_facts") or []
        if latest:
            answer += " Latest Toast activity: " + "; ".join(_fact_summary_text(row) for row in latest[:3]) + "."
    else:
        answer += " does not have a materialized personal Toast profile DB yet."
        fact_counts = summary.get("central_fact_type_counts") or []
        if fact_counts:
            answer += " Central Toast facts exist: " + _count_list(fact_counts) + "."
    if summary.get("raw_payloads_included") is False:
        answer += " Raw webhook JSON is not included in this assistant payload."
    return answer


def _tool_payload_ok(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get("ok") is not False


def _toast_table_person_intent(question: str) -> tuple[bool, bool]:
    text = str(question or "").casefold()
    wants_opened_by = bool(re.search(r"\b(who\s+opened|opened\s+by|opened\s+it)\b", text))
    wants_server = bool(re.search(r"\b(waiter|server)\b", text))
    return wants_opened_by, wants_server


def _toast_table_activity_answer_for_question(summary: dict, question: str) -> str:
    location_label = str(summary.get("location_label") or "the requested location").strip()
    business_date = str(summary.get("business_date") or "").strip()
    date_label = "today"
    if re.fullmatch(r"\d{8}", business_date):
        formatted = f"{business_date[:4]}-{business_date[4:6]}-{business_date[6:]}"
        if business_date != _today_ct().strftime("%Y%m%d"):
            date_label = f"on {formatted}"
    latest = summary.get("latest") if isinstance(summary, dict) else None
    if not isinstance(latest, dict):
        return f"I do not see any in-store table opens for {location_label} {date_label} in Toast."

    opened_local = str(latest.get("opened_at_local") or "").strip()
    table_name = str(latest.get("table_name") or "").strip()
    if table_name:
        answer = (
            f"The most recent {location_label} in-store table open I see {date_label} is "
            f"table {table_name}"
        )
    else:
        answer = (
            f"I can see the latest {location_label} in-store table open event, "
            "but Toast did not return a table label for it"
        )
    if opened_local:
        answer += f", opened at {opened_local}"
    answer += "."
    opened_by = str(latest.get("opened_by_name") or "").strip()
    server_name = str(latest.get("server_name") or "").strip()
    wants_opened_by, wants_server = _toast_table_person_intent(question)
    if wants_opened_by and not opened_by:
        if server_name:
            answer += (
                f" Toast returned the waiter/server as {server_name}, but did not "
                "return an opened-by employee for that check."
            )
        elif latest.get("employee_lookup_available") is False:
            answer += " Toast returned the table event, but employee lookup was unavailable, so I cannot name who opened it yet."
        else:
            answer += " Toast did not return the opened-by employee for that check."
    elif wants_server and not server_name:
        if opened_by:
            answer += (
                f" It was opened by {opened_by}, but Toast did not return a "
                "waiter/server for that check."
            )
        elif latest.get("employee_lookup_available") is False:
            answer += " Toast returned the table event, but employee lookup was unavailable, so I cannot name the waiter/server yet."
        else:
            answer += " Toast did not return the waiter/server for that check."
    elif opened_by and server_name and opened_by != server_name:
        answer += f" It was opened by {opened_by}; the waiter/server was {server_name}."
    elif opened_by:
        answer += f" It was opened by {opened_by}."
    elif server_name:
        answer += f" The waiter/server was {server_name}."
    elif latest.get("employee_lookup_available") is False:
        answer += " Toast returned the table event, but employee lookup was unavailable, so I cannot name the waiter/server yet."
    if not latest.get("table_config_available"):
        answer += " Table-name config was unavailable, so I did not expose the raw Toast table ID."
    return answer


def _toast_table_activity_needs_employee_refresh(summary: object, question: str = "") -> bool:
    if not isinstance(summary, dict):
        return False
    latest = summary.get("latest")
    if not isinstance(latest, dict):
        return False
    if latest.get("employee_lookup_available") is False:
        return False
    opened_by = str(latest.get("opened_by_name") or "").strip()
    server_name = str(latest.get("server_name") or "").strip()
    wants_opened_by, wants_server = _toast_table_person_intent(question)
    if wants_opened_by and not opened_by:
        return not server_name
    if wants_server and not server_name:
        return True
    if opened_by or server_name:
        return False
    return latest.get("employee_lookup_available") is not False


def _toast_table_person_followup_answer(question: str, previous_answer: str) -> str | None:
    if not previous_answer.strip():
        return None
    if not re.search(r"\b(who|waiter|server|opened by|opened it)\b", question, re.IGNORECASE):
        return None
    if "in-store table open" not in previous_answer or "waiter/server was" not in previous_answer:
        return None
    server_match = re.search(r"\bwaiter/server was ([^.]+)", previous_answer)
    if not server_match:
        return None
    table_matches = re.findall(r"(?:is|was)\s+table\s+([^,.]+)", previous_answer, re.IGNORECASE)
    time_match = re.search(r"\bopened at ([^.]+? CT)\b", previous_answer)
    server_name = server_match.group(1).strip()
    answer = f"The waiter/server was {server_name}"
    details = []
    if table_matches:
        details.append(f"table {table_matches[-1].strip()}")
    if time_match:
        details.append(f"opened at {time_match.group(1).strip()}")
    if details:
        answer += " for " + ", ".join(details)
    return answer + "."


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


def _count_verb(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _there_is_are(count: int) -> str:
    return _count_verb(count, "There is", "There are")


def _requested_today_window(question: str) -> tuple[str, str] | None:
    text = question.casefold()
    if "earlier this morning" in text or "this morning" in text or re.search(r"\bmorning\b", text):
        return "morning", "earlier this morning"
    if "earlier today" in text:
        return "earlier_today", "earlier today"
    if re.search(r"\bafternoon\b", text):
        return "afternoon", "this afternoon"
    if re.search(r"\b(evening|tonight)\b", text):
        return "evening", "tonight"
    return None


def _store_count(mapping: dict, store: str | None, default_total: int) -> int:
    if not store:
        return default_total
    return int((mapping or {}).get(store, 0) or 0)


def _store_label(store: str) -> str:
    normalized = _normalize_store_key(store)
    labels = {
        "copperfield": "Copperfield",
        "tomball": "Tomball",
    }
    return labels.get(normalized, normalized)


def _normalized_store_counts(mapping: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(mapping, dict):
        return counts
    for raw_store, raw_count in mapping.items():
        store = _normalize_store_key(raw_store)
        try:
            count = int(raw_count or 0)
        except (TypeError, ValueError):
            count = 0
        counts[store] = counts.get(store, 0) + count
    return counts


def _store_split(mapping: dict) -> str:
    return "; ".join(
        f"{_store_label(store)}: {count}" for store, count in sorted((mapping or {}).items())
    )


def _orders_summary_answer(summary: dict, question: str = "") -> str:
    requested_stores = _requested_store_list(question)
    requested_store = requested_stores[0] if len(requested_stores) == 1 else None
    requested_window = _requested_today_window(question)
    today_date = str(summary.get("today") or "").strip()
    if requested_window:
        window_key, label = requested_window
        window_counts = summary.get("today_time_windows") or {}
        window_by_store = summary.get("today_time_windows_by_store") or {}
        count = int(window_counts.get(window_key) or 0)
        store_counts = _normalized_store_counts(window_by_store.get(window_key) or {})
        if requested_store:
            count = int(store_counts.get(requested_store) or 0)
        date_suffix = f" ({today_date})" if today_date else ""
        if requested_store:
            answer = (
                f"For {label}{date_suffix}, {_store_label(requested_store)} has "
                f"{count} {_plural(count, 'catering order')}."
            )
        else:
            answer = f"For {label}{date_suffix}, {_there_is_are(count).casefold()} {count} {_plural(count, 'catering order')}."
        split = _store_split(store_counts)
        if split and not requested_store:
            answer += " Store split: " + split + "."
        return answer

    today_orders = int(summary.get("today_orders") or 0)
    upcoming_orders = int(summary.get("upcoming_orders") or 0)
    needs_driver = int(summary.get("needs_driver_orders") or 0)
    live_tracking = int(summary.get("live_tracking_orders") or 0)
    active_tracking = int(summary.get("active_tracking_orders") or 0)
    by_store = _normalized_store_counts(summary.get("today_by_store") or summary.get("by_store") or {})
    today_orders = _store_count(by_store, requested_store, today_orders)
    store_bits = [f"{_store_label(store)}: {count}" for store, count in sorted(by_store.items())]
    if len(requested_stores) >= 2:
        compare_bits = [
            f"{_store_label(store)}: {int((by_store or {}).get(store, 0) or 0)}"
            for store in requested_stores
        ]
        answer = "Today catering orders by requested store: " + "; ".join(compare_bits) + "."
    elif requested_store:
        answer = f"{_store_label(requested_store)} has {today_orders} {_plural(today_orders, 'catering order')} today."
    else:
        answer = (
            f"You have {today_orders} {_plural(today_orders, 'catering order')} today"
            f" and {upcoming_orders} upcoming {_plural(upcoming_orders, 'order')} in the current view."
        )
    if needs_driver:
        answer += f" {needs_driver} still {_count_verb(needs_driver, 'needs', 'need')} driver attention."
    else:
        answer += " No orders currently need driver attention."
    answer += f" {live_tracking} {_plural(live_tracking, 'order')} {_count_verb(live_tracking, 'has', 'have')} tracking links"
    if active_tracking:
        answer += f", with {active_tracking} currently active"
    answer += "."
    if store_bits:
        answer += " Store split: " + "; ".join(store_bits) + "."
    return answer


def _order_id_list(rows: list[dict], limit: int = 5) -> str:
    ids = [
        str(row.get("external_order_id") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("external_order_id") or "").strip()
    ]
    return ", ".join(ids[:limit])


def _dict_split(mapping: dict, limit: int = 6) -> str:
    if not isinstance(mapping, dict) or not mapping:
        return ""
    pairs = sorted(mapping.items(), key=lambda item: str(item[0]))
    return "; ".join(f"{key}: {value}" for key, value in pairs[:limit])


def _store_dict_split(mapping: dict, limit: int = 6) -> str:
    normalized = _normalized_store_counts(mapping)
    if not normalized:
        return ""
    pairs = sorted(normalized.items(), key=lambda item: str(item[0]))
    return "; ".join(f"{_store_label(key)}: {value}" for key, value in pairs[:limit])


def _orders_read_answer(payload: dict, tool_id: str, question: str = "") -> str:
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return "I could not read the approved catering data for that question, so I saved it for Sam review."
    if payload.get("found") is False:
        searched = str(payload.get("searched_token") or "").strip()
        if searched:
            return f"I did not find a matching visible catering record for {searched}."
        return "I did not find a matching visible catering record for that question."

    if tool_id in {
        "orders.catering_today",
        "orders.catering_tomorrow",
        "orders.catering_week",
        "orders.catering_next_30_days",
    }:
        count = int(payload.get("count") or 0)
        window = str(payload.get("window") or "requested window").replace("_", " ")
        answer = f"{_there_is_are(count)} {count} {_plural(count, 'catering order')} in the {window} view."
        split = _store_dict_split(payload.get("by_store") or {})
        if split:
            answer += " Store split: " + split + "."
        ids = _order_id_list(payload.get("orders") or [])
        if ids:
            answer += " First visible orders: " + ids + "."
        return answer

    if tool_id == "orders.catering_count":
        return (
            "Catering counts: "
            f"today {int(payload.get('today_count') or 0)}, "
            f"tomorrow {int(payload.get('tomorrow_count') or 0)}, "
            f"next 7 days {int(payload.get('next_7_days_count') or 0)}, "
            f"next 30 days {int(payload.get('next_30_days_count') or 0)}, "
            f"all visible {int(payload.get('total_count') or 0)}."
        )

    if tool_id == "orders.catering_by_store":
        split = _store_dict_split(payload.get("by_store") or {}) or "no visible stores"
        return f"Catering store split: {split}."

    if tool_id == "orders.catering_by_status":
        split = _dict_split(payload.get("by_status") or {}) or "no visible statuses"
        return f"Catering status split: {split}."

    if tool_id == "orders.catering_needs_driver":
        count = int(payload.get("count") or 0)
        answer = f"{count} {_plural(count, 'catering order')} {_count_verb(count, 'needs', 'need')} driver attention."
        ids = _order_id_list(payload.get("orders") or [])
        if ids:
            answer += " First visible orders: " + ids + "."
        return answer

    if tool_id == "orders.catering_live_tracking":
        count = int(payload.get("count") or 0)
        active = int(payload.get("active_count") or 0)
        answer = (
            f"{count} {_plural(count, 'catering order')} {_count_verb(count, 'has', 'have')} tracking links; "
            f"{active} {_count_verb(active, 'is', 'are')} currently active."
        )
        split = _dict_split(payload.get("by_status") or {})
        if split:
            answer += " Tracking status split: " + split + "."
        return answer

    if tool_id == "orders.catering_tracking_missing":
        count = int(payload.get("count") or 0)
        answer = f"{count} active {_plural(count, 'catering order')} {_count_verb(count, 'is', 'are')} missing tracking links."
        split = _store_dict_split(payload.get("by_store") or {})
        if split:
            answer += " Store split: " + split + "."
        return answer

    if tool_id == "orders.catering_uuid_status":
        return (
            "Tracking UUID coverage: "
            f"{int(payload.get('with_tracking_uuid') or 0)} with UUIDs, "
            f"{int(payload.get('missing_tracking_uuid') or 0)} missing, "
            f"{int(payload.get('active_tracking_count') or 0)} active."
        )

    if tool_id == "orders.catering_late_risk":
        count = int(payload.get("count") or 0)
        answer = f"{count} same-day {_plural(count, 'catering order')} {_count_verb(count, 'shows', 'show')} late risk."
        ids = _order_id_list(payload.get("orders") or [])
        if ids:
            answer += " First visible orders: " + ids + "."
        return answer

    if tool_id == "orders.catering_order_lookup":
        order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
        order_id = order.get("external_order_id") or "the selected order"
        return (
            f"Order {order_id}: store {order.get('store') or 'unknown'}, "
            f"date {order.get('delivery_date') or 'unknown'}, "
            f"time {order.get('deliver_at') or 'unknown'}, "
            f"status {order.get('status') or 'unknown'}, "
            f"headcount {order.get('headcount') or 'unknown'}."
        )

    if tool_id == "orders.catering_order_items_safe":
        order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        labels = []
        for item in items[:6]:
            if isinstance(item, dict):
                labels.append(f"{item.get('qty') or 1} x {item.get('label') or item.get('item_key') or 'item'}")
        order_id = order.get("external_order_id") or "the selected order"
        suffix = "; ".join(labels) if labels else "no safe item rows found"
        item_count = int(payload.get("item_count") or 0)
        return f"Order {order_id} has {item_count} item {_plural(item_count, 'row')}: {suffix}."

    if tool_id == "orders.catering_item_mix":
        top_items = payload.get("top_items") if isinstance(payload.get("top_items"), list) else []
        labels = []
        for item in top_items[:6]:
            if isinstance(item, dict):
                labels.append(f"{item.get('label')}: {item.get('qty')}")
        return "Top catering items: " + ("; ".join(labels) if labels else "no visible item rows") + "."

    if tool_id == "orders.catering_fees_summary":
        return (
            f"Visible catering fees: delivery fees {_money(payload.get('delivery_fee_total'))}, "
            f"tips {_money(payload.get('tip_total'))}, "
            f"commission {_money(payload.get('commission_total'))}, "
            f"service fees {_money(payload.get('service_fee_total'))}, "
            f"processing fees {_money(payload.get('processing_fee_total'))}."
        )

    if tool_id == "orders.catering_payout_safe_summary":
        return (
            f"Visible catering payout summary: potential {_money(payload.get('potential_payout_total'))}, "
            f"paid {_money(payload.get('paid_payout_total'))}, "
            f"tips {_money(payload.get('tip_total'))}, "
            f"verified miles {payload.get('verified_miles_total') or 0}."
        )

    if tool_id == "orders.catering_pdf_status":
        split = _dict_split(payload.get("by_processing_status") or {})
        answer = (
            f"Catering PDF status: {int(payload.get('processing_rows') or 0)} processing rows, "
            f"{int(payload.get('pdf_detail_rows') or 0)} detail rows, "
            f"{int(payload.get('with_pdf_source') or 0)} with PDF source, "
            f"{int(payload.get('parse_error_count') or 0)} parse errors."
        )
        if split:
            answer += " Processing split: " + split + "."
        return answer

    if tool_id == "orders.catering_driver_assignment_summary":
        split = _dict_split(payload.get("by_status") or {})
        job_count = int(payload.get("job_count") or 0)
        answer = f"Driver assignment jobs: {job_count} visible {_plural(job_count, 'job')}."
        if split:
            answer += " Status split: " + split + "."
        return answer

    if tool_id == "orders.catering_returning_customers_aggregate":
        return (
            "Returning customer aggregate: "
            f"{int(payload.get('returning_customer_count') or 0)} repeat customer keys, "
            f"{int(payload.get('returning_order_count') or 0)} orders tied to repeat customer keys."
        )

    if tool_id == "orders.in_house_quotes_summary":
        split = _dict_split(payload.get("by_status") or {})
        quote_count = int(payload.get("quote_count") or 0)
        answer = (
            f"{_there_is_are(quote_count)} {quote_count} visible in-house catering {_plural(quote_count, 'quote')} "
            f"totaling {_money(payload.get('subtotal_total'))}."
        )
        if split:
            answer += " Status split: " + split + "."
        return answer

    if tool_id == "orders.in_house_quote_lookup":
        quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else {}
        return (
            f"Quote {quote.get('quote_id') or 'selected'}: store {quote.get('store') or 'unknown'}, "
            f"status {quote.get('status') or 'unknown'}, "
            f"event date {quote.get('event_date') or 'unknown'}, "
            f"guest count {quote.get('guest_count') or 'unknown'}, "
            f"subtotal {_money(quote.get('subtotal'))}."
        )

    return _orders_summary_answer(payload, question)


def _schedule_shift_labels(rows: list[dict], limit: int = 4) -> str:
    labels = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        start = str(row.get("start_at") or "").replace("T", " ")[:16]
        role = row.get("position_name") or "shift"
        employee = row.get("employee_name") or "open"
        if start:
            labels.append(f"{start} {role} ({employee})")
        else:
            labels.append(f"{role} ({employee})")
    return "; ".join(labels)


def _stores_without_visible_rows_text(payload: dict) -> str:
    stores = payload.get("stores_without_visible_rows") or []
    if not isinstance(stores, list) or not stores:
        return ""
    labels = [str(store).strip() for store in stores if str(store).strip()]
    if not labels:
        return ""
    return " No visible schedule rows for: " + ", ".join(labels[:6]) + "."


def _schedule_read_answer(payload: dict, tool_id: str, question: str = "") -> str:
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return "I could not read the approved schedule data for that question, so I saved it for Sam review."

    if tool_id == "schedule.store_today":
        shifts = int(payload.get("shift_count") or 0)
        local_date = str(payload.get("date") or "today").strip()
        window_label = str(payload.get("window_label") or "").casefold()
        question_text = str(question or "").casefold()
        day_scope = "tomorrow" if (
            window_label.startswith("tomorrow_") or re.search(r"\btomorrow\b", question_text)
        ) else "today"
        day_label = "Tomorrow's schedule" if day_scope == "tomorrow" else "Today's schedule"
        answer = (
            f"{day_label} ({day_scope}, {local_date}) from allowed schedule stores: "
            f"{shifts} visible schedule {_plural(shifts, 'shift')}: "
            f"{int(payload.get('assigned_shift_count') or 0)} assigned, "
            f"{int(payload.get('open_shift_count') or 0)} open, "
            f"{float(payload.get('total_hours') or 0):g} hours."
        )
        split = _dict_split(payload.get("by_store") or {})
        if split:
            answer += (
                " Store split for allowed schedule stores: "
                + split
                + "."
            )
        answer += _stores_without_visible_rows_text(payload)
        sample = _schedule_shift_labels(payload.get("shifts") or [])
        if sample:
            answer += " First shifts: " + sample + "."
        return answer

    if tool_id in {"schedule.store_week", "schedule.view"}:
        shifts = int(payload.get("shift_count") or 0)
        schedules = int(payload.get("schedule_count") or 0)
        label = "Current schedule view" if tool_id == "schedule.view" else "Current week schedule"
        week_start = str(payload.get("week_start") or "").strip()
        week_end = str(payload.get("week_end") or "").strip()
        window = f" ({week_start} to {week_end})" if week_start and week_end else ""
        answer = (
            f"{label}{window} has {schedules} {_plural(schedules, 'schedule')} and "
            f"{shifts} {_plural(shifts, 'shift')}: "
            f"{int(payload.get('assigned_shift_count') or 0)} assigned, "
            f"{int(payload.get('open_shift_count') or 0)} open, "
            f"{float(payload.get('total_hours') or 0):g} hours."
        )
        if (
            payload.get("published_schedule_count") is not None
            or payload.get("draft_schedule_count") is not None
        ):
            published_schedules = int(payload.get("published_schedule_count") or 0)
            draft_schedules = int(payload.get("draft_schedule_count") or 0)
            if published_schedules and draft_schedules:
                open_scope = "draft+published visible shifts"
            elif draft_schedules:
                open_scope = "draft visible shifts"
            elif published_schedules:
                open_scope = "published visible shifts"
            else:
                open_scope = "visible shifts with no published/draft schedule status"
            answer += (
                f" Open count includes {open_scope} "
                f"({published_schedules} published {_plural(published_schedules, 'schedule')}, "
                f"{draft_schedules} draft {_plural(draft_schedules, 'schedule')})."
            )
        split = _dict_split(payload.get("by_store") or {})
        if split:
            answer += " Store split: " + split + "."
        answer += _stores_without_visible_rows_text(payload)
        return answer

    if tool_id == "schedule.open_shifts":
        count = int(payload.get("count") or 0)
        as_of = str(payload.get("as_of_date") or "").strip()
        date_suffix = f" as of local date {as_of}" if as_of else ""
        answer = (
            f"Remaining today-forward open schedule {_plural(count, 'shift')} "
            f"in the current view{date_suffix}: {count}."
        )
        schedule_status_split = _dict_split(payload.get("by_schedule_status") or {})
        if schedule_status_split:
            answer += (
                " Open count schedule-status scope: "
                + schedule_status_split
                + "."
            )
        split = _dict_split(payload.get("by_store") or {})
        if split:
            answer += " Store split: " + split + "."
        answer += _stores_without_visible_rows_text(payload)
        sample = _schedule_shift_labels(payload.get("shifts") or [])
        if sample:
            answer += " First open shifts: " + sample + "."
        return answer

    if tool_id == "schedule.shift_acceptance_summary":
        split = _dict_split(payload.get("by_response") or {}) or "no responses yet"
        return (
            "Shift acceptance summary: "
            f"{int(payload.get('assigned_shift_count') or 0)} assigned shifts, "
            f"{int(payload.get('response_count') or 0)} responses, "
            f"{int(payload.get('pending_count') or 0)} pending. "
            f"Response split: {split}."
        )

    if tool_id == "schedule.alarm_pending_summary":
        split = _dict_split(payload.get("by_channel") or {}) or "no pending channels"
        return (
            "Schedule alarm summary: "
            f"{int(payload.get('pending_count') or 0)} pending reminders, "
            f"{int(payload.get('overdue_count') or 0)} overdue. "
            f"Channel split: {split}."
        )

    if tool_id == "schedule.time_off_pending":
        count = int(payload.get("pending_count") or 0)
        return f"{_there_is_are(count)} {count} pending time-off {_plural(count, 'request')} in the current schedule view."

    if tool_id == "schedule.unavailability_blocks":
        count = int(payload.get("block_count") or 0)
        return f"{_there_is_are(count)} {count} upcoming schedule unavailability {_plural(count, 'block')} in the current view."

    if tool_id == "schedule.availability_conflicts":
        count = int(payload.get("conflict_count") or 0)
        split = _dict_split(payload.get("by_type") or {})
        answer = f"{_there_is_are(count)} {count} schedule availability {_plural(count, 'conflict')} in the current view."
        if split:
            answer += " Conflict split: " + split + "."
        return answer

    if tool_id == "schedule.shift_offer_summary":
        count = int(payload.get("offer_count") or 0)
        split = _dict_split(payload.get("by_status") or {})
        answer = f"Shift offer summary: {count} {_plural(count, 'offer')} in the current view."
        if split:
            answer += " Status split: " + split + "."
        if payload.get("restricted_count") is not None:
            answer += f" Restricted offers: {int(payload.get('restricted_count') or 0)}."
        return answer

    if tool_id == "schedule.shift_swap_summary":
        count = int(payload.get("swap_count") or 0)
        split = _dict_split(payload.get("by_status") or {})
        answer = f"Shift swap summary: {count} {_plural(count, 'swap')} in the current view."
        if split:
            answer += " Status split: " + split + "."
        return answer

    return "Schedule summary is available from the approved internal Schedules V2 data."


def _drivers_summary_answer(summary: dict) -> str:
    total = int(summary.get("total_drivers") or 0)
    active = int(summary.get("active_drivers") or 0)
    on_shift = int(summary.get("drivers_on_shift") or 0)
    on_orders = int(summary.get("drivers_on_active_orders") or 0)
    average_score = summary.get("average_score")
    answer = (
        f"{_there_is_are(total)} {total} {_plural(total, 'driver')} in the current view; "
        f"{active} {_plural(active, 'driver')} {_count_verb(active, 'is', 'are')} active."
    )
    answer += f" {on_shift} {_plural(on_shift, 'driver')} {_count_verb(on_shift, 'is', 'are')} on shift"
    answer += f" and {on_orders} {_plural(on_orders, 'driver')} {_count_verb(on_orders, 'is', 'are')} tied to active orders."
    if average_score is not None:
        answer += f" Average current score is {average_score}."
    by_store = summary.get("by_store") or {}
    if by_store:
        answer += " Store split: " + "; ".join(
            f"{store}: {count}" for store, count in sorted(by_store.items())
        ) + "."
    return answer


def _labor_summary_answer(summary: dict) -> str:
    total = int(summary.get("total_employees") or 0)
    active = int(summary.get("active_employees") or 0)
    published = int(summary.get("published_shifts") or 0)
    open_shifts = int(summary.get("open_shifts") or 0)
    hours = float(summary.get("last30_cached_hours") or 0.0)
    answer = (
        f"Labor summary: employee count is all allowed employee store assignments: "
        f"{total} {_plural(total, 'employee')}, {active} active. "
        f"Published-schedule/all-allowed historical scope: {published} assigned "
        f"{_plural(published, 'shift')} and {open_shifts} open {_plural(open_shifts, 'shift')}."
    )
    answer += f" Last-30 cached labor hours from performance-cache rows: {hours:g} hours."
    statuses = summary.get("today_attendance_statuses") or {}
    if statuses:
        answer += " Today's attendance statuses: " + "; ".join(
            f"{status}: {count}" for status, count in sorted(statuses.items())
        ) + "."
    return answer


def _list_count(payload: dict, *keys: str) -> tuple[str | None, int]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return key, len(value)
        if isinstance(value, dict):
            return key, len(value)
    return None, int(payload.get("count") or 0)


def _sample_names(rows: object, *keys: str, limit: int = 3) -> str:
    if not isinstance(rows, list):
        return ""
    names: list[str] = []
    for row in rows[:limit]:
        if isinstance(row, dict):
            for key in keys:
                value = str(row.get(key) or "").strip()
                if value:
                    names.append(value)
                    break
        elif row:
            names.append(str(row))
    return "; ".join(names)


def _operational_readonly_answer(payload: dict, tool_id: str, question: str = "") -> str:
    if payload.get("ok") is False:
        error = str(payload.get("error") or "unavailable").replace("_", " ")
        return f"The approved read-only {tool_id} tool could not return data: {error}."

    if tool_id == "employee.my_profile.read":
        employee = payload.get("employee") if isinstance(payload.get("employee"), dict) else {}
        name = employee.get("full_name") or employee.get("name") or "this employee"
        active = employee.get("active")
        status = "active" if active is True else "inactive" if active is False else "listed"
        return f"Employee profile: {name} is {status} in the approved employee records."
    if tool_id == "employee.my_contact.read":
        contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else {}
        fields = [key for key in ("phone", "email", "secondary_phones") if contact.get(key)]
        return f"Employee contact record is available. Fields present: {', '.join(fields) if fields else 'none'}."
    if tool_id == "employee.my_stores.read":
        stores = payload.get("stores") or []
        return f"Employee store assignments: {_sample_names(stores, 'store_key', 'store') or 'none'}."
    if tool_id == "employee.my_positions.read":
        positions = payload.get("positions") or []
        return f"Employee positions: {_sample_names(positions, 'position', 'name', 'role') or 'none'}."
    if tool_id in {"employee.my_schedule.today", "employee.my_schedule.week", "employee.my_recent_shifts"}:
        _key, count = _list_count(payload, "shifts")
        sample = _sample_names(payload.get("shifts"), "position", "role", "employee_name")
        window = payload.get("date") or payload.get("week_start") or "current view"
        answer = f"Employee schedule: {count} {_plural(count, 'shift')} in {window}."
        if sample:
            answer += f" Sample: {sample}."
        return answer
    if tool_id == "employee.my_open_shifts":
        count = int(payload.get("count") or 0)
        return f"Employee open shifts: {count} available {_plural(count, 'shift')} in assigned stores."
    if tool_id == "employee.my_availability.read":
        key, count = _list_count(payload, "availability")
        return f"Employee availability: {count} {_plural(count, key or 'availability row')} in the approved records."
    if tool_id == "employee.my_time_off.status":
        count = int(payload.get("count") or len(payload.get("requests") or []))
        return f"Employee time-off status: {count} {_plural(count, 'request')} in the current view."
    if tool_id == "employee.my_shift_alarm_settings":
        pending = len(payload.get("pending_alarms") or [])
        return f"Employee shift alarm settings are available, with {pending} pending {_plural(pending, 'alarm')}."
    if tool_id == "employee.my_attendance_summary":
        split = _dict_split(payload.get("by_status") or {})
        return f"Employee attendance summary: {split or 'no attendance status rows in the current view'}."
    if tool_id == "employee.my_day_breakdown":
        shift_count = len(payload.get("shifts") or [])
        attendance_count = len(payload.get("attendance") or [])
        return f"Employee day breakdown: {shift_count} {_plural(shift_count, 'shift')} and {attendance_count} attendance {_plural(attendance_count, 'row')}."

    if tool_id in {"schedules.today_view", "schedules.week_view"}:
        count = int(payload.get("count") or 0)
        store = payload.get("store") or "all"
        window = payload.get("date") or payload.get("week_start") or "current view"
        sample = _sample_names(payload.get("shifts"), "employee_name", "position", "role")
        answer = f"Schedule view for {store} ({window}): {count} {_plural(count, 'shift')}."
        if sample:
            answer += f" Sample: {sample}."
        return answer
    if tool_id == "kitchen.recipe_search":
        count = int(payload.get("count") or 0)
        sample = _sample_names(payload.get("recipes"), "name", "code")
        answer = f"Kitchen recipe search returned {count} {_plural(count, 'recipe')}."
        if sample:
            answer += f" Matches: {sample}."
        return answer
    if tool_id == "kitchen.recipe_lookup":
        recipe = payload.get("recipe") if isinstance(payload.get("recipe"), dict) else {}
        name = recipe.get("name") or recipe.get("code") or "recipe"
        return f"Kitchen recipe lookup: {name}. Prep time: {recipe.get('prep_time') or 'not listed'}."
    if tool_id.startswith("kitchen.prep_"):
        count = int(payload.get("count") or 0)
        store = payload.get("store") or "all"
        day = payload.get("date") or "current day"
        sample = _sample_names(payload.get("entries"), "item", "assignee_name", "status")
        answer = f"Kitchen prep for {store} on {day}: {count} {_plural(count, 'entry')}."
        if sample:
            answer += f" Sample: {sample}."
        return answer
    if tool_id == "vendors.vendor_recent_orders":
        count = int(payload.get("count") or 0)
        vendor = payload.get("vendor") or "all vendors"
        sample = _sample_names(payload.get("orders"), "order_number", "vendor", "status")
        answer = f"Vendor recent orders for {vendor}: {count} {_plural(count, 'order')}."
        if sample:
            answer += f" Sample: {sample}."
        return answer
    if tool_id.startswith("attendance."):
        count = int(payload.get("count") or 0)
        store = payload.get("store") or "all"
        day = payload.get("date") or "current day"
        split = _dict_split(payload.get("by_status") or {})
        answer = f"Attendance summary for {store} on {day}: {count} {_plural(count, 'row')}."
        if split:
            answer += f" Status split: {split}."
        return answer

    return f"The approved read-only {tool_id} tool returned data for the current view."


def _contextual_followup(question: str, previous_question: str) -> bool:
    return _shared_contextual_followup(question, previous_question)


def _resolved_question(question: str, previous_question: str = "") -> str:
    return _shared_resolved_question(question, previous_question)


def _route_required_verifications() -> int:
    raw = (os.getenv("ASSISTANT_TOOL_ROUTE_REQUIRED_VERIFICATIONS") or "").strip()
    try:
        value = int(raw) if raw else _TOOL_ROUTE_REQUIRED_VERIFICATIONS
    except ValueError:
        value = _TOOL_ROUTE_REQUIRED_VERIFICATIONS
    return max(value, 1)


def _route_scope(principal: dict) -> tuple[str, str]:
    role = _role(principal)
    store = str(principal.get("current_store") or "").strip()
    if not store:
        stores = principal.get("store_slugs") or []
        store = str(stores[0]) if stores else ""
    return role, store


def _route_args(tool_id: str, resolved_question: str) -> tuple[str, dict]:
    if tool_id == "toast.table_activity":
        return "latest_table_open", {
            "location": _requested_store(resolved_question) or "all_locations",
            "business_date": _toast_table_business_date_from_question(resolved_question) or "today",
        }
    if tool_id == "toast.sales_summary":
        return "sales_summary", {
            "period": _toast_period_from_question(resolved_question),
        }
    if tool_id == "toast.webhook_activity":
        return "toast_webhook_activity", {
            "store": _requested_store(resolved_question) or "all_accessible",
            "business_date": _toast_table_business_date_from_question(resolved_question) or "today",
        }
    if tool_id == "toast.employee_profiles":
        employee_match = re.search(
            r"\b(?:cena_employee_|employee(?:\s+id)?\s*#?\s*)(\d+)\b",
            resolved_question,
            re.IGNORECASE,
        )
        return "toast_employee_profiles", {
            "employee": employee_match.group(1) if employee_match else "overview_or_name_lookup",
        }
    if tool_id == "orders.store_summary":
        window = _requested_today_window(resolved_question)
        return "order_summary", {
            "store": _requested_store(resolved_question) or "all_accessible",
            "window": window[0] if window else "current_view",
        }
    if tool_id.startswith("orders.catering_"):
        window = _requested_today_window(resolved_question)
        return tool_id.split(".", 1)[1], {
            "tool": tool_id,
            "store": _requested_store(resolved_question) or "all_accessible",
            "window": window[0] if window else "current_view",
        }
    if tool_id.startswith("orders.in_house_"):
        return tool_id.split(".", 1)[1], {
            "tool": tool_id,
            "store": _requested_store(resolved_question) or "all_accessible",
        }
    if tool_id.startswith("schedule."):
        text = str(resolved_question or "").casefold()
        if re.search(r"\btomorrow\b", text):
            window = "tomorrow"
        elif "today" in text:
            window = "today"
        elif re.search(r"\b(this week|week|weekly)\b", text):
            window = "current_week"
        else:
            window = "current_view"
        return tool_id.split(".", 1)[1], {
            "tool": tool_id,
            "store": _requested_store(resolved_question) or "all_accessible",
            "window": window,
        }
    if tool_id == "drivers.store_summary":
        return "driver_summary", {"scope": "current_view"}
    if tool_id == "labor.store_aggregate":
        return "labor_summary", {"scope": "current_view"}
    if tool_id in _READONLY_OPERATIONAL_TOOL_IDS:
        text = str(resolved_question or "").casefold()
        if re.search(r"\btomorrow\b", text):
            window = "tomorrow"
        elif re.search(r"\b(yesterday|last\s+night|previous\s+day)\b", text):
            window = "yesterday"
        elif re.search(r"\bweek\b", text):
            window = "current_week"
        else:
            window = "today_or_current_view"
        return tool_id.replace(".", "_"), {
            "tool": tool_id,
            "store": _requested_store(resolved_question) or "all_accessible",
            "window": window,
        }
    return "unknown", {}


def _tool_payload_for(tool_id: str, tool_data: dict) -> object:
    if not isinstance(tool_data, dict):
        return None
    return tool_data.get(tool_id)


def _tool_answer_verified(tool_id: str, payload: object, answer: str) -> bool:
    if not str(answer or "").strip():
        return False
    if tool_id == "toast.table_activity":
        if not isinstance(payload, dict):
            return "table" in answer.casefold() or "do not see any in-store table opens" in answer.casefold()
        latest = payload.get("latest")
        if not isinstance(latest, dict):
            return "do not see any in-store table opens" in answer.casefold()
        table_name = str(latest.get("table_name") or "").strip()
        opened_at = str(latest.get("opened_at_local") or "").strip()
        opened_by = str(latest.get("opened_by_name") or "").strip()
        server_name = str(latest.get("server_name") or "").strip()
        if table_name and table_name not in answer:
            return False
        if opened_at and opened_at not in answer:
            return False
        if opened_by and opened_by not in answer:
            return False
        if server_name and server_name not in answer:
            return False
        return True
    if tool_id == "toast.sales_summary":
        return isinstance(payload, dict) and "Toast Analytics" in answer
    if tool_id == "toast.webhook_activity":
        return isinstance(payload, dict) and payload.get("data_class") == "toast_webhook_activity_sanitized" and "Toast webhook" in answer
    if tool_id == "toast.employee_profiles":
        return isinstance(payload, dict) and payload.get("data_class") == "toast_employee_profiles_sanitized" and "Toast" in answer and "profile" in answer.casefold()
    if tool_id == "orders.store_summary":
        return isinstance(payload, dict) and any(word in answer for word in ("catering", "order", "tracking"))
    if tool_id.startswith("orders."):
        return (
            isinstance(payload, dict)
            and payload.get("ok") is not False
            and any(word in answer.casefold() for word in ("catering", "order", "quote", "tracking", "driver"))
        )
    if tool_id.startswith("schedule."):
        return (
            isinstance(payload, dict)
            and payload.get("ok") is not False
            and any(
                word in answer.casefold()
                for word in ("schedule", "shift", "time-off", "availability", "alarm", "offer", "swap")
            )
        )
    if tool_id == "drivers.store_summary":
        return isinstance(payload, dict) and "driver" in answer.casefold()
    if tool_id == "labor.store_aggregate":
        return isinstance(payload, dict) and any(word in answer.casefold() for word in ("employee", "labor", "shift"))
    if tool_id in _READONLY_OPERATIONAL_TOOL_IDS:
        if not isinstance(payload, dict) or payload.get("ok") is False:
            return False
        domain = tool_id.split(".", 1)[0]
        domain_words = {
            "employee": ("employee", "schedule", "shift", "attendance", "time-off", "profile"),
            "schedules": ("schedule", "shift"),
            "kitchen": ("kitchen", "recipe", "prep"),
            "vendors": ("vendor", "order"),
            "attendance": ("attendance", "row", "status"),
        }.get(domain, (domain,))
        return any(word in answer.casefold() for word in domain_words)
    return False


def _record_tool_route_verification(
    question: str,
    previous_question: str,
    principal: dict,
    approved: dict,
    tool_data: dict,
    route_path: str = "deterministic",
    route_meta: dict | None = None,
) -> dict | None:
    tool_id = str(approved.get("tool_id") or "")
    if tool_id not in _VERIFIED_ROUTE_TOOL_IDS:
        return None
    answer = str(approved.get("answer") or "")
    payload = _tool_payload_for(tool_id, tool_data)
    if not _tool_answer_verified(tool_id, payload, answer):
        return {
            "status": "not_recorded",
            "tool_id": tool_id,
            "reason": "verification_failed",
        }

    resolved_question = _resolved_question(question, previous_question)
    route_kind, route_args = _route_args(tool_id, resolved_question)
    role_scope, store_scope = _route_scope(principal)
    required = _route_required_verifications()
    now = _now_iso()
    route_key_hash = _stable_hash({
        "role_scope": role_scope,
        "store_scope": store_scope,
        "tool_id": tool_id,
        "route_kind": route_kind,
        "route_args": route_args,
    })
    payload_hash = _stable_hash(payload)
    answer_hash = _stable_hash(answer)
    route_id = _stable_hash({"route_key_hash": route_key_hash})[:32]

    review_receiver._init_db()
    with sqlite3.connect(review_receiver._db_path()) as con:
        existing = con.execute(
            """
            SELECT verification_count, first_seen_at, status
              FROM assistant_verified_tool_route
             WHERE route_key_hash = ?
            """,
            (route_key_hash,),
        ).fetchone()
        count = int(existing[0]) + 1 if existing else 1
        first_seen_at = str(existing[1]) if existing else now
        existing_status = str(existing[2]) if existing else "learning"
        status = existing_status if existing_status in {"verified", "flagged", "rejected"} else "learning"
        con.execute(
            """
            INSERT INTO assistant_verified_tool_route (
                id, route_key_hash, role_scope, store_scope, tool_id,
                route_kind, route_args_redacted, status, verification_count,
                required_verifications, answer_hash, payload_hash,
                first_seen_at, last_verified_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(route_key_hash) DO UPDATE SET
                role_scope = excluded.role_scope,
                store_scope = excluded.store_scope,
                tool_id = excluded.tool_id,
                route_kind = excluded.route_kind,
                route_args_redacted = excluded.route_args_redacted,
                status = excluded.status,
                verification_count = excluded.verification_count,
                required_verifications = excluded.required_verifications,
                answer_hash = excluded.answer_hash,
                payload_hash = excluded.payload_hash,
                last_verified_at = excluded.last_verified_at,
                updated_at = excluded.updated_at
            """,
            (
                route_id,
                route_key_hash,
                role_scope,
                store_scope,
                tool_id,
                route_kind,
                json.dumps(route_args, ensure_ascii=False, sort_keys=True),
                status,
                count,
                required,
                answer_hash,
                payload_hash,
                first_seen_at,
                now,
                now,
            ),
        )
        _record_route_event(
            con,
            route_key_hash,
            tool_id,
            route_kind,
            route_path,
            "candidate",
            route_meta or {},
            now,
        )
        con.commit()

    return {
        "status": status,
        "tool_id": tool_id,
        "route_kind": route_kind,
        "route_path": route_path,
        "verification_count": count,
        "required_verifications": required,
    }


def _record_route_event(
    con: sqlite3.Connection,
    route_key_hash: str,
    tool_id: str,
    route_kind: str,
    route_path: str,
    event_type: str,
    route_meta: dict,
    now: str,
) -> None:
    classifier = route_meta.get("classifier") if isinstance(route_meta, dict) else None
    if not isinstance(classifier, dict):
        classifier = {}
    event_id = uuid.uuid4().hex
    con.execute(
        """
        INSERT OR REPLACE INTO assistant_route_event (
            id, route_key_hash, tool_id, route_kind, route_path, event_type,
            classifier_model, classifier_latency_ms, classifier_token_cost_usd,
            metadata_redacted, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            route_key_hash,
            tool_id,
            route_kind,
            route_path,
            event_type,
            classifier.get("model"),
            classifier.get("latency_ms"),
            classifier.get("token_cost_usd"),
            json.dumps(route_meta, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )


def _auto_verify_tool_routes(min_age_days: int = 7) -> dict:
    review_receiver._init_db()
    now_dt = datetime.now(timezone.utc)
    cutoff = (now_dt - timedelta(days=min_age_days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    now = now_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    promoted: list[str] = []
    with sqlite3.connect(review_receiver._db_path()) as con:
        rows = con.execute(
            """
            SELECT id, route_key_hash, tool_id, route_kind
              FROM assistant_verified_tool_route
             WHERE status = 'learning'
               AND verification_count >= required_verifications
               AND first_seen_at <= ?
            """,
            (cutoff,),
        ).fetchall()
        for route_id, route_key_hash, tool_id, route_kind in rows:
            con.execute(
                """
                UPDATE assistant_verified_tool_route
                   SET status = 'verified',
                       last_verified_at = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (now, now, route_id),
            )
            _record_route_event(
                con,
                str(route_key_hash),
                str(tool_id),
                str(route_kind),
                "nightly_auto_verify",
                "auto_verify",
                {"min_age_days": min_age_days},
                now,
            )
            promoted.append(str(route_id))
        con.commit()
    return {
        "ok": True,
        "promoted": len(promoted),
        "route_ids": promoted,
        "cutoff": cutoff,
    }


def _approved_tool_answer(
    question: str,
    previous_question: str,
    principal: dict,
    tools: list[dict],
    tool_data: dict,
    previous_answer: str = "",
    routed_tool_id: str | None = None,
) -> dict | None:
    resolved_question = _resolved_question(question, previous_question)
    routed_tool_id = str(routed_tool_id or "").strip()
    if (
        (not routed_tool_id or routed_tool_id == "assistant.tool_discovery")
        and _wants_tool_discovery(resolved_question)
    ):
        return {
            "ok": True,
            "answer": _tool_discovery_answer(principal, tools),
            "queued": False,
            "storage": "tool_catalog",
            "tool_id": "assistant.tool_discovery",
            "generated_at": _now_iso(),
        }
    if not _has_partner_tool_access(principal):
        return None
    if (
        (not routed_tool_id or routed_tool_id == "assistant.session_context")
        and _OWNER_IDENTITY_RE.search(str(question or ""))
    ):
        if principal.get("is_owner_operator"):
            identity_answer = (
                "This authenticated session is already marked as an owner-operator "
                "session, so I will use the permissions attached to this login. I "
                "still will not treat chat text alone as proof of identity."
            )
        else:
            identity_answer = (
                "This authenticated session is partner-level, so I will use the "
                "permissions attached to this login. I still will not treat chat "
                "text alone as proof of identity."
            )
        return {
            "ok": True,
            "answer": identity_answer,
            "queued": False,
            "storage": "session_context",
            "tool_id": "assistant.session_context",
            "generated_at": _now_iso(),
        }
    if not routed_tool_id:
        return None
    if routed_tool_id == "toast.sales_summary" and (
        _wants_toast_data_freshness(resolved_question)
        or _has_unsupported_toast_sales_scope(resolved_question)
    ):
        return None
    if routed_tool_id == "toast.employee_profiles" and _toast_employee_profiles_tool_authorized(principal, tools):
        employee_profiles = tool_data.get("toast.employee_profiles") if isinstance(tool_data, dict) else None
        if not _tool_payload_ok(employee_profiles):
            return None
        return {
            "ok": True,
            "answer": _toast_employee_profiles_answer(employee_profiles, question),
            "queued": False,
            "storage": "toast_employee_profiles_tool",
            "tool_id": "toast.employee_profiles",
            "generated_at": employee_profiles.get("generated_at"),
        }
    if routed_tool_id == "toast.webhook_activity" and _toast_webhook_tool_authorized(principal, tools):
        webhook_activity = tool_data.get("toast.webhook_activity") if isinstance(tool_data, dict) else None
        if webhook_activity is None:
            try:
                webhook_activity = _toast_webhook_activity_payload(resolved_question)
            except Exception:  # noqa: BLE001
                log.exception("assistant runtime: failed to build toast.webhook_activity")
                webhook_activity = None
        if not _tool_payload_ok(webhook_activity):
            return None
        return {
            "ok": True,
            "answer": _toast_webhook_activity_answer(webhook_activity, question),
            "queued": False,
            "storage": "toast_webhook_activity_tool",
            "tool_id": "toast.webhook_activity",
            "generated_at": webhook_activity.get("generated_at"),
        }
    if routed_tool_id == "toast.table_activity" and _toast_table_tool_authorized(principal, tools):
        contextual_table_answer = _toast_table_person_followup_answer(question, previous_answer)
        if contextual_table_answer:
            return {
                "ok": True,
                "answer": contextual_table_answer,
                "queued": False,
                "storage": "toast_table_activity_context",
                "tool_id": "toast.table_activity",
                "generated_at": _now_iso(),
            }
        requested_store = _requested_store(resolved_question)
        business_date = _toast_table_business_date_from_question(resolved_question)
        table_activity = tool_data.get("toast.table_activity") if isinstance(tool_data, dict) else None
        requested_business_date = business_date or _today_ct().strftime("%Y%m%d")
        payload_business_date = (
            str(table_activity.get("business_date") or "").strip()
            if isinstance(table_activity, dict)
            else ""
        )
        if (
            not isinstance(table_activity, dict)
            or (payload_business_date and payload_business_date != requested_business_date)
            or (not payload_business_date and business_date)
            or _toast_table_activity_needs_employee_refresh(table_activity, question)
        ):
            return None
        return {
            "ok": True,
            "answer": _toast_table_activity_answer_for_question(table_activity, question),
            "queued": False,
            "storage": "toast_table_activity_tool",
            "tool_id": "toast.table_activity",
            "generated_at": table_activity.get("generated_at"),
        }
    if routed_tool_id == "toast.sales_summary" and _toast_tool_authorized(principal, tools):
        toast_summary = tool_data.get("toast.sales_summary") if isinstance(tool_data, dict) else None
        if not isinstance(toast_summary, dict):
            return None
        return {
            "ok": True,
            "answer": _toast_sales_summary_answer(toast_summary),
            "queued": False,
            "storage": "toast_analytics_tool",
            "tool_id": "toast.sales_summary",
            "generated_at": toast_summary.get("generated_at"),
        }
    if routed_tool_id in _READONLY_OPERATIONAL_TOOL_IDS and _tool_available(tools, routed_tool_id):
        operational_payload = tool_data.get(routed_tool_id) if isinstance(tool_data, dict) else None
        if isinstance(operational_payload, dict):
            return {
                "ok": True,
                "answer": _operational_readonly_answer(operational_payload, routed_tool_id, resolved_question),
                "queued": False,
                "storage": "operational_tool",
                "tool_id": routed_tool_id,
                "generated_at": operational_payload.get("generated_at"),
            }
    if routed_tool_id.startswith("schedule.") and _tool_available(tools, routed_tool_id):
        schedule_payload = tool_data.get(routed_tool_id) if isinstance(tool_data, dict) else None
        if isinstance(schedule_payload, dict):
            return {
                "ok": True,
                "answer": _schedule_read_answer(schedule_payload, routed_tool_id, resolved_question),
                "queued": False,
                "storage": "operational_tool",
                "tool_id": routed_tool_id,
                "generated_at": schedule_payload.get("generated_at"),
            }
    if routed_tool_id == "drivers.store_summary" and _tool_available(tools, "drivers.store_summary"):
        driver_summary = tool_data.get("drivers.store_summary") if isinstance(tool_data, dict) else None
        if isinstance(driver_summary, dict):
            return {
                "ok": True,
                "answer": _drivers_summary_answer(driver_summary),
                "queued": False,
                "storage": "operational_tool",
                "tool_id": "drivers.store_summary",
                "generated_at": driver_summary.get("generated_at"),
            }
    if routed_tool_id == "labor.store_aggregate" and _tool_available(tools, "labor.store_aggregate"):
        labor_summary = tool_data.get("labor.store_aggregate") if isinstance(tool_data, dict) else None
        if isinstance(labor_summary, dict):
            return {
                "ok": True,
                "answer": _labor_summary_answer(labor_summary),
                "queued": False,
                "storage": "operational_tool",
                "tool_id": "labor.store_aggregate",
                "generated_at": labor_summary.get("generated_at"),
            }
    if (
        routed_tool_id.startswith("orders.")
        and routed_tool_id != "orders.store_summary"
        and _tool_available(tools, routed_tool_id)
    ):
        orders_payload = tool_data.get(routed_tool_id) if isinstance(tool_data, dict) else None
        if isinstance(orders_payload, dict):
            return {
                "ok": True,
                "answer": _orders_read_answer(orders_payload, routed_tool_id, resolved_question),
                "queued": False,
                "storage": "operational_tool",
                "tool_id": routed_tool_id,
                "generated_at": orders_payload.get("generated_at"),
            }
    if routed_tool_id == "orders.store_summary" and _tool_available(tools, "orders.store_summary"):
        summary = tool_data.get("orders.store_summary") if isinstance(tool_data, dict) else None
        if isinstance(summary, dict):
            return {
                "ok": True,
                "answer": _orders_summary_answer(summary, resolved_question),
                "queued": False,
                "storage": "operational_tool",
                "tool_id": "orders.store_summary",
                "generated_at": summary.get("generated_at"),
            }
    return None


def _should_queue(question: str, principal: dict) -> tuple[bool, str, str | None]:
    if str(principal.get("kind") or "") == "anonymous":
        return True, "not_authenticated", "ai.ask_claude_personal"
    if not _can_ask_personal(principal):
        return True, "missing_ai_permission", "ai.ask_claude_personal"
    forced_review_reason = _shared_force_review_reason(question)
    if forced_review_reason:
        return True, forced_review_reason, "ai.ask_claude"
    if _SENSITIVE_RE.search(question):
        needed = "ai.ask_claude"
        if not _can_ask_operational(principal):
            return True, "sensitive_or_operational_question_requires_higher_permission", needed
        return True, "sensitive_or_operational_question_needs_approved_tool", needed
    if _DATA_TOOL_RE.search(question):
        needed = "ai.ask_claude"
        if not _can_ask_operational(principal):
            return True, "data_question_requires_higher_permission", needed
        return True, "data_question_needs_approved_tool", needed
    return False, "", None


def _queue_for_review(question: str, principal: dict, reason: str,
                      required_permission: str | None, source: str) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "created_at": _now_iso(),
        "status": _REVIEW_STATUS,
        "risk_level": _review_risk_level(reason),
        "question": question,
        "reason": reason,
        "required_permission": required_permission,
        "role": _role(principal),
        "store_key": principal.get("current_store") or ((principal.get("store_slugs") or [None])[0]),
        "model_key": "ck_runtime_review_queue",
        "tool_name": required_permission or "assistant.general_help",
        "delivery_target": "ck_assistant_review",
        "origin": source or "ck_runtime",
        "principal": principal,
    }
    qid = review_receiver._save_question(row)
    row["ck_question_id"] = qid
    return row


def _gemini_generate(prompt: str) -> tuple[str | None, str | None]:
    key = _read_secret("GEMINI_API_KEY")
    if not key:
        return None, None
    try:
        from google import genai  # type: ignore[import]
    except ImportError:
        log.warning("assistant runtime: google-genai package not installed")
        return None, None

    model = os.getenv("AI_ASSISTANT_GEMINI_MODEL", _DEFAULT_GEMINI_MODEL)
    client = genai.Client(api_key=key, http_options={"timeout": _provider_timeout_ms()})
    resp = client.models.generate_content(model=model, contents=prompt)
    text = (getattr(resp, "text", None) or "").strip()
    return text or None, model


def _review_notice_prompt(principal: dict, reason: str, required_permission: str | None,
                          fallback: str) -> str:
    return (
        _stable_policy_prompt()
        + "\n\n"
        + "A user question has already been durably saved in the CK assistant "
        "review queue. Draft only the short message shown to the user. Do not "
        "answer the saved question. Do not invent facts, mention Gemini, mention "
        "API keys, expose internal reason codes, or imply that Sam received a "
        "separate live alert. Say that it was saved for Sam review. Keep it to "
        "one or two friendly sentences.\n\n"
        f"{_session_prompt(principal)}\n"
        f"Review reason: {_review_reason_label(reason)}.\n"
        f"Required permission: {required_permission or 'none'}.\n"
        f"Fallback notice: {fallback}"
    )


def _gemini_review_notice(principal: dict, reason: str, required_permission: str | None,
                          fallback: str) -> tuple[str | None, str | None]:
    return _gemini_generate(_review_notice_prompt(principal, reason, required_permission, fallback))


def _assistant_business_context_prompt() -> str:
    try:
        return _load_assistant_context().strip()
    except Exception:  # noqa: BLE001
        log.exception("assistant runtime: failed to load assistant business context")
        return ""


def _system_prompt(principal: dict) -> str:
    parts = [_stable_policy_prompt()]
    business_context = _assistant_business_context_prompt()
    if business_context:
        parts.append("Cenas Kitchen business context:\n" + business_context)
    parts.append(_session_prompt(principal))
    return "\n\n".join(parts)


def _stable_policy_prompt() -> str:
    return (
        "You are the Cenas Kitchen in-app assistant running on CK. Answer only "
        "within the current user's role and permissions. You do not reveal "
        "secrets, passcodes, tokens, customer PII, unauthorized payroll, raw "
        "peer pay, sales internals, GUIDs, or cross-store data. This first "
        "version answers operational data questions only from approved, "
        "sanitized read-only tool payloads. If a question needs a tool that is "
        "not available, say it needs Sam review and do not guess. If "
        "owner_operator=true in the authenticated session, use that session "
        "context for permission decisions; do not ask the user to prove they "
        "are Sam in chat."
    )


def _session_prompt(principal: dict) -> str:
    return (
        f"Current session: role={_role(principal)}, kind={principal.get('kind')}, "
        f"stores={principal.get('store_slugs')}, path={principal.get('path')}, "
        f"owner_operator={bool(principal.get('is_owner_operator'))}."
    )


def _gemini_answer(question: str, principal: dict) -> tuple[str | None, str | None]:
    prompt = _system_prompt(principal) + "\n\nUser question:\n" + question
    return _gemini_generate(prompt)


# --- C.E.N.A. Level 3: when no deterministic tool matched, investigate the data --
_DATA_QUESTION_RE = re.compile(
    r"\b(sales|net|gross|revenue|orders?|catering|labor|hours?|overtime|\bot\b|"
    r"avg|average|check|covers?|drivers?|deliver\w*|items?|sold|menu|store|"
    r"copperfield|tomball|week|weekly|day|daypart|month|april|may|march|june|"
    r"anomal\w*|trend|compare|comparison|why|how many|how much|busiest|slowest|"
    r"top|best|worst|highest|lowest|splh|prime cost|spend)\b",
    re.IGNORECASE,
)


def _looks_like_data_question(question: str) -> bool:
    return bool(_DATA_QUESTION_RE.search(question or ""))


def _investigation_answer(question: str, principal: dict) -> dict | None:
    """Run the L3 reasoning engine for a data question that matched no tool.
    Defensive: any failure (engine/ snapshots/ model unavailable) returns None so
    the caller falls through to the existing conversational path - the live
    runtime must never regress because L3 is mid-deploy."""
    if not _looks_like_data_question(question):
        return None
    try:
        from app.services.cena_sql_orchestrator import answer_question
    except Exception:  # noqa: BLE001
        return None
    try:
        res = answer_question(question, principal)
    except Exception:  # noqa: BLE001
        log.exception("assistant runtime: L3 investigation failed")
        return None
    if not isinstance(res, dict) or not res.get("ok") or not str(res.get("answer", "")).strip():
        return None
    return res


# reasons from _should_queue that mean "authorized user, but no approved tool" -
# exactly the turns C.E.N.A. Level 3 should INVESTIGATE instead of saving for review.
_L3_INVESTIGABLE_REASONS = frozenset({
    "data_question_needs_approved_tool",
    "sensitive_or_operational_question_needs_approved_tool",
})


def _investigation_response(investigation: dict) -> tuple[dict, int]:
    return {
        "ok": True,
        "answer": investigation["answer"],
        "queued": False,
        "confidence": investigation.get("confidence"),
        "confidence_reason": investigation.get("confidence_reason"),
        "show_work": investigation.get("show_work"),
        "trace": investigation.get("trace"),
        "storage": "ck_runtime",
        "route_path": "investigation",
        "routed_tool_id": None,
    }, 200


def _answer(payload: dict) -> tuple[dict, int]:
    question = str(payload.get("question") or "").strip()[:_MAX_QUESTION_CHARS]
    previous_question = str(payload.get("previous_question") or "").strip()[:_MAX_QUESTION_CHARS]
    previous_answer = str(payload.get("previous_answer") or "").strip()[:_MAX_QUESTION_CHARS]
    principal = payload.get("principal") or {}
    tools = payload.get("tools") or []
    tool_data = payload.get("tool_data") or {}
    routed_tool_id = str(payload.get("routed_tool_id") or "").strip() or None
    route_path = str(payload.get("route_path") or "review").strip() or "review"
    route_meta = payload.get("route_meta") if isinstance(payload.get("route_meta"), dict) else {}
    source = str(payload.get("source") or "cenas_app")
    if not question:
        return {"ok": False, "error": "question required"}, 400

    forced_review_reason = _shared_force_review_reason(question)
    if forced_review_reason:
        routed_tool_id = None
        tool_data = {}
        route_path = "review"
        route_meta = {
            **route_meta,
            "tool_id": None,
            "route_path": "review",
            "reason": forced_review_reason,
            "forced_review": True,
        }

    resolved_question = _resolved_question(question, previous_question)
    if forced_review_reason:
        row = _queue_for_review(question, principal, forced_review_reason, "ai.ask_claude", source)
        answer = _queued_answer(forced_review_reason)
        response = {
            "ok": True,
            "answer": answer,
            "queued": True,
            "queue_id": row["id"],
            "storage": "ck",
            "ck_question_id": row["ck_question_id"],
            "reason": forced_review_reason,
            "route_path": "review",
            "routed_tool_id": None,
            "route_meta": route_meta,
        }
        return response, 200

    approved = _approved_tool_answer(
        question,
        previous_question,
        principal,
        tools,
        tool_data,
        previous_answer,
        routed_tool_id,
    )
    if approved is not None:
        route_cache = _record_tool_route_verification(
            question,
            previous_question,
            principal,
            approved,
            tool_data,
            route_path,
            route_meta,
        )
        if route_cache is not None:
            approved["route_cache"] = route_cache
        approved.setdefault("route_path", route_path)
        approved.setdefault("routed_tool_id", routed_tool_id)
        approved.setdefault("route_meta", route_meta)
        return approved, 200

    should_queue, reason, required = _should_queue(resolved_question, principal)
    if should_queue:
        # L3: an AUTHORIZED user asking a data/operational question with no approved
        # tool -> investigate the data instead of saving it for Sam review. Access
        # reasons (not_authenticated / missing permission / requires_higher_permission)
        # are NOT in this set, so unauthorized users still queue.
        if reason in _L3_INVESTIGABLE_REASONS:
            investigation = _investigation_answer(resolved_question, principal)
            if investigation is not None:
                return _investigation_response(investigation)
        row = _queue_for_review(question, principal, reason, required, source)
        answer = _queued_answer(reason)
        response = {
            "ok": True,
            "answer": answer,
            "queued": True,
            "queue_id": row["id"],
            "storage": "ck",
            "ck_question_id": row["ck_question_id"],
            "reason": reason,
            "route_path": "review",
            "routed_tool_id": None,
        }
        return response, 200

    # Non-queued data/operations question -> still investigate before a plain reply.
    investigation = _investigation_answer(resolved_question, principal)
    if investigation is not None:
        return _investigation_response(investigation)

    answer = None
    model = None
    try:
        answer, model = _gemini_answer(question, principal)
    except Exception:  # noqa: BLE001
        log.exception("assistant runtime gemini answer failed")
        answer = None
        model = None

    if not answer:
        row = _queue_for_review(question, principal, "model_unavailable_or_no_answer", None, source)
        return {
            "ok": True,
            "answer": "I saved that for Sam review. The assistant model is not available right now.",
            "queued": True,
            "queue_id": row["id"],
            "storage": "ck",
            "ck_question_id": row["ck_question_id"],
            "reason": "model_unavailable_or_no_answer",
            "route_path": "review",
            "routed_tool_id": None,
        }, 200

    return {
        "ok": True,
        "answer": answer,
        "queued": False,
        "model": model,
        "storage": "ck_runtime",
        "route_path": "general",
        "routed_tool_id": None,
    }, 200


class Handler(BaseHTTPRequestHandler):
    server_version = "CenasAssistantRuntime/1.0"

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        expected = (os.getenv("ASSISTANT_RUNTIME_TOKEN") or os.getenv("ASSISTANT_REVIEW_TOKEN") or "").strip()
        if not expected:
            return False
        auth = self.headers.get("Authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        token = token or self.headers.get("X-Ai-Assistant-Token", "").strip()
        return token == expected

    def _drain_bounded_request_body(self) -> None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return
        try:
            length = int(raw_length)
        except ValueError:
            return
        if 0 < length <= _MAX_REQUEST_BODY_BYTES:
            self.rfile.read(length)

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/healthz":
            self._json(404, {"ok": False, "error": "not_found"})
            return
        self._json(200, {
            "ok": True,
            "service": "cenas_assistant_runtime",
            "db": str(review_receiver._db_path()),
            "row_counts": review_receiver._row_counts(),
            "providers": {
                "gemini": bool(_read_secret("GEMINI_API_KEY")),
            },
            "active_model_provider": "gemini",
            "active_model": os.getenv("AI_ASSISTANT_GEMINI_MODEL", _DEFAULT_GEMINI_MODEL),
        })

    def do_POST(self) -> None:
        if urlparse(self.path).path != ANSWER_PATH:
            self._json(404, {"ok": False, "error": "not_found"})
            return
        if not self._authorized():
            self._drain_bounded_request_body()
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > _MAX_REQUEST_BODY_BYTES:
                self._json(400, {"ok": False, "error": "bad_length"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            body, status = _answer(payload)
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": str(exc)})
            return
        self._json(status, body)

    def log_message(self, fmt, *args) -> None:
        return


def main() -> None:
    review_receiver._init_db()
    raw_hosts = os.getenv("ASSISTANT_RUNTIME_HOSTS") or os.getenv("ASSISTANT_RUNTIME_HOST") or "127.0.0.1"
    hosts = [host.strip() for host in raw_hosts.split(",") if host.strip()]
    port = int(os.getenv("ASSISTANT_RUNTIME_PORT") or "8782")
    servers = [ThreadingHTTPServer((host, port), Handler) for host in hosts]
    for httpd, host in zip(servers, hosts, strict=True):
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        print(f"assistant runtime listening on http://{host}:{port}")
    print(f"db: {review_receiver._db_path()}")
    threading.Event().wait()


if __name__ == "__main__":
    main()
