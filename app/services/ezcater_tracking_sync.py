"""ezCater tracking URL sync helpers.

This is the shared app-side receiver logic for two sources:
- the partner bookmarklet/manual sync page
- CK/pwck read-only collector posts

It intentionally only writes the ezCater tracking UUID/status cache fields on
Order. It does not assign drivers, fulfill orders, notify anyone, or change
driver payroll tracking_status.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Order
from app.services.ezcater_live_tracker import extract_tracking_uuid, poll_one


SessionFactory = Callable[[], Session]


_STARTED_STATUS_KEYS = {
    "driver_en_route_to_pickup",
    "driver_arrived_at_pickup",
    "driver_en_route_to_dropoff",
    "driver_arrived_at_dropoff",
    "delivered",
}


def _order_candidates(order_number: str) -> list[str]:
    raw = (order_number or "").strip().upper()
    if not raw:
        return []
    candidates = [raw]
    no_dash = raw.replace("-", "")
    if no_dash != raw:
        candidates.append(no_dash)
    if "-" not in raw and len(raw) >= 4:
        candidates.append(f"{raw[:3]}-{raw[3:]}")
    out: list[str] = []
    for value in candidates:
        if value and value not in out:
            out.append(value)
    return out


def _find_order(db: Session, order_number: str) -> Order | None:
    candidates = _order_candidates(order_number)
    if not candidates:
        return None
    upper_candidates = [c.upper() for c in candidates]
    return (
        db.query(Order)
        .filter(func.upper(Order.external_order_id).in_(upper_candidates))
        .order_by(Order.id.desc())
        .first()
    )


def tracking_body_started(body: dict[str, Any] | None) -> bool:
    """Return True when ezCater's tracking body shows a driver has started."""
    data = (body or {}).get("data") or {}
    drivers = data.get("drivers") or []
    if drivers:
        return True
    status = data.get("status")
    if status in _STARTED_STATUS_KEYS:
        return True
    return False


def _order_started(order: Order, body: dict[str, Any] | None) -> bool:
    if tracking_body_started(body):
        return True
    if order.ezcater_driver_lat is not None and order.ezcater_driver_lng is not None:
        return True
    return (order.ezcater_status_key or "") in _STARTED_STATUS_KEYS


def sync_tracking_updates(
    updates: Iterable[dict[str, Any]],
    *,
    session_factory: SessionFactory | None = None,
    poll: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """Save ezCater tracking URLs/UUIDs against existing Order rows.

    Input rows need only:
      {"order_number": "ABC-123", "tracking_url": "https://.../<uuid>"}

    Output avoids raw URLs and customer/order PII. Order numbers and short UUID
    prefixes are kept so pwck/ops can reconcile live tests.
    """
    rows = list(updates or [])[:limit]
    result: dict[str, Any] = {
        "received": len(rows),
        "saved": 0,
        "new": 0,
        "changed": 0,
        "unchanged": 0,
        "polled": 0,
        "started": 0,
        "orders": [],
        "skipped": [],
        "not_found": [],
    }

    if session_factory is None:
        from app.db import SessionLocal
        session_factory = SessionLocal
    if session_factory is None:
        raise RuntimeError("database session is not configured")

    db = session_factory()
    try:
        for row in rows:
            order_number = (row.get("order_number") or "").strip().upper()
            url = (row.get("tracking_url") or row.get("url") or row.get("uuid") or "").strip()
            uuid_ = extract_tracking_uuid(url)
            if not order_number or not uuid_:
                result["skipped"].append({
                    "order_number": order_number,
                    "reason": "missing_order_number_or_tracking_uuid",
                })
                continue

            order = _find_order(db, order_number)
            if not order:
                result["not_found"].append(order_number)
                continue

            before = order.delivery_tracking_id
            if before == uuid_:
                result["unchanged"] += 1
            else:
                if before:
                    result["changed"] += 1
                else:
                    result["new"] += 1
                order.delivery_tracking_id = uuid_

            body = poll_one(order) if poll else None
            if body:
                result["polled"] += 1
            started = _order_started(order, body)
            if started:
                result["started"] += 1
            result["saved"] += 1
            result["orders"].append({
                "order_number": order.external_order_id or order_number,
                "uuid_prefix": uuid_[:8],
                "status_key": order.ezcater_status_key,
                "started": started,
                "polled": bool(body),
            })

        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
