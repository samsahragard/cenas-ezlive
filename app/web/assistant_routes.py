"""Permission-scoped in-app assistant.

This is the manager/employee/driver-facing assistant surface, distinct from
Sam's operator-only /sam/chat. It deliberately starts read-only and
queue-first: if a question needs a data tool that is not approved for the
current role/session, it is saved for Sam review instead of guessed.

Durable review and model-runtime ownership: CK/Mini_IT13. In production the
web app should proxy assistant turns to the CK-local runtime; Render direct
model calls are blocked unless explicitly overridden for an emergency.
"""
from __future__ import annotations

import json
import logging
import os
import re
import hashlib
import threading
import urllib.parse
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, g, jsonify, request, session

from app.db import SessionLocal
from app.models import (
    AttendanceShift,
    Driver,
    DriverShift,
    Employee,
    EmployeeStoreAssignment,
    Order,
    PerfPeriodCache,
    Schedule,
    Shift,
)
from app.web.permissions import accessible_store_slugs
from app.services.assistant_tool_inventory import iter_partner_tool_definitions
from app.services.permissions import ROLE_PERMISSIONS, has_permission

log = logging.getLogger(__name__)

assistant_bp = Blueprint("assistant", __name__)

_QUEUE_LOCK = threading.Lock()
_MAX_QUESTION_CHARS = 2000
_DEFAULT_MODEL = "claude-sonnet-4-6"
_REVIEW_STATUS = "needs_review"
_CK_REVIEW_PATH = "/review/question"
_CK_RUNTIME_PATH = "/assistant/answer"
_SENSITIVE_RE = re.compile(
    r"\b("
    r"password|passcode|token|secret|api key|credential|pin|"
    r"phone|email|address|customer|"
    r"wage|payroll|pay rate|hourly rate|peer pay|"
    r"sales|revenue|eligible_sales|cashsales|noncashsales|guid|"
    r"all employees|all drivers|all stores"
    r")\b",
    re.IGNORECASE,
)
_DATA_TOOL_RE = re.compile(
    r"\b("
    r"how many|how amny|count|total|report|summary|list|show me|who|which|"
    r"order|orders|driver|drivers|employee|employees|staff|team|"
    r"schedule|shift|roster|attendance|incident|write up|"
    r"tip|tips|labor|staffing|inventory|vendor|customer|ezcater|catering|caterings|"
    r"late|tracking|tracking link|tracking links|delivery|deliveries|toast|"
    r"table|tables|talbe|floor|opened|open|pay|bonus|fee|fees|"
    r"tool|tools|file|files|filesystem|shell|sql|render|deploy|git|env|"
    r"log|logs|restart|dev chat|sam chat|permission|permissions"
    r")\b",
    re.IGNORECASE,
)
_TOAST_SALES_RE = re.compile(
    r"\b("
    r"toast|sales|revenue|net sales|gross sales|"
    r"average order|avg order|labor percent|labor ratio|sales per labor"
    r")\b",
    re.IGNORECASE,
)
_TOAST_TABLE_ACTIVITY_RE = re.compile(
    r"\b("
    r"table|tables|talbe|floor|seated|seat|opened|open check|"
    r"most recent.*open|latest.*open"
    r")\b",
    re.IGNORECASE,
)
_OPERATIONAL_NOUN_RE = re.compile(
    r"\b("
    r"catering|caterings|order|orders|delivery|deliveries|"
    r"driver|drivers|labor|employee|employees|staff|team|"
    r"table|tables|talbe|floor"
    r")\b",
    re.IGNORECASE,
)
_FOLLOWUP_RE = re.compile(
    r"\b("
    r"what about|how about|what baout|earlier|morning|afternoon|"
    r"evening|tonight|today|tomorrow|yesterday|this week|"
    r"tomball|dos|dos mas|copperfield|uno|uno mas"
    r")\b",
    re.IGNORECASE,
)
_SECRET_TEXT_RE = re.compile(
    r"(?i)\b("
    r"sk-[A-Za-z0-9_-]{12,}|"
    r"[A-Za-z0-9_./+-]{24,}\.[A-Za-z0-9_./+-]{12,}\.[A-Za-z0-9_./+-]{12,}|"
    r"(?:token|secret|api key|password|passcode|pin)\s*[:=]\s*\S+"
    r")"
)


_TOOL_REGISTRY: list[dict[str, Any]] = [
    {
        "tool_id": "assistant.general_help",
        "label": "General app help",
        "description": "Answer general navigation, workflow, and policy-style questions without private operational data.",
        "required_permissions": ["ai.ask_claude_personal"],
        "session_types": ["partner", "staff", "employee", "driver"],
        "store_scope": "none",
        "data_class": "general",
        "read_write_class": "read_only",
        "status": "active",
    },
    {
        "tool_id": "employee.my_profile",
        "label": "Own employee profile",
        "description": "Future read-only personal profile answers scoped to the current employee.",
        "required_permissions": ["ai.ask_claude_personal"],
        "session_types": ["employee", "staff"],
        "store_scope": "own_profile",
        "data_class": "personal_profile",
        "read_write_class": "read_only",
        "status": "review_gated",
    },
    {
        "tool_id": "orders.store_summary",
        "label": "Store order summary",
        "description": "Future store-scoped order and catering summaries from approved marts.",
        "required_permissions": ["ai.ask_claude", "orders.view"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "operations",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
    },
    {
        "tool_id": "drivers.store_summary",
        "label": "Driver summary",
        "description": "Future driver performance and delivery summaries from CK driver marts.",
        "required_permissions": ["ai.ask_claude", "drivers.view_roster"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "driver_operations",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
    },
    {
        "tool_id": "labor.store_aggregate",
        "label": "Labor aggregate",
        "description": "Future aggregate-only labor answers from approved employee marts.",
        "required_permissions": ["ai.ask_claude", "labor.view_store_summary"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "labor_aggregate",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
    },
    {
        "tool_id": "toast.sales_summary",
        "label": "Toast sales summary",
        "description": "Read-only Toast Analytics sales, order, labor, and menu aggregates for an approved period.",
        "required_permissions": ["ai.ask_claude", "sales.view_today"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "sales_aggregate",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
    },
    {
        "tool_id": "toast.table_activity",
        "label": "Toast table activity",
        "description": "Read-only latest in-store Toast table open activity with sanitized table labels and timestamps.",
        "required_permissions": ["ai.ask_claude", "sales.view_today"],
        "session_types": ["partner", "staff"],
        "store_scope": "current_user_store_scope",
        "data_class": "table_activity",
        "read_write_class": "read_only",
        "status": "review_gated",
        "operator_enabled": True,
    },
]


_KNOWN_TOOL_IDS = {tool["tool_id"] for tool in _TOOL_REGISTRY}
for _partner_tool in iter_partner_tool_definitions():
    if _partner_tool["tool_id"] in _KNOWN_TOOL_IDS:
        continue
    _TOOL_REGISTRY.append(_partner_tool)
    _KNOWN_TOOL_IDS.add(_partner_tool["tool_id"])
del _partner_tool


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_text(value: str) -> str:
    return _SECRET_TEXT_RE.sub("[REDACTED]", value)


def _env_truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _operator_user_ids() -> set[int]:
    raw = ",".join(
        value
        for value in (
            os.getenv("AI_ASSISTANT_OPERATOR_USER_IDS"),
            os.getenv("SAM_CHAT_USER_IDS"),
            os.getenv("SAM_CHAT_USER_ID"),
            os.getenv("MASOOD_CHAT_USER_ID"),
        )
        if value
    )
    ids: set[int] = set()
    for part in _split_csv(raw):
        try:
            ids.add(int(part))
        except ValueError:
            log.warning("assistant: ignoring non-integer operator user id %r", part)
    return ids


def _normalized_person_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def _operator_names() -> set[str]:
    configured = os.getenv("AI_ASSISTANT_OPERATOR_NAMES")
    names = _split_csv(configured) if configured else [
        "sam",
        "sam sahragard",
        "masood",
        "masood sahragard",
        "masood ck",
        "masood c",
    ]
    return {_normalized_person_name(name) for name in names if _normalized_person_name(name)}


def _is_owner_operator_user(user: Any | None) -> bool:
    if user is None:
        return False
    role = getattr(user, "permission_level", None)
    if role != "partner":
        return False
    user_id = getattr(user, "id", None)
    if user_id in _operator_user_ids():
        return True
    return _normalized_person_name(getattr(user, "full_name", None)) in _operator_names()


def _assistant_enabled() -> bool:
    if os.getenv("RENDER") and not _env_truthy("AI_ASSISTANT_ENABLED"):
        return False
    return not _env_truthy("AI_ASSISTANT_DISABLED")


def _review_risk_level(reason: str | None) -> str:
    reason_text = (reason or "").casefold()
    if any(term in reason_text for term in ("sensitive", "operational", "data", "permission")):
        return "blocked"
    return "normal"


def _queue_path() -> Path:
    raw = (
        os.getenv("AI_ASSISTANT_PENDING_PATH")
        or os.getenv("ASSISTANT_PENDING_QUEUE_PATH")
        or "/var/data/assistant_review_retry_outbox.jsonl"
    )
    return Path(raw)


def _env_first(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _review_timeout_seconds() -> float:
    raw = _env_first("ASSISTANT_REVIEW_TIMEOUT_SECONDS", "AI_ASSISTANT_CK_REVIEW_TIMEOUT_SECONDS")
    if not raw:
        return 8.0
    try:
        value = float(raw)
    except ValueError:
        return 8.0
    return max(1.0, min(value, 30.0))


def _ck_review_url(raw_url: str) -> str:
    return _normalize_service_url(raw_url, _CK_REVIEW_PATH)


def _ck_runtime_url(raw_url: str) -> str:
    return _normalize_service_url(raw_url, _CK_RUNTIME_PATH)


def _normalize_service_url(raw_url: str, default_path: str) -> str:
    url = raw_url.strip()
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme and parts.netloc and parts.path.rstrip("/") in {"", "/"}:
        return urllib.parse.urlunsplit((
            parts.scheme,
            parts.netloc,
            default_path,
            parts.query,
            parts.fragment,
        ))
    return url


def _runtime_configured() -> bool:
    url = _ck_runtime_url(_env_first("AI_ASSISTANT_CK_RUNTIME_URL", "ASSISTANT_RUNTIME_URL"))
    token = _env_first("AI_ASSISTANT_CK_RUNTIME_TOKEN", "ASSISTANT_RUNTIME_TOKEN")
    return bool(url and token)


def _assistant_available_for_context(ctx: dict[str, Any]) -> bool:
    if not _assistant_enabled() or not ctx.get("can_ask_personal"):
        return False
    if os.getenv("RENDER") and not _runtime_configured() and not _env_truthy("AI_ASSISTANT_ALLOW_RENDER_MODELS"):
        return False
    return True


def _extract_token() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Ai-Assistant-Token") or request.args.get("token")


def _role_permissions(role: str | None) -> set[str]:
    if not role:
        return set()
    perms = ROLE_PERMISSIONS.get(role)
    if perms == {"*"}:
        return {"*"}
    return set(perms or ())


def _store_scope_key(raw_store: Any) -> str | None:
    if raw_store is None:
        return None
    if isinstance(raw_store, str):
        return raw_store
    for attr in ("slug", "store_key", "key", "id"):
        value = getattr(raw_store, attr, None)
        if value:
            return str(value)
    return str(raw_store)


def _has_all_permissions(ctx: dict[str, Any], permissions: list[str]) -> bool:
    granted = set(ctx.get("permissions") or [])
    return "*" in granted or all(perm in granted for perm in permissions)


def _has_partner_tool_access(ctx: dict[str, Any]) -> bool:
    return bool(ctx.get("is_owner_operator") or ctx.get("role") == "partner")


def _tool_catalog_for(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    session_type = ctx.get("kind")
    catalog: list[dict[str, Any]] = []
    for tool in _TOOL_REGISTRY:
        allowed_session = session_type in set(tool["session_types"])
        allowed_permissions = _has_all_permissions(ctx, tool["required_permissions"])
        status = tool["status"]
        operator_active = bool(
            _has_partner_tool_access(ctx)
            and tool.get("operator_enabled")
            and allowed_session
            and allowed_permissions
        )
        partner_catalog_active = bool(
            _has_partner_tool_access(ctx)
            and tool.get("partner_catalog_enabled")
            and allowed_session
            and allowed_permissions
        )
        promoted_active = operator_active or partner_catalog_active
        available = bool((status == "active" or promoted_active) and allowed_session and allowed_permissions)
        effective_status = "active" if promoted_active else status
        reason = None
        if not allowed_session:
            reason = "session_type_not_allowed"
        elif not allowed_permissions:
            reason = "missing_permission"
        elif status != "active" and not promoted_active:
            reason = "needs_sam_review"
        catalog.append({
            **tool,
            "status": effective_status,
            "available": available,
            "deny_reason": reason,
        })
    return catalog


def _principal_context() -> dict[str, Any]:
    user = getattr(g, "current_user", None)
    if session.get("driver_id"):
        role = "driver"
        kind = "driver"
        display_name = session.get("driver_name") or "Driver"
        principal_id = session.get("driver_id")
        stores: list[str] = []
    elif session.get("employee_id") and not user:
        role = "employee"
        kind = "employee"
        display_name = session.get("employee_name") or "Employee"
        principal_id = session.get("employee_id")
        stores = []
    elif user is not None:
        role = getattr(user, "permission_level", None) or "unknown"
        kind = "partner" if session.get("partner_auth_ok") else "staff"
        display_name = getattr(user, "full_name", None) or "User"
        principal_id = getattr(user, "id", None)
        stores = accessible_store_slugs(user)
        is_owner_operator = _is_owner_operator_user(user)
    else:
        role = "anonymous"
        kind = "anonymous"
        display_name = "Anonymous"
        principal_id = None
        stores = []
        is_owner_operator = False

    if session.get("driver_id") or (session.get("employee_id") and not user):
        is_owner_operator = False

    return {
        "kind": kind,
        "role": role,
        "principal_id": principal_id,
        "display_name": display_name,
        "store_slugs": stores,
        "current_store": _store_scope_key(getattr(g, "current_store", None)),
        "path": request.headers.get("X-Current-Path") or request.referrer or request.path,
        "permissions": sorted(_role_permissions(role)),
        "is_owner_operator": bool(is_owner_operator),
        "can_ask_personal": bool(
            role == "partner"
            or has_permission("ai.ask_claude_personal")
            or has_permission("ai.ask_claude")
            or role in {"driver", "employee"}
        ),
        "can_ask_operational": bool(role == "partner" or has_permission("ai.ask_claude")),
    }


def _append_pending_question(row: dict[str, Any]) -> None:
    row = _outbox_record(row)
    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _QUEUE_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _outbox_record(row: dict[str, Any]) -> dict[str, Any]:
    principal = row.get("principal") or {}
    question = _redact_text(str(row.get("question") or ""))
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "status": row.get("status") or _REVIEW_STATUS,
        "question_hash": _stable_hash({"question": question}),
        "question_summary_redacted": question[:500],
        "reason": row.get("reason"),
        "required_permission": row.get("required_permission"),
        "risk_level": row.get("risk_level") or _review_risk_level(row.get("reason")),
        "principal_hash": _stable_hash({
            "kind": principal.get("kind"),
            "role": principal.get("role"),
            "principal_id": principal.get("principal_id"),
            "display_name": principal.get("display_name"),
        }),
        "scope_role": principal.get("role"),
        "scope_store_key": (
            principal.get("current_store")
            or ((principal.get("store_slugs") or [None])[0])
        ),
        "source_path": principal.get("path"),
        "storage": "render_retry_outbox_redacted",
        "ck_target": "assistant_review.sqlite",
    }


def _post_to_ck_review(row: dict[str, Any]) -> tuple[bool, str | None]:
    """Review-only durable persistence path for blocked questions.

    CK owns the review DB. Configure AI_ASSISTANT_CK_REVIEW_URL to a CK-local
    endpoint, for example a Tailscale URL on Mini_IT13. Token can be supplied
    with AI_ASSISTANT_CK_REVIEW_TOKEN. Production answer execution should use
    the CK runtime path instead; this receiver path remains for local review
    ingestion and compatibility tests.
    """
    url = _ck_review_url(_env_first("AI_ASSISTANT_CK_REVIEW_URL", "ASSISTANT_REVIEW_RECEIVER_URL"))
    if not url:
        return False, None
    headers = {"Content-Type": "application/json"}
    token = _env_first("AI_ASSISTANT_CK_REVIEW_TOKEN", "ASSISTANT_REVIEW_RECEIVER_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Ai-Assistant-Token"] = token
    try:
        import httpx

        proxy = (os.getenv("CENA_PROXY") or "").strip() or None
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(_review_timeout_seconds(), connect=min(3.0, _review_timeout_seconds())),
        }
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as hx:
            resp = hx.post(url, json=row, headers=headers)
        data = resp.json() if resp.content else {}
        if 200 <= resp.status_code < 300 and data.get("ok", True):
            ck_id = data.get("ck_question_id") or data.get("question_id") or data.get("id")
            return True, str(ck_id) if ck_id is not None else None
    except Exception:  # noqa: BLE001
        log.exception("assistant: CK review save failed")
    return False, None


def _runtime_principal(ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": ctx["kind"],
        "role": ctx["role"],
        "principal_id": ctx["principal_id"],
        "display_name": ctx["display_name"],
        "store_slugs": ctx["store_slugs"],
        "current_store": ctx["current_store"],
        "path": ctx["path"],
        "permissions": ctx["permissions"],
        "is_owner_operator": ctx.get("is_owner_operator", False),
        "can_ask_personal": ctx["can_ask_personal"],
        "can_ask_operational": ctx["can_ask_operational"],
    }


def _date_key(value: Any) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return None
    text = str(value).strip()
    return text[:10] if text else None


def _store_key_for_order(order: Order) -> str:
    raw = (
        order.origin_store_id
        or order.pickup_kitchen
        or order.reported_store_id
        or order.reported_store
        or "unknown"
    )
    value = str(raw).strip().casefold()
    aliases = {
        "1": "copperfield",
        "uno": "copperfield",
        "uno mas": "copperfield",
        "copperfield": "copperfield",
        "2": "tomball",
        "dos": "tomball",
        "dos mas": "tomball",
        "tomball": "tomball",
    }
    return aliases.get(value, value or "unknown")


def _order_needs_driver(order: Order) -> bool:
    status = (order.status or "").casefold()
    has_driver = bool(order.assigned_driver_id or order.ezcater_driver_name or order.assigned_driver)
    return not has_driver and status in {"new", "available", "requested", "needs_driver", "needs_review"}


def _order_delivery_minute(order: Order) -> int | None:
    if isinstance(order.delivery_window_start, datetime):
        return order.delivery_window_start.hour * 60 + order.delivery_window_start.minute
    text = str(order.deliver_at or "").strip()
    if not text:
        return None
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").casefold()
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _time_window_key(minute: int | None, now_minute: int) -> str:
    if minute is None:
        return "unknown_time"
    if minute < 12 * 60:
        return "morning"
    if minute < 17 * 60:
        return "afternoon"
    return "evening"


def _increment_count(mapping: dict[str, int], key: str) -> None:
    mapping[key] = mapping.get(key, 0) + 1


def _increment_nested_count(mapping: dict[str, dict[str, int]], outer: str, inner: str) -> None:
    bucket = mapping.setdefault(outer, {})
    bucket[inner] = bucket.get(inner, 0) + 1


def _tool_store_filter(ctx: dict[str, Any]) -> set[str]:
    if ctx.get("is_owner_operator"):
        return set()
    return {str(store).casefold() for store in (ctx.get("store_slugs") or [])}


def _orders_store_summary(ctx: dict[str, Any]) -> dict[str, Any]:
    today = date.today().isoformat()
    db = SessionLocal()
    try:
        orders = db.query(Order).all()
        allowed = _tool_store_filter(ctx)
        if allowed:
            orders = [
                order for order in orders
                if _store_key_for_order(order).casefold() in allowed
            ]
        by_store: dict[str, int] = {}
        today_by_store: dict[str, int] = {}
        today_time_windows: dict[str, int] = {
            "morning": 0,
            "afternoon": 0,
            "evening": 0,
            "earlier_today": 0,
            "unknown_time": 0,
        }
        today_time_windows_by_store: dict[str, dict[str, int]] = {}
        status_counts: dict[str, int] = {}
        today_orders = 0
        upcoming_orders = 0
        needs_driver = 0
        live_tracking = 0
        active_tracking = 0
        now_minute = datetime.now().hour * 60 + datetime.now().minute
        for order in orders:
            order_date = _date_key(order.delivery_date)
            store = _store_key_for_order(order)
            if order_date == today:
                today_orders += 1
                _increment_count(today_by_store, store)
                minute = _order_delivery_minute(order)
                window = _time_window_key(minute, now_minute)
                _increment_count(today_time_windows, window)
                _increment_nested_count(today_time_windows_by_store, window, store)
                if minute is not None and minute <= now_minute:
                    _increment_count(today_time_windows, "earlier_today")
                    _increment_nested_count(today_time_windows_by_store, "earlier_today", store)
            if order_date and order_date >= today:
                upcoming_orders += 1
            by_store[store] = by_store.get(store, 0) + 1
            status = (order.status or "unknown").casefold()
            status_counts[status] = status_counts.get(status, 0) + 1
            if _order_needs_driver(order):
                needs_driver += 1
            if order.delivery_tracking_id:
                live_tracking += 1
            if order.delivery_tracking_id and order.ezcater_status_key not in {None, "", "expired", "completed", "delivered"}:
                active_tracking += 1

        return {
            "generated_at": _now_iso(),
            "data_class": "operations_aggregate_sanitized",
            "today": today,
            "total_orders": len(orders),
            "today_orders": today_orders,
            "upcoming_orders": upcoming_orders,
            "needs_driver_orders": needs_driver,
            "live_tracking_orders": live_tracking,
            "active_tracking_orders": active_tracking,
            "by_store": by_store,
            "today_by_store": today_by_store,
            "today_time_windows": today_time_windows,
            "today_time_windows_by_store": today_time_windows_by_store,
            "status_counts": status_counts,
        }
    finally:
        db.close()


def _drivers_store_summary(ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        drivers = db.query(Driver).all()
        allowed = _tool_store_filter(ctx)
        if allowed:
            drivers = [
                driver for driver in drivers
                if str(driver.home_store_id or driver.location or "").casefold() in allowed
            ]
        active = [
            driver for driver in drivers
            if driver.active and (driver.status or "active").casefold() == "active"
        ]
        by_store: dict[str, int] = {}
        score_count = 0
        score_total = 0
        for driver in drivers:
            store = str(driver.home_store_id or driver.location or "unknown").casefold()
            by_store[store] = by_store.get(store, 0) + 1
            if driver.current_score is not None:
                score_count += 1
                score_total += int(driver.current_score)
        active_shift_driver_ids = {
            row.driver_id
            for row in db.query(DriverShift.driver_id).filter(DriverShift.ended_at.is_(None)).all()
        }
        active_delivery_driver_ids = {
            row.assigned_driver_id
            for row in db.query(Order.assigned_driver_id)
            .filter(Order.assigned_driver_id.isnot(None))
            .filter(Order.status.in_(["approved", "picked_up", "en_route", "requested"]))
            .all()
        }
        active_ids = {driver.id for driver in active}
        return {
            "generated_at": _now_iso(),
            "data_class": "driver_aggregate_sanitized",
            "total_drivers": len(drivers),
            "active_drivers": len(active),
            "inactive_drivers": max(0, len(drivers) - len(active)),
            "drivers_on_shift": len(active_shift_driver_ids & active_ids),
            "drivers_on_active_orders": len(active_delivery_driver_ids & active_ids),
            "average_score": round(score_total / score_count, 1) if score_count else None,
            "by_store": by_store,
        }
    finally:
        db.close()


def _labor_store_aggregate(ctx: dict[str, Any]) -> dict[str, Any]:
    today = date.today()
    db = SessionLocal()
    try:
        employees = db.query(Employee).all()
        allowed = _tool_store_filter(ctx)
        if allowed:
            employee_ids = {
                row.employee_id
                for row in db.query(EmployeeStoreAssignment.employee_id)
                .filter(EmployeeStoreAssignment.store_key.in_(list(allowed)))
                .all()
            }
            employees = [employee for employee in employees if employee.id in employee_ids]
        employee_ids = {employee.id for employee in employees}
        active_employees = [employee for employee in employees if employee.active]
        by_store: dict[str, int] = {}
        if allowed and not employee_ids:
            assignments = []
            shifts = []
        else:
            assignment_query = db.query(EmployeeStoreAssignment)
            if employee_ids:
                assignment_query = assignment_query.filter(EmployeeStoreAssignment.employee_id.in_(employee_ids))
            assignments = assignment_query.all()
            published_schedule_query = db.query(Schedule.id).filter(Schedule.status == "published")
            if allowed:
                published_schedule_query = published_schedule_query.filter(Schedule.store_key.in_(list(allowed)))
            published_schedule_ids = [row.id for row in published_schedule_query.all()]
            shift_query = db.query(Shift)
            if published_schedule_ids:
                shift_query = shift_query.filter(Shift.schedule_id.in_(published_schedule_ids))
            else:
                shift_query = shift_query.filter(Shift.id == -1)
            if employee_ids:
                shift_query = shift_query.filter(
                    (Shift.employee_id.in_(employee_ids)) | (Shift.employee_id.is_(None))
                )
            shifts = shift_query.all()
        for assignment in assignments:
            store = str(assignment.store_key or "unknown").casefold()
            by_store[store] = by_store.get(store, 0) + 1
        today_attendance = db.query(AttendanceShift).filter(AttendanceShift.entry_date == today).all()
        if allowed:
            today_attendance = [
                row for row in today_attendance
                if (row.store_scope or "").casefold() in allowed
            ]
        attendance_statuses: dict[str, int] = {}
        for row in today_attendance:
            status = (row.status or "unknown").casefold()
            attendance_statuses[status] = attendance_statuses.get(status, 0) + 1
        perf_rows = db.query(PerfPeriodCache).all()
        if employee_ids:
            perf_rows = [row for row in perf_rows if row.cena_employee_id in employee_ids]
        elif allowed:
            perf_rows = []
        total_hours = sum(float(row.total_hours or 0.0) for row in perf_rows if row.period == "last30")
        latest_sync = max((row.synced_at for row in perf_rows if row.synced_at), default=None)
        return {
            "generated_at": _now_iso(),
            "data_class": "labor_aggregate_sanitized",
            "total_employees": len(employees),
            "active_employees": len(active_employees),
            "inactive_employees": max(0, len(employees) - len(active_employees)),
            "by_store_assignments": by_store,
            "published_shifts": len([shift for shift in shifts if shift.status != "open"]),
            "open_shifts": len([shift for shift in shifts if shift.status == "open"]),
            "today_attendance_statuses": attendance_statuses,
            "last30_cached_hours": round(total_hours, 2),
            "perf_cache_rows": len(perf_rows),
            "latest_perf_sync": latest_sync.isoformat() if latest_sync else None,
        }
    finally:
        db.close()


def _tool_is_available(ctx: dict[str, Any], tool_id: str) -> bool:
    return any(
        tool["tool_id"] == tool_id and tool.get("available")
        for tool in _tool_catalog_for(ctx)
    )


def _toast_period_from_question(question: str) -> str:
    text = str(question or "").casefold()
    if "last week" in text or "previous week" in text:
        return "last_week"
    if "this week" in text or re.search(r"\bweek\b", text):
        return "week"
    return "today"


def _wants_toast_sales_summary(question: str) -> bool:
    return bool(_TOAST_SALES_RE.search(str(question or "")))


def _wants_toast_table_activity(question: str) -> bool:
    text = str(question or "")
    return bool(
        _TOAST_TABLE_ACTIVITY_RE.search(text)
        and re.search(r"\b(tomball|dos|dos mas|copperfield|uno|uno mas|today|latest|recent|open|opened)\b", text, re.IGNORECASE)
    )


def _requested_store(question: str) -> str | None:
    text = str(question or "").casefold()
    aliases = {
        "tomball": "tomball",
        "dos mas": "tomball",
        "dos": "tomball",
        "copperfield": "copperfield",
        "uno mas": "copperfield",
        "uno": "copperfield",
    }
    for alias, store in aliases.items():
        escaped = re.escape(alias).replace(r"\ ", r"\s+")
        if re.search(rf"\b{escaped}\b", text):
            return store
    return None


def _toast_sales_summary_tool_payload(period: str) -> dict[str, Any]:
    from app.services.toast_analytics_summary import analytics_summary_payload

    return analytics_summary_payload(period)


def _toast_table_activity_tool_payload(location: str | None) -> dict[str, Any]:
    from app.services.toast_table_activity import latest_table_activity_payload

    return latest_table_activity_payload(location)


def _approved_tool_data(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    if not _has_partner_tool_access(ctx):
        return {}
    data: dict[str, Any] = {}
    if _tool_is_available(ctx, "orders.store_summary"):
        try:
            data["orders.store_summary"] = _orders_store_summary(ctx)
        except Exception:  # noqa: BLE001
            log.exception("assistant: failed to build orders.store_summary")
    if _tool_is_available(ctx, "drivers.store_summary"):
        try:
            data["drivers.store_summary"] = _drivers_store_summary(ctx)
        except Exception:  # noqa: BLE001
            log.exception("assistant: failed to build drivers.store_summary")
    if _tool_is_available(ctx, "labor.store_aggregate"):
        try:
            data["labor.store_aggregate"] = _labor_store_aggregate(ctx)
        except Exception:  # noqa: BLE001
            log.exception("assistant: failed to build labor.store_aggregate")
    if _tool_is_available(ctx, "toast.sales_summary") and _wants_toast_sales_summary(question):
        try:
            data["toast.sales_summary"] = _toast_sales_summary_tool_payload(
                _toast_period_from_question(question)
            )
        except Exception:  # noqa: BLE001
            log.exception("assistant: failed to build toast.sales_summary")
    if _tool_is_available(ctx, "toast.table_activity") and _wants_toast_table_activity(question):
        try:
            data["toast.table_activity"] = _toast_table_activity_tool_payload(
                _requested_store(question)
            )
        except Exception:  # noqa: BLE001
            log.exception("assistant: failed to build toast.table_activity")
    return data


def _contextual_followup(question: str, previous_question: str) -> bool:
    if not previous_question.strip():
        return False
    if re.search(r"^\s*(what about|how about|what baout|and\b|earlier|this morning|this afternoon|tonight)", question, re.IGNORECASE):
        return True
    if _OPERATIONAL_NOUN_RE.search(question):
        return False
    return bool(_FOLLOWUP_RE.search(question) or _DATA_TOOL_RE.search(question))


def _resolved_question(question: str, previous_question: str = "") -> str:
    question = str(question or "").strip()
    previous_question = str(previous_question or "").strip()
    if _contextual_followup(question, previous_question):
        return f"{previous_question}\nFollow-up: {question}"
    return question


def _previous_question_from_body(body: dict[str, Any], current_question: str) -> str:
    direct = str(body.get("previous_question") or "").strip()
    if direct and direct != current_question:
        return direct[:_MAX_QUESTION_CHARS]
    history = body.get("history")
    if isinstance(history, list):
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").casefold()
            text = str(item.get("content") or item.get("question") or "").strip()
            if role == "user" and text and text != current_question:
                return text[:_MAX_QUESTION_CHARS]
    return ""


def _post_to_ck_runtime(
    question: str,
    ctx: dict[str, Any],
    previous_question: str = "",
) -> tuple[dict[str, Any], int] | None:
    """Send the assistant turn to the CK-local runtime.

    The runtime is the execution boundary for production: model calls, durable
    question storage, and future data tools stay on CK. Render is only a
    signed web/session proxy when this URL is configured.
    """
    url = _ck_runtime_url(_env_first("AI_ASSISTANT_CK_RUNTIME_URL", "ASSISTANT_RUNTIME_URL"))
    token = _env_first("AI_ASSISTANT_CK_RUNTIME_TOKEN", "ASSISTANT_RUNTIME_TOKEN")
    if not url or not token:
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Ai-Assistant-Token": token,
        "Content-Type": "application/json",
    }
    payload = {
        "question": question,
        "principal": _runtime_principal(ctx),
        "tools": _tool_catalog_for(ctx),
        "tool_data": _approved_tool_data(_resolved_question(question, previous_question), ctx),
        "source": "cenas_app",
        "requested_at": _now_iso(),
    }
    if previous_question:
        payload["previous_question"] = previous_question
    try:
        import httpx

        proxy = (os.getenv("CENA_PROXY") or "").strip() or None
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(_review_timeout_seconds(), connect=min(3.0, _review_timeout_seconds())),
        }
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as hx:
            resp = hx.post(url, json=payload, headers=headers)
        data = resp.json() if resp.content else {}
        if 200 <= resp.status_code < 300:
            return data, resp.status_code
        return {
            "ok": False,
            "error": "ck_runtime_rejected",
            "answer": "I could not reach the CK assistant safely right now.",
            "queued": False,
        }, 503
    except Exception:  # noqa: BLE001
        log.exception("assistant: CK runtime call failed")
        return {
            "ok": False,
            "error": "ck_runtime_unavailable",
            "answer": "I could not reach the CK assistant safely right now.",
            "queued": False,
        }, 503


def _queue_for_review(question: str, ctx: dict[str, Any], reason: str,
                      required_permission: str | None = None) -> dict[str, Any]:
    row = {
        "id": str(uuid.uuid4()),
        "created_at": _now_iso(),
        "status": _REVIEW_STATUS,
        "risk_level": _review_risk_level(reason),
        "question": question,
        "reason": reason,
        "required_permission": required_permission,
        "role": ctx["role"],
        "store_key": ctx["current_store"] or ((ctx["store_slugs"] or [None])[0]),
        "model_key": "review_queue",
        "tool_name": required_permission or "assistant.general_help",
        "delivery_target": "ck_assistant_review",
        "principal": {
            "kind": ctx["kind"],
            "role": ctx["role"],
            "principal_id": ctx["principal_id"],
            "display_name": ctx["display_name"],
            "store_slugs": ctx["store_slugs"],
            "current_store": ctx["current_store"],
            "path": ctx["path"],
            "permissions": ctx["permissions"],
            "is_owner_operator": ctx.get("is_owner_operator", False),
        },
    }
    saved_on_ck, ck_id = _post_to_ck_review(row)
    if saved_on_ck:
        row["storage"] = "ck"
        row["ck_question_id"] = ck_id
    else:
        row["storage"] = "render_retry_outbox"
        row["outbox_note"] = (
            "CK review ingress unavailable or not configured; CK remains "
            "the authoritative target."
        )
        _append_pending_question(row)
    return row


def _queued_answer(reason: str) -> str:
    if reason in {
        "sensitive_or_operational_question_needs_approved_tool",
        "data_question_needs_approved_tool",
    }:
        return "I do not have the approved Cenas data tool for that yet, so I saved it for Sam review."
    if reason == "not_authenticated":
        return "Please sign in first. I saved the question for Sam review."
    return "I can't safely answer that from your current permissions yet, so I saved it for Sam review."


def _anthropic_answer(question: str, ctx: dict[str, Any]) -> tuple[str | None, str | None]:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None, None
    try:
        import anthropic  # type: ignore[import]
    except ImportError:
        log.warning("assistant: anthropic package not installed")
        return None, None

    model = os.getenv("AI_ASSISTANT_ANTHROPIC_MODEL", _DEFAULT_MODEL)
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model,
        max_tokens=800,
        temperature=0.2,
        system=_anthropic_system_blocks(ctx),
        messages=[{"role": "user", "content": question}],
    )
    text = "".join(
        block.text for block in getattr(msg, "content", [])
        if getattr(block, "type", None) == "text"
    ).strip()
    return text or None, model


def _gemini_answer(question: str, ctx: dict[str, Any]) -> tuple[str | None, str | None]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None, None
    try:
        from google import genai  # type: ignore[import]
    except ImportError:
        log.warning("assistant: google-genai package not installed")
        return None, None

    model = os.getenv("AI_ASSISTANT_GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=key)
    prompt = _system_prompt(ctx) + "\n\nUser question:\n" + question
    resp = client.models.generate_content(model=model, contents=prompt)
    text = (getattr(resp, "text", None) or "").strip()
    return text or None, model


def _system_prompt(ctx: dict[str, Any]) -> str:
    return (
        _stable_policy_prompt()
        + "\n\n"
        + _session_prompt(ctx)
    )


def _stable_policy_prompt() -> str:
    return (
        "You are the Cenas Kitchen in-app assistant. Answer only within the "
        "current user's role and permissions. You do not reveal secrets, "
        "passcodes, tokens, customer PII, unauthorized payroll, raw peer pay, "
        "sales internals, GUIDs, or cross-store data. Answer operational data "
        "questions only from approved, sanitized read-only tool payloads. "
        "Review-gated tools are visible to the server but not available to you "
        "for answers. If the user asks for private data or operational facts "
        "that require an unavailable tool, say the question needs Sam review "
        "and do not guess. If owner_operator=true in the authenticated session, "
        "use that session context for permission decisions; do not ask the user "
        "to prove they are Sam in chat."
    )


def _session_prompt(ctx: dict[str, Any]) -> str:
    return (
        f"Current session: role={ctx['role']}, kind={ctx['kind']}, "
        f"stores={ctx['store_slugs']}, path={ctx['path']}, "
        f"owner_operator={bool(ctx.get('is_owner_operator'))}."
    )


def _anthropic_system_blocks(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": _stable_policy_prompt(),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": _session_prompt(ctx),
        },
    ]


def _should_queue(question: str, ctx: dict[str, Any]) -> tuple[bool, str, str | None]:
    if ctx["kind"] == "anonymous":
        return True, "not_authenticated", "ai.ask_claude_personal"
    if not ctx["can_ask_personal"]:
        return True, "missing_ai_permission", "ai.ask_claude_personal"
    if _SENSITIVE_RE.search(question):
        needed = "ai.ask_claude"
        if not ctx["can_ask_operational"]:
            return True, "sensitive_or_operational_question_requires_higher_permission", needed
        return True, "sensitive_or_operational_question_needs_approved_tool", needed
    if _DATA_TOOL_RE.search(question):
        needed = "ai.ask_claude"
        if not ctx["can_ask_operational"]:
            return True, "data_question_requires_higher_permission", needed
        return True, "data_question_needs_approved_tool", needed
    return False, "", None


@assistant_bp.route("/assistant/context", methods=["GET"])
def assistant_context():
    ctx = _principal_context()
    tools = _tool_catalog_for(ctx)
    enabled = _assistant_available_for_context(ctx)
    return jsonify({
        "ok": ctx["kind"] != "anonymous",
        "principal": {
            "kind": ctx["kind"],
            "role": ctx["role"],
            "display_name": ctx["display_name"],
            "store_slugs": ctx["store_slugs"],
            "is_owner_operator": ctx.get("is_owner_operator", False),
        },
        "enabled": bool(enabled),
        "tools": [
            {
                "tool_id": tool["tool_id"],
                "label": tool["label"],
                "status": tool["status"],
                "available": tool["available"],
                "deny_reason": tool["deny_reason"],
            }
            for tool in tools
        ],
    })


@assistant_bp.route("/assistant/tools", methods=["GET"])
def assistant_tools():
    ctx = _principal_context()
    if ctx["kind"] == "anonymous":
        return jsonify({"ok": False, "error": "not_authenticated"}), 401
    return jsonify({
        "ok": True,
        "generated_at": _now_iso(),
        "tools": _tool_catalog_for(ctx),
    })


@assistant_bp.route("/assistant/ask", methods=["POST"])
def assistant_ask():
    if not _assistant_enabled():
        return jsonify({"ok": False, "error": "assistant_disabled"}), 503

    ctx = _principal_context()
    if not _assistant_available_for_context(ctx):
        return jsonify({"ok": False, "error": "assistant_unavailable"}), 503

    body = request.get_json(silent=True) or {}
    question = str(body.get("question") or "").strip()[:_MAX_QUESTION_CHARS]
    if not question:
        return jsonify({"ok": False, "error": "question required"}), 400
    previous_question = _previous_question_from_body(body, question)
    safety_question = _resolved_question(question, previous_question)

    runtime_response = _post_to_ck_runtime(question, ctx, previous_question)
    if runtime_response is not None:
        data, status = runtime_response
        return jsonify(data), status

    if os.getenv("RENDER") and not _env_truthy("AI_ASSISTANT_ALLOW_RENDER_MODELS"):
        return jsonify({"ok": False, "error": "ck_runtime_required"}), 503

    should_queue, reason, required = _should_queue(safety_question, ctx)
    if should_queue:
        row = _queue_for_review(question, ctx, reason, required)
        return jsonify({
            "ok": True,
            "answer": _queued_answer(reason),
            "queued": True,
            "queue_id": row["id"],
            "storage": row.get("storage"),
            "ck_question_id": row.get("ck_question_id"),
            "reason": reason,
        })

    try:
        answer, model = _anthropic_answer(question, ctx)
        if answer is None:
            answer, model = _gemini_answer(question, ctx)
    except Exception:
        log.exception("assistant answer failed")
        answer = None
        model = None

    if not answer:
        row = _queue_for_review(question, ctx, "model_unavailable_or_no_answer", None)
        return jsonify({
            "ok": True,
            "answer": "I saved that for Sam review. The assistant model is not available right now.",
            "queued": True,
            "queue_id": row["id"],
            "storage": row.get("storage"),
            "ck_question_id": row.get("ck_question_id"),
            "reason": "model_unavailable_or_no_answer",
        })

    return jsonify({"ok": True, "answer": answer, "queued": False, "model": model})


@assistant_bp.route("/cron/assistant-questions-export", methods=["GET"])
def assistant_questions_export():
    expected = os.getenv("AI_ASSISTANT_EXPORT_TOKEN")
    if not expected or _extract_token() != expected:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    path = _queue_path()
    rows: list[dict[str, Any]] = []
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return jsonify({"ok": True, "generated_at": _now_iso(), "rows": rows})
