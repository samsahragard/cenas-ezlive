"""Read-only Cenas AI handlers for driver summary questions."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.db import SessionLocal
from app.models import Driver, DriverShift, Order
from app.services.assistant_handlers.orders import _normalize_store, _tool_store_filter


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def drivers_store_summary(question_or_ctx: str | dict[str, Any], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    if ctx is None and isinstance(question_or_ctx, dict):
        ctx = question_or_ctx
        question = ""
    else:
        question = str(question_or_ctx or "")
        ctx = ctx or {}
    db = SessionLocal()
    try:
        drivers = db.query(Driver).all()
        allowed = _tool_store_filter(ctx)
        if allowed:
            drivers = [
                driver for driver in drivers
                if _normalize_store(driver.home_store_id or driver.location or "") in allowed
            ]
        active = [
            driver for driver in drivers
            if driver.active and (driver.status or "active").casefold() == "active"
        ]
        by_store: dict[str, int] = {}
        score_count = 0
        score_total = 0
        for driver in drivers:
            store = _normalize_store(driver.home_store_id or driver.location or "unknown")
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
            "ok": True,
            "tool_id": "drivers.store_summary",
            "generated_at": _now_iso(),
            "data_class": "driver_aggregate_sanitized",
            "question": question,
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
