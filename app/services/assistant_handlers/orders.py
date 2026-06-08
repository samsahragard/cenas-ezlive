"""Read-only Cenas AI handlers for order and catering questions."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    DriverAssignmentJob,
    EzcaterOrderDetails,
    InHouseCateringQuote,
    Order,
    OrderItem,
    ProcessingOrder,
)
from app.services.assistant_routing_shared import (
    STORE_ALIASES as _STORE_ALIASES,
    normalize_store_key as _normalize_store_key,
)


MAX_SAMPLE_ROWS = 20
MAX_ITEM_ROWS = 25
_COMPLETE_STATUSES = {"completed", "complete", "delivered", "cancelled", "canceled", "void"}
_ACTIVE_TRACKING_DONE = {"", "expired", "completed", "complete", "delivered", "cancelled", "canceled"}
_LOOKUP_TOKEN_STOP_WORDS = {
    "order",
    "orders",
    "catering",
    "caterings",
    "ezcater",
    "ticket",
    "tickets",
    "quote",
    "quotes",
    "id",
    "ids",
    "item",
    "items",
    "food",
    "detail",
    "details",
    "number",
    "numbers",
    "no",
    "status",
    "statuses",
    "summary",
    "count",
    "total",
}
_REAL_EZCATER_ID_RE = re.compile(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", re.IGNORECASE)
_KEYWORD_LOOKUP_TOKEN_RE = re.compile(
    r"\b(?:order|catering|ezcater|ticket|quote)\s*"
    r"(?:id|#|number|no\.?)?\s*[:#-]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9_-]{2,})\b",
    re.IGNORECASE,
)
_ITEM_MIX_AGGREGATE_RE = re.compile(
    r"\b("
    r"what\s+items?\s+get\s+ordered\s+most|"
    r"items?\s+(?:get\s+)?ordered\s+most|"
    r"most\s+ordered|"
    r"ordered\s+most|"
    r"most\s+popular|"
    r"popular\s+items?|"
    r"best[-\s]+selling|"
    r"top[-\s]+selling"
    r")\b",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _date_key(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return None
    text = str(value).strip()
    return text[:10] if text else None


def _parse_date(value: Any) -> date | None:
    key = _date_key(value)
    if not key:
        return None
    try:
        return date.fromisoformat(key)
    except ValueError:
        return None


def _normalize_store(raw: Any) -> str:
    return _normalize_store_key(raw)


def _store_key_for_order(order: Order) -> str:
    return _normalize_store(
        order.origin_store_id
        or order.pickup_kitchen
        or order.reported_store_id
        or order.reported_store
        or "unknown"
    )


def _store_key_for_quote(quote: InHouseCateringQuote) -> str:
    return _normalize_store(quote.store_scope or "unknown")


def _tool_store_filter(ctx: dict[str, Any]) -> set[str] | None:
    if ctx.get("is_owner_operator"):
        return None
    return {_normalize_store(store) for store in (ctx.get("store_slugs") or [])}


def _status_key(value: Any) -> str:
    return str(value or "unknown").strip().casefold() or "unknown"


def _order_needs_driver(order: Order) -> bool:
    status = _status_key(order.status)
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


def _time_window_key(minute: int | None) -> str:
    if minute is None:
        return "unknown_time"
    if minute < 12 * 60:
        return "morning"
    if minute < 17 * 60:
        return "afternoon"
    return "evening"


def _is_tracking_active(order: Order) -> bool:
    if not order.delivery_tracking_id:
        return False
    return _status_key(order.ezcater_status_key) not in _ACTIVE_TRACKING_DONE


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _allowed_orders(db: Session, ctx: dict[str, Any]) -> list[Order]:
    orders = db.query(Order).all()
    allowed = _tool_store_filter(ctx)
    if allowed is None:
        return orders
    if not allowed:
        return []
    return [order for order in orders if _store_key_for_order(order) in allowed]


def _allowed_quotes(db: Session, ctx: dict[str, Any]) -> list[InHouseCateringQuote]:
    quotes = db.query(InHouseCateringQuote).all()
    allowed = _tool_store_filter(ctx)
    if allowed is None:
        return quotes
    if not allowed:
        return []
    return [quote for quote in quotes if _store_key_for_quote(quote) in allowed]


def _visible_external_ids(orders: list[Order]) -> set[str]:
    return {str(order.external_order_id).strip() for order in orders if order.external_order_id}


def _filter_by_range(orders: list[Order], start: date, end: date) -> list[Order]:
    return [
        order for order in orders
        if (order_date := _parse_date(order.delivery_date)) and start <= order_date <= end
    ]


def _sort_orders(orders: list[Order]) -> list[Order]:
    return sorted(
        orders,
        key=lambda order: (
            _parse_date(order.delivery_date) or date.max,
            _order_delivery_minute(order) if _order_delivery_minute(order) is not None else 24 * 60,
            str(order.external_order_id or ""),
        ),
    )


def _safe_order(order: Order, *, include_tracking: bool = True) -> dict[str, Any]:
    payload = {
        "external_order_id": order.external_order_id,
        "store": _store_key_for_order(order),
        "delivery_date": _date_key(order.delivery_date),
        "deliver_at": order.deliver_at,
        "status": order.status,
        "headcount": order.headcount,
        "setup_required": bool(order.setup_required) if order.setup_required is not None else None,
        "total_amount": _money(order.total_amount),
        "needs_driver": _order_needs_driver(order),
        "assigned_driver_name": order.assigned_driver or order.ezcater_driver_name,
    }
    if include_tracking:
        payload.update({
            "has_tracking_uuid": bool(order.delivery_tracking_id),
            "tracking_status": order.ezcater_status_key or order.tracking_status,
            "tracking_active": _is_tracking_active(order),
            "tracking_updated_at": (
                order.ezcater_status_updated_at.isoformat()
                if order.ezcater_status_updated_at
                else None
            ),
        })
    return payload


def _safe_item(item: OrderItem) -> dict[str, Any]:
    label = item.item_key or "unmapped_item"
    return {
        "item_key": item.item_key,
        "label": label,
        "qty": item.qty,
        "package_type": item.package_type,
        "packaging": item.packaging,
        "servings": item.servings,
    }


def _safe_quote(quote: InHouseCateringQuote) -> dict[str, Any]:
    return {
        "quote_id": quote.id,
        "created_at": quote.created_at.isoformat() if quote.created_at else None,
        "store": _store_key_for_quote(quote),
        "event_date": quote.event_date.isoformat() if quote.event_date else None,
        "guest_count": quote.guest_count,
        "subtotal": _money(quote.subtotal),
        "status": quote.status,
        "email_sent": bool(quote.email_sent_at),
        "linked_order_id": quote.ezorder_id,
    }


def _payload(tool_id: str, ctx: dict[str, Any], **data: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "tool_id": tool_id,
        "generated_at": _now_iso(),
        "data_class": "orders_read_sanitized",
        "scope": {
            "owner_operator": bool(ctx.get("is_owner_operator")),
            "store_slugs": list(ctx.get("store_slugs") or []),
            "current_store": ctx.get("current_store"),
        },
        **data,
    }


def _is_lookup_stop_word(token: str) -> bool:
    normalized = str(token or "").strip(" .:#-_").casefold()
    return normalized in _LOOKUP_TOKEN_STOP_WORDS


def _looks_like_order_id(token: str) -> bool:
    text = str(token or "").strip()
    return bool(text and (re.search(r"\d", text) or "-" in text or "_" in text))


def _keyword_lookup_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    while match := _KEYWORD_LOOKUP_TOKEN_RE.search(text, pos):
        token = match.group(1).strip()
        tokens.append(token)
        next_pos = match.start(1) if _is_lookup_stop_word(token) else match.end()
        if next_pos <= pos:
            next_pos = match.start() + 1
        pos = next_pos
    return tokens


def _lookup_token(question: str) -> str | None:
    text = str(question or "").strip()
    real_id = _REAL_EZCATER_ID_RE.search(text)
    if real_id:
        return real_id.group(1).strip()
    for token in _keyword_lookup_tokens(text):
        if not _is_lookup_stop_word(token):
            return token
    patterns = [r"\b([A-Z]{1,4}-?\d{2,})\b", r"\b(\d{4,})\b"]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            token = match.group(1).strip()
            if _is_lookup_stop_word(token):
                continue
            return token
    return None


def _has_explicit_order_reference(question: str) -> bool:
    text = str(question or "")
    if _REAL_EZCATER_ID_RE.search(text):
        return True
    for token in _keyword_lookup_tokens(text):
        if not _is_lookup_stop_word(token) and _looks_like_order_id(token):
            return True
    return False


def _find_order(orders: list[Order], question: str) -> tuple[Order | None, str | None]:
    token = _lookup_token(question)
    if token:
        token_cf = token.casefold()
        for order in orders:
            if str(order.external_order_id or "").casefold() == token_cf:
                return order, token
        for order in orders:
            if token_cf in str(order.external_order_id or "").casefold():
                return order, token
        return None, token
    sorted_orders = _sort_orders(orders)
    return (sorted_orders[-1] if sorted_orders else None), None


def _details_by_external_id(db: Session, external_ids: set[str]) -> dict[str, EzcaterOrderDetails]:
    if not external_ids:
        return {}
    rows = (
        db.query(EzcaterOrderDetails)
        .filter(EzcaterOrderDetails.external_order_id.in_(list(external_ids)))
        .all()
    )
    return {row.external_order_id: row for row in rows}


def _processing_rows(db: Session, external_ids: set[str]) -> list[ProcessingOrder]:
    if not external_ids:
        return []
    return (
        db.query(ProcessingOrder)
        .filter(ProcessingOrder.external_order_id.in_(list(external_ids)))
        .all()
    )


def _orders_window_payload(
    tool_id: str,
    question: str,
    ctx: dict[str, Any],
    start: date,
    end: date,
    window_label: str,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _filter_by_range(_allowed_orders(db, ctx), start, end)
        sorted_orders = _sort_orders(orders)
        by_store = Counter(_store_key_for_order(order) for order in sorted_orders)
        by_status = Counter(_status_key(order.status) for order in sorted_orders)
        return _payload(
            tool_id,
            ctx,
            window=window_label,
            question=question,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            count=len(sorted_orders),
            by_store=dict(by_store),
            by_status=dict(by_status),
            needs_driver_count=sum(1 for order in sorted_orders if _order_needs_driver(order)),
            active_tracking_count=sum(1 for order in sorted_orders if _is_tracking_active(order)),
            orders=[_safe_order(order) for order in sorted_orders[:MAX_SAMPLE_ROWS]],
            truncated=len(sorted_orders) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def orders_store_summary(question_or_ctx: str | dict[str, Any], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    if ctx is None and isinstance(question_or_ctx, dict):
        ctx = question_or_ctx
        question = ""
    else:
        question = str(question_or_ctx or "")
        ctx = ctx or {}
    today = date.today()
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
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
            order_date = _parse_date(order.delivery_date)
            store = _store_key_for_order(order)
            if order_date == today:
                today_orders += 1
                today_by_store[store] = today_by_store.get(store, 0) + 1
                minute = _order_delivery_minute(order)
                window = _time_window_key(minute)
                today_time_windows[window] = today_time_windows.get(window, 0) + 1
                today_time_windows_by_store.setdefault(window, {})
                today_time_windows_by_store[window][store] = (
                    today_time_windows_by_store[window].get(store, 0) + 1
                )
                if minute is not None and minute <= now_minute:
                    today_time_windows["earlier_today"] += 1
                    today_time_windows_by_store.setdefault("earlier_today", {})
                    today_time_windows_by_store["earlier_today"][store] = (
                        today_time_windows_by_store["earlier_today"].get(store, 0) + 1
                    )
            if order_date and order_date >= today:
                upcoming_orders += 1
            by_store[store] = by_store.get(store, 0) + 1
            status = _status_key(order.status)
            status_counts[status] = status_counts.get(status, 0) + 1
            if _order_needs_driver(order):
                needs_driver += 1
            if order.delivery_tracking_id:
                live_tracking += 1
            if _is_tracking_active(order):
                active_tracking += 1
        return _payload(
            "orders.store_summary",
            ctx,
            question=question,
            today=today.isoformat(),
            total_orders=len(orders),
            today_orders=today_orders,
            upcoming_orders=upcoming_orders,
            needs_driver_orders=needs_driver,
            live_tracking_orders=live_tracking,
            active_tracking_orders=active_tracking,
            by_store=by_store,
            today_by_store=today_by_store,
            today_time_windows=today_time_windows,
            today_time_windows_by_store=today_time_windows_by_store,
            status_counts=status_counts,
        )
    finally:
        db.close()


def catering_today(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    today = date.today()
    return _orders_window_payload("orders.catering_today", question, ctx, today, today, "today")


def catering_tomorrow(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    tomorrow = date.today() + timedelta(days=1)
    return _orders_window_payload(
        "orders.catering_tomorrow",
        question,
        ctx,
        tomorrow,
        tomorrow,
        "tomorrow",
    )


def catering_week(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    today = date.today()
    return _orders_window_payload(
        "orders.catering_week",
        question,
        ctx,
        today,
        today + timedelta(days=6),
        "next_7_days",
    )


def catering_next_30_days(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    today = date.today()
    return _orders_window_payload(
        "orders.catering_next_30_days",
        question,
        ctx,
        today,
        today + timedelta(days=30),
        "next_30_days",
    )


def catering_count(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        today = date.today()
        orders = _allowed_orders(db, ctx)
        today_orders = _filter_by_range(orders, today, today)
        tomorrow = today + timedelta(days=1)
        return _payload(
            "orders.catering_count",
            ctx,
            question=question,
            total_count=len(orders),
            today_count=len(today_orders),
            tomorrow_count=len(_filter_by_range(orders, tomorrow, tomorrow)),
            next_7_days_count=len(_filter_by_range(orders, today, today + timedelta(days=6))),
            next_30_days_count=len(_filter_by_range(orders, today, today + timedelta(days=30))),
        )
    finally:
        db.close()


def catering_by_status(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
        return _payload(
            "orders.catering_by_status",
            ctx,
            question=question,
            total_count=len(orders),
            by_status=dict(Counter(_status_key(order.status) for order in orders)),
        )
    finally:
        db.close()


def catering_by_store(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
        return _payload(
            "orders.catering_by_store",
            ctx,
            question=question,
            total_count=len(orders),
            by_store=dict(Counter(_store_key_for_order(order) for order in orders)),
        )
    finally:
        db.close()


def catering_needs_driver(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = [order for order in _allowed_orders(db, ctx) if _order_needs_driver(order)]
        sorted_orders = _sort_orders(orders)
        return _payload(
            "orders.catering_needs_driver",
            ctx,
            question=question,
            count=len(sorted_orders),
            by_store=dict(Counter(_store_key_for_order(order) for order in sorted_orders)),
            orders=[_safe_order(order) for order in sorted_orders[:MAX_SAMPLE_ROWS]],
            truncated=len(sorted_orders) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def catering_live_tracking(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = [order for order in _allowed_orders(db, ctx) if order.delivery_tracking_id]
        active_orders = [order for order in orders if _is_tracking_active(order)]
        return _payload(
            "orders.catering_live_tracking",
            ctx,
            question=question,
            count=len(orders),
            active_count=len(active_orders),
            by_status=dict(Counter(_status_key(order.ezcater_status_key) for order in orders)),
            orders=[_safe_order(order) for order in _sort_orders(orders)[:MAX_SAMPLE_ROWS]],
            truncated=len(orders) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def catering_tracking_missing(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = [
            order for order in _allowed_orders(db, ctx)
            if not order.delivery_tracking_id and _status_key(order.status) not in _COMPLETE_STATUSES
        ]
        return _payload(
            "orders.catering_tracking_missing",
            ctx,
            question=question,
            count=len(orders),
            by_store=dict(Counter(_store_key_for_order(order) for order in orders)),
            orders=[_safe_order(order) for order in _sort_orders(orders)[:MAX_SAMPLE_ROWS]],
            truncated=len(orders) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def catering_uuid_status(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
        with_uuid = [order for order in orders if order.delivery_tracking_id]
        return _payload(
            "orders.catering_uuid_status",
            ctx,
            question=question,
            total_count=len(orders),
            with_tracking_uuid=len(with_uuid),
            missing_tracking_uuid=max(0, len(orders) - len(with_uuid)),
            active_tracking_count=sum(1 for order in with_uuid if _is_tracking_active(order)),
            by_tracking_status=dict(Counter(_status_key(order.ezcater_status_key) for order in with_uuid)),
        )
    finally:
        db.close()


def catering_late_risk(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        today = date.today()
        now_minute = datetime.now().hour * 60 + datetime.now().minute
        risky: list[Order] = []
        for order in _filter_by_range(_allowed_orders(db, ctx), today, today):
            status = _status_key(order.status)
            minute = _order_delivery_minute(order)
            if status not in _COMPLETE_STATUSES and minute is not None and minute < now_minute:
                risky.append(order)
        return _payload(
            "orders.catering_late_risk",
            ctx,
            question=question,
            today=today.isoformat(),
            count=len(risky),
            by_store=dict(Counter(_store_key_for_order(order) for order in risky)),
            orders=[_safe_order(order) for order in _sort_orders(risky)[:MAX_SAMPLE_ROWS]],
            truncated=len(risky) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def catering_order_lookup(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
        order, searched_token = _find_order(orders, question)
        if not order:
            return _payload(
                "orders.catering_order_lookup",
                ctx,
                question=question,
                found=False,
                searched_token=searched_token,
            )
        return _payload(
            "orders.catering_order_lookup",
            ctx,
            question=question,
            found=True,
            searched_token=searched_token,
            order=_safe_order(order),
        )
    finally:
        db.close()


def catering_order_items_safe(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
        order, searched_token = _find_order(orders, question)
        if not order:
            return _payload(
                "orders.catering_order_items_safe",
                ctx,
                question=question,
                found=False,
                searched_token=searched_token,
            )
        items = list(order.items or [])
        return _payload(
            "orders.catering_order_items_safe",
            ctx,
            question=question,
            found=True,
            searched_token=searched_token,
            order=_safe_order(order, include_tracking=False),
            item_count=len(items),
            items=[_safe_item(item) for item in items[:MAX_ITEM_ROWS]],
            truncated=len(items) > MAX_ITEM_ROWS,
        )
    finally:
        db.close()


def catering_item_mix(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
        visible_order_ids = {order.id for order in orders}
        rows = db.query(OrderItem).filter(OrderItem.order_id.in_(list(visible_order_ids))).all() if visible_order_ids else []
        mix: dict[str, dict[str, Any]] = {}
        for row in rows:
            label = (row.item_key or "unmapped_item").strip()
            bucket = mix.setdefault(label, {"label": label, "qty": 0, "order_rows": 0})
            bucket["qty"] += int(row.qty or 1)
            bucket["order_rows"] += 1
        top_items = sorted(mix.values(), key=lambda item: (-int(item["qty"]), str(item["label"])))[:MAX_SAMPLE_ROWS]
        return _payload(
            "orders.catering_item_mix",
            ctx,
            question=question,
            total_item_rows=len(rows),
            top_items=top_items,
            truncated=len(mix) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def catering_fees_summary(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
        details = _details_by_external_id(db, _visible_external_ids(orders))
        commission = sum(int(row.commission_cents or 0) for row in details.values()) / 100
        service = sum(int(row.service_fee_cents or 0) for row in details.values()) / 100
        processing = sum(int(row.processing_fee_cents or 0) for row in details.values()) / 100
        return _payload(
            "orders.catering_fees_summary",
            ctx,
            question=question,
            order_count=len(orders),
            detail_rows=len(details),
            delivery_fee_total=round(sum(_money(order.delivery_fee) for order in orders), 2),
            tip_total=round(sum(_money(order.tip_amount) for order in orders), 2),
            commission_total=round(commission, 2),
            service_fee_total=round(service, 2),
            processing_fee_total=round(processing, 2),
        )
    finally:
        db.close()


def catering_payout_safe_summary(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
        return _payload(
            "orders.catering_payout_safe_summary",
            ctx,
            question=question,
            order_count=len(orders),
            potential_payout_total=round(sum(_money(order.potential_payout) for order in orders), 2),
            paid_payout_total=round(sum(_money(order.paid_payout) for order in orders), 2),
            tip_total=round(sum(_money(order.tip_amount) for order in orders), 2),
            delivery_fee_total=round(sum(_money(order.delivery_fee) for order in orders), 2),
            verified_miles_total=round(sum(_money(order.pay_verified_miles) for order in orders), 2),
        )
    finally:
        db.close()


def catering_pdf_status(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
        external_ids = _visible_external_ids(orders)
        processing_rows = _processing_rows(db, external_ids)
        details = _details_by_external_id(db, external_ids)
        with_pdf_details = [
            row for row in details.values()
            if row.source_pdf_sha256 or row.source_pdf_path
        ]
        return _payload(
            "orders.catering_pdf_status",
            ctx,
            question=question,
            order_count=len(orders),
            processing_rows=len(processing_rows),
            pdf_detail_rows=len(details),
            with_pdf_source=len(with_pdf_details),
            by_processing_status=dict(Counter(_status_key(row.status) for row in processing_rows)),
            parse_error_count=sum(1 for row in details.values() if row.parse_error),
        )
    finally:
        db.close()


def catering_driver_assignment_summary(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
        visible_ids = _visible_external_ids(orders)
        rows = (
            db.query(DriverAssignmentJob)
            .filter(DriverAssignmentJob.order_id.in_(list(visible_ids)))
            .all()
            if visible_ids
            else []
        )
        recent = sorted(rows, key=lambda row: row.updated_at or row.created_at or datetime.min, reverse=True)
        return _payload(
            "orders.catering_driver_assignment_summary",
            ctx,
            question=question,
            job_count=len(rows),
            by_status=dict(Counter(_status_key(row.status) for row in rows)),
            retry_count_total=sum(int(row.retry_count or 0) for row in rows),
            recent_jobs=[
                {
                    "order_id": row.order_id,
                    "status": row.status,
                    "current_driver": row.current_driver,
                    "new_driver": row.new_driver,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in recent[:MAX_SAMPLE_ROWS]
            ],
            truncated=len(rows) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def catering_returning_customers_aggregate(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        orders = _allowed_orders(db, ctx)
        counts = Counter(
            str(order.client or "").strip().casefold()
            for order in orders
            if str(order.client or "").strip()
        )
        repeat_counts = [count for count in counts.values() if count > 1]
        distribution = Counter(str(count) for count in repeat_counts)
        return _payload(
            "orders.catering_returning_customers_aggregate",
            ctx,
            question=question,
            order_count=len(orders),
            customer_key_count=len(counts),
            returning_customer_count=len(repeat_counts),
            returning_order_count=sum(repeat_counts),
            repeat_order_count_distribution=dict(distribution),
        )
    finally:
        db.close()


def in_house_quotes_summary(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        quotes = _allowed_quotes(db, ctx)
        recent = sorted(quotes, key=lambda quote: quote.created_at or datetime.min, reverse=True)
        return _payload(
            "orders.in_house_quotes_summary",
            ctx,
            question=question,
            quote_count=len(quotes),
            by_status=dict(Counter(_status_key(quote.status) for quote in quotes)),
            by_store=dict(Counter(_store_key_for_quote(quote) for quote in quotes)),
            subtotal_total=round(sum(_money(quote.subtotal) for quote in quotes), 2),
            recent_quotes=[_safe_quote(quote) for quote in recent[:MAX_SAMPLE_ROWS]],
            truncated=len(quotes) > MAX_SAMPLE_ROWS,
        )
    finally:
        db.close()


def in_house_quote_lookup(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        quotes = _allowed_quotes(db, ctx)
        token = _lookup_token(question)
        quote: InHouseCateringQuote | None = None
        if token and token.isdigit():
            quote = next((row for row in quotes if row.id == int(token)), None)
        if quote is None and quotes:
            quote = sorted(quotes, key=lambda row: row.created_at or datetime.min)[-1]
        if quote is None:
            return _payload("orders.in_house_quote_lookup", ctx, question=question, found=False)
        item_count = 0
        try:
            items = json.loads(quote.items_json or "[]")
            if isinstance(items, list):
                item_count = len(items)
        except json.JSONDecodeError:
            item_count = 0
        return _payload(
            "orders.in_house_quote_lookup",
            ctx,
            question=question,
            found=True,
            quote={**_safe_quote(quote), "item_count": item_count},
        )
    finally:
        db.close()


def _txt(question: str) -> str:
    return str(question or "").casefold()


def _order_context(question: str) -> bool:
    text = _txt(question)
    if "order of operations" in text:
        return False
    return bool(re.search(r"\b(catering|caterings|ezcater|delivery|deliveries|orders?|quotes?|in[- ]house)\b", text))


def _in_house_context(question: str) -> bool:
    return bool(re.search(r"\b(in[- ]house|quote|quotes)\b", _txt(question)))


def _mentioned_store_keys(question: str) -> set[str]:
    text = _txt(question)
    stores: set[str] = set()
    for alias, store in _STORE_ALIASES.items():
        escaped = re.escape(alias).replace(r"\ ", r"\s+")
        if re.search(rf"\b{escaped}\b", text):
            stores.add(store)
    return stores


def _wants_today_store_comparison(question: str) -> bool:
    text = _txt(question)
    if not _order_context(question) or _in_house_context(question):
        return False
    if "today" not in text:
        return False
    stores = _mentioned_store_keys(question)
    return len(stores) >= 2 or bool(
        re.search(r"\b(vs|versus|compare|comparison)\b", text)
        and re.search(r"\b(store|stores?|location|locations?)\b", text)
    )


def _wants_today(question: str) -> bool:
    text = _txt(question)
    return (
        _order_context(question)
        and not _in_house_context(question)
        and not _wants_today_store_comparison(question)
        and "today" in text
    )


def _wants_tomorrow(question: str) -> bool:
    text = _txt(question)
    return _order_context(question) and not _in_house_context(question) and "tomorrow" in text


def _wants_week(question: str) -> bool:
    text = _txt(question)
    return _order_context(question) and not _in_house_context(question) and bool(re.search(r"\b(this week|week|next 7 days)\b", text))


def _wants_next_30(question: str) -> bool:
    text = _txt(question)
    return _order_context(question) and not _in_house_context(question) and bool(re.search(r"\b(30 days|next month|upcoming month)\b", text))


def _wants_count(question: str) -> bool:
    text = _txt(question)
    if _wants_returning_customers(question) or _wants_today_store_comparison(question):
        return False
    return _order_context(question) and not _in_house_context(question) and bool(re.search(r"\b(how many|count|total)\b", text))


def _wants_by_status(question: str) -> bool:
    return _order_context(question) and not _in_house_context(question) and "status" in _txt(question)


def _wants_by_store(question: str) -> bool:
    text = _txt(question)
    return (
        _order_context(question)
        and not _in_house_context(question)
        and not _wants_today_store_comparison(question)
        and bool(re.search(r"\b(by store|store split|location split|by location)\b", text))
    )


def _wants_needs_driver(question: str) -> bool:
    text = _txt(question)
    return (
        _order_context(question)
        and not _in_house_context(question)
        and bool(re.search(r"\b(needs?(?:\s+a)?\s+driver|driver attention|without driver|missing driver)\b", text))
    )


def _wants_live_tracking(question: str) -> bool:
    text = _txt(question)
    if re.search(r"\b(missing tracking|without tracking|no tracking)\b", text):
        return False
    return (
        _order_context(question)
        and not _in_house_context(question)
        and bool(re.search(r"\b(live tracking|active tracking|tracking links?)\b", text))
    )


def _wants_tracking_missing(question: str) -> bool:
    text = _txt(question)
    return (
        _order_context(question)
        and not _in_house_context(question)
        and bool(re.search(r"\b(missing tracking|without tracking|no tracking)\b", text))
    )


def _wants_uuid_status(question: str) -> bool:
    text = _txt(question)
    return (
        _order_context(question)
        and not _in_house_context(question)
        and bool(re.search(r"\b(uuid|tracking id|tracking status)\b", text))
    )


def _wants_late_risk(question: str) -> bool:
    text = _txt(question)
    return (
        _order_context(question)
        and not _in_house_context(question)
        and bool(re.search(r"\b(late|risk|overdue|behind)\b", text))
    )


def _wants_order_items(question: str) -> bool:
    text = _txt(question)
    return (
        _order_context(question)
        and not _in_house_context(question)
        and _has_explicit_order_reference(question)
        and bool(re.search(r"\b(items?|food|what was on|what is on)\b", text))
        and "mix" not in text
    )


def _wants_order_lookup(question: str) -> bool:
    text = _txt(question)
    real_id = _REAL_EZCATER_ID_RE.search(str(question or ""))
    real_id_with_digit = bool(real_id and re.search(r"\d", real_id.group(1)))
    explicit_lookup = bool(re.search(r"\b(lookup|find|details?|order #|order id|ticket)\b", text))
    return (
        _order_context(question)
        and not _in_house_context(question)
        and bool(explicit_lookup or real_id_with_digit)
    )


def _wants_item_mix(question: str) -> bool:
    text = _txt(question)
    wants_mix = bool(re.search(r"\b(item mix|menu mix|top items?|food mix|what items sell)\b", text))
    wants_aggregate = bool(_ITEM_MIX_AGGREGATE_RE.search(text))
    return (
        not _in_house_context(question)
        and ((_order_context(question) and wants_mix) or wants_aggregate)
    )


def _wants_fees(question: str) -> bool:
    text = _txt(question)
    return (
        _order_context(question)
        and not _in_house_context(question)
        and bool(re.search(r"\b(fee|fees|commission|service fee|processing fee|tips?)\b", text))
    )


def _wants_payout(question: str) -> bool:
    text = _txt(question)
    return (
        _order_context(question)
        and not _in_house_context(question)
        and bool(re.search(r"\b(payout|driver pay|paid payout|potential payout|verified miles)\b", text))
    )


def _wants_pdf_status(question: str) -> bool:
    text = _txt(question)
    return (
        _order_context(question)
        and not _in_house_context(question)
        and bool(re.search(r"\b(pdfs?|parse|processing status|uploaded)\b", text))
    )


def _wants_driver_assignment(question: str) -> bool:
    text = _txt(question)
    return (
        _order_context(question)
        and not _in_house_context(question)
        and bool(re.search(r"\b(driver assignment|assignment jobs?|assignments?|reassignment)\b", text))
    )


def _wants_returning_customers(question: str) -> bool:
    text = _txt(question)
    returning_phrase = bool(re.search(r"\b(?:returning|repeat)(?:\s+\w+){0,4}\s+customers?\b", text))
    return (
        not _in_house_context(question)
        and (
            returning_phrase
            or (_order_context(question) and bool(re.search(r"\bcustomer aggregate\b", text)))
        )
    )


def _wants_in_house_summary(question: str) -> bool:
    text = _txt(question)
    return _in_house_context(question) and bool(re.search(r"\b(summary|count|how many|list|recent|status)\b", text))


def _wants_in_house_lookup(question: str) -> bool:
    text = _txt(question)
    return _in_house_context(question) and bool(re.search(r"\b(lookup|find|show|details?|quote #|quote id)\b", text))


ORDER_TOOL_HANDLERS: dict[str, Callable[[str, dict[str, Any]], dict[str, Any]]] = {
    "orders_store_summary": orders_store_summary,
    "orders_catering_by_status": catering_by_status,
    "orders_catering_by_store": catering_by_store,
    "orders_catering_count": catering_count,
    "orders_catering_driver_assignment_summary": catering_driver_assignment_summary,
    "orders_catering_fees_summary": catering_fees_summary,
    "orders_catering_item_mix": catering_item_mix,
    "orders_catering_late_risk": catering_late_risk,
    "orders_catering_live_tracking": catering_live_tracking,
    "orders_catering_needs_driver": catering_needs_driver,
    "orders_catering_next_30_days": catering_next_30_days,
    "orders_catering_order_items_safe": catering_order_items_safe,
    "orders_catering_order_lookup": catering_order_lookup,
    "orders_catering_payout_safe_summary": catering_payout_safe_summary,
    "orders_catering_pdf_status": catering_pdf_status,
    "orders_catering_returning_customers_aggregate": catering_returning_customers_aggregate,
    "orders_catering_today": catering_today,
    "orders_catering_tomorrow": catering_tomorrow,
    "orders_catering_tracking_missing": catering_tracking_missing,
    "orders_catering_uuid_status": catering_uuid_status,
    "orders_catering_week": catering_week,
    "orders_in_house_quote_lookup": in_house_quote_lookup,
    "orders_in_house_quotes_summary": in_house_quotes_summary,
}


ORDER_TOOL_MATCHERS: dict[str, Callable[[str], bool]] = {
    "orders_catering_by_status": _wants_by_status,
    "orders_catering_by_store": _wants_by_store,
    "orders_catering_count": _wants_count,
    "orders_catering_driver_assignment_summary": _wants_driver_assignment,
    "orders_catering_fees_summary": _wants_fees,
    "orders_catering_item_mix": _wants_item_mix,
    "orders_catering_late_risk": _wants_late_risk,
    "orders_catering_live_tracking": _wants_live_tracking,
    "orders_catering_needs_driver": _wants_needs_driver,
    "orders_catering_next_30_days": _wants_next_30,
    "orders_catering_order_items_safe": _wants_order_items,
    "orders_catering_order_lookup": _wants_order_lookup,
    "orders_catering_payout_safe_summary": _wants_payout,
    "orders_catering_pdf_status": _wants_pdf_status,
    "orders_catering_returning_customers_aggregate": _wants_returning_customers,
    "orders_catering_today": _wants_today,
    "orders_catering_tomorrow": _wants_tomorrow,
    "orders_catering_tracking_missing": _wants_tracking_missing,
    "orders_catering_uuid_status": _wants_uuid_status,
    "orders_catering_week": _wants_week,
    "orders_in_house_quote_lookup": _wants_in_house_lookup,
    "orders_in_house_quotes_summary": _wants_in_house_summary,
}
