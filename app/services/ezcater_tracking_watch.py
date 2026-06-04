"""Isolated ezCater customer-tracker watchlist.

This service deliberately does not write Order rows. It reads upcoming orders
so the partner page can show an order board, stores a separate watchlist of
customer tracking URLs, polls ezCater's public delivery-tracking refresh
endpoint, and returns manager-facing map/status data.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_

from app.db import SessionLocal
from app.models import Order
from app.services.ezcater_live_tracker import extract_tracking_uuid, fetch_state

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utcnow()).isoformat()


def _state_path() -> Path:
    configured = os.getenv("EZCATER_TRACKING_WATCH_FILE")
    if configured:
        return Path(configured)
    base = Path("/var/data")
    if not base.exists():
        base = Path(os.getenv("TMPDIR") or "/tmp")
    return base / "ezcater_tracking_watch.json"


def _blank_state() -> dict[str, Any]:
    return {"orders": [], "events": []}


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return _blank_state()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _blank_state()
    if not isinstance(loaded, dict):
        return _blank_state()
    loaded.setdefault("orders", [])
    loaded.setdefault("events", [])
    return loaded


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _event(state: dict[str, Any], name: str, order_number: str, **extra: Any) -> None:
    events = state.setdefault("events", [])
    row = {"at": _iso(), "event": name, "order_number": order_number}
    row.update(extra)
    events.append(row)
    state["events"] = events[-100:]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _risk_for(order: dict[str, Any]) -> dict[str, Any]:
    if order.get("started_at") or order.get("lat") or order.get("lng"):
        return {"level": "ok", "text": "tracking started"}
    deliver_at = _parse_dt(order.get("deliver_at"))
    if not deliver_at:
        return {"level": "watch", "text": "waiting for tracking"}
    drive_minutes = int(order.get("drive_minutes") or 35)
    buffer_minutes = int(order.get("buffer_minutes") or 10)
    leave_by = deliver_at - timedelta(minutes=drive_minutes + buffer_minutes)
    now = _utcnow()
    if now >= leave_by:
        mins_left = int((deliver_at - now).total_seconds() // 60)
        return {
            "level": "danger",
            "text": f"manager alert: tracking has not started; late risk ({mins_left} min to due)",
            "leave_by_at": leave_by.isoformat(),
        }
    mins_to_leave = int((leave_by - now).total_seconds() // 60)
    return {
        "level": "watch",
        "text": f"tracking not started; leave-by in {mins_to_leave} min",
        "leave_by_at": leave_by.isoformat(),
    }


def _public_order(order: dict[str, Any]) -> dict[str, Any]:
    out = dict(order)
    status = order.get("status_key") or order.get("raw_status")
    out["status_label"] = status or "waiting"
    out["risk"] = _risk_for(order)
    return out


def _order_key(value: str | None) -> str:
    return (value or "").strip().upper()


def _watch_by_order_number(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in state.get("orders", []):
        key = _order_key(row.get("order_number"))
        if key:
            out[key] = row
    return out


def _delivery_due_at(order: Order) -> str | None:
    if order.delivery_window_start:
        return order.delivery_window_start.isoformat()
    return order.deliver_at or None


def _store_label(order: Order) -> str:
    pickup = (order.pickup_kitchen or "").strip().lower()
    origin = (order.origin_store_id or order.reported_store_id or "").strip().lower()
    reported = (order.reported_store or "").strip()
    if pickup == "tomball" or origin in {"store_2", "store_4"}:
        return "Tomball"
    if pickup == "copperfield" or origin in {"store_1", "store_3"}:
        return "Copperfield"
    return reported or "Cenas"


def _watch_like_from_order(order: Order, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
    uuid_ = order.delivery_tracking_id
    if not uuid_:
        return None
    out: dict[str, Any] = {
        "uuid": uuid_,
        "order_number": order.external_order_id or f"order-{order.id}",
        "tracking_url": f"https://delivery-tracking.ezcater.com/delivery/{uuid_}",
        "client": order.client,
        "store_label": _store_label(order),
        "deliver_at": _delivery_due_at(order),
        "driver_name": order.ezcater_driver_name,
        "raw_status": order.ezcater_status_key,
        "status_key": order.ezcater_status_key,
        "lat": order.ezcater_driver_lat,
        "lng": order.ezcater_driver_lng,
        "last_polled_at": order.ezcater_status_updated_at.isoformat() if order.ezcater_status_updated_at else None,
    }
    data = (body or {}).get("data") or {}
    if data:
        out["raw_status"] = data.get("status") or out.get("raw_status")
        drivers = data.get("drivers") or []
        if drivers:
            d0 = drivers[0]
            current = d0.get("currentStatus") or {}
            loc = d0.get("currentLocation") or {}
            out["driver_name"] = d0.get("name") or out.get("driver_name")
            out["status_key"] = current.get("key") or out.get("status_key")
            if loc.get("latitude") is not None and loc.get("longitude") is not None:
                out["lat"] = loc.get("latitude")
                out["lng"] = loc.get("longitude")
                out["location_updated_at"] = _iso()
        out["last_polled_at"] = _iso()
    return out


def _public_app_order(
    order: Order,
    watch_row: dict[str, Any] | None = None,
    live_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = {
        "order_number": order.external_order_id or f"order-{order.id}",
        "client": order.client,
        "store_label": _store_label(order),
        "delivery_date": order.delivery_date,
        "deliver_at": _delivery_due_at(order),
        "deliver_at_label": order.deliver_at or "",
        "headcount": order.headcount,
        "status": order.status,
        "assigned_driver": order.assigned_driver,
        "ezcater_driver_name": order.ezcater_driver_name,
        "has_tracking": bool(watch_row or order.delivery_tracking_id),
    }
    if order.delivery_tracking_id and not watch_row:
        out["stored_tracking_url"] = f"https://delivery-tracking.ezcater.com/delivery/{order.delivery_tracking_id}"
    if watch_row:
        out["tracker"] = _public_order(watch_row)
    elif order.delivery_tracking_id:
        order_tracker = _watch_like_from_order(order, live_body)
        if order_tracker:
            out["tracker"] = _public_order(order_tracker)
    return out


def list_watch() -> dict[str, Any]:
    state = _load_state()
    orders = [_public_order(o) for o in state.get("orders", [])]
    orders.sort(key=lambda r: (r.get("deliver_at") or "", r.get("order_number") or ""))
    return {"orders": orders, "events": state.get("events", [])[-40:]}


def list_app_orders(days: int = 14, *, live_poll: bool = False, poll_limit: int = 25) -> list[dict[str, Any]]:
    """Read upcoming Cenas orders without mutating the catering workflow."""
    if SessionLocal is None:
        return []
    today_iso = _utcnow().astimezone().strftime("%Y-%m-%d")
    cutoff_iso = (_utcnow().astimezone() + timedelta(days=days)).strftime("%Y-%m-%d")
    state = _load_state()
    watch_by_number = _watch_by_order_number(state)
    try:
        db = SessionLocal()
    except Exception:
        logger.exception("ezcater tracking watch could not open DB session")
        return []
    try:
        rows = (
            db.query(Order)
            .filter(Order.delivery_date >= today_iso)
            .filter(Order.delivery_date <= cutoff_iso)
            .filter(or_(Order.status.is_(None), Order.status != "cancelled"))
            .order_by(Order.delivery_date.asc(), Order.deliver_at.asc())
            .limit(100)
            .all()
        )
        live_bodies: dict[str, dict[str, Any] | None] = {}
        if live_poll:
            for o in rows:
                if len(live_bodies) >= poll_limit:
                    break
                if not o.delivery_tracking_id:
                    continue
                try:
                    live_bodies[o.delivery_tracking_id] = fetch_state(o.delivery_tracking_id, refresh_only=True)
                except Exception:
                    logger.exception("ezcater tracking watch live poll failed for %s", o.external_order_id)
                    live_bodies[o.delivery_tracking_id] = None
        return [
            _public_app_order(
                o,
                watch_by_number.get(_order_key(o.external_order_id)),
                live_bodies.get(o.delivery_tracking_id or ""),
            )
            for o in rows
        ]
    finally:
        db.close()


def save_tracker(payload: dict[str, Any]) -> dict[str, Any]:
    raw_url = (payload.get("tracking_url") or payload.get("url") or "").strip()
    uuid_ = extract_tracking_uuid(raw_url or payload.get("uuid"))
    if not uuid_:
        raise ValueError("Paste a full ezCater tracking URL or UUID.")
    order_number = (payload.get("order_number") or f"LIVE-{uuid_[:8]}").strip()
    state = _load_state()
    rows = state.setdefault("orders", [])
    existing = next((r for r in rows if r.get("uuid") == uuid_), None)
    now = _iso()
    if existing is None:
        existing = {
            "uuid": uuid_,
            "order_number": order_number,
            "tracking_url": raw_url or f"https://delivery-tracking.ezcater.com/delivery/{uuid_}",
            "saved_at": now,
        }
        rows.append(existing)
        _event(state, "tracker_saved", order_number)
    existing.update({
        "order_number": order_number,
        "tracking_url": raw_url or existing.get("tracking_url"),
        "client": payload.get("client") or existing.get("client"),
        "store_label": payload.get("store_label") or existing.get("store_label"),
        "deliver_at": payload.get("deliver_at") or existing.get("deliver_at"),
        "drive_minutes": int(payload.get("drive_minutes") or existing.get("drive_minutes") or 35),
        "buffer_minutes": int(payload.get("buffer_minutes") or existing.get("buffer_minutes") or 10),
        "updated_at": now,
    })
    _save_state(state)
    return _public_order(existing)


def import_text(text: str, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    defaults = defaults or {}
    uuids: list[str] = []
    for part in text.replace("\r", " ").replace("\n", " ").split():
        uuid_ = extract_tracking_uuid(part)
        if uuid_ and uuid_ not in uuids:
            uuids.append(uuid_)
    saved = []
    for uuid_ in uuids:
        saved.append(save_tracker({
            **defaults,
            "tracking_url": f"https://delivery-tracking.ezcater.com/delivery/{uuid_}",
            "order_number": defaults.get("order_number") or f"LIVE-{uuid_[:8].upper()}",
        }))
    return {"saved": len(saved), "orders": saved}


def delete_tracker(uuid_: str) -> bool:
    state = _load_state()
    rows = state.setdefault("orders", [])
    before = len(rows)
    kept = [r for r in rows if r.get("uuid") != uuid_]
    state["orders"] = kept
    if len(kept) != before:
        _event(state, "tracker_removed", uuid_)
        _save_state(state)
        return True
    return False


def poll_all() -> dict[str, Any]:
    state = _load_state()
    polled = updated = 0
    started: list[str] = []
    for order in state.get("orders", []):
        risk = _risk_for(order)
        if risk.get("level") == "danger" and not order.get("manager_alerted_at"):
            order["manager_alerted_at"] = _iso()
            _event(state, "manager_late_risk", order.get("order_number") or order.get("uuid"), risk=risk.get("text"))
        uuid_ = order.get("uuid")
        if not uuid_:
            continue
        polled += 1
        body = fetch_state(uuid_, refresh_only=True)
        order["last_polled_at"] = _iso()
        if not body:
            order["last_poll_result"] = "no_response"
            continue
        data = (body or {}).get("data") or {}
        order["raw_status"] = data.get("status") or order.get("raw_status")
        drivers = data.get("drivers") or []
        if drivers:
            d0 = drivers[0]
            current = d0.get("currentStatus") or {}
            loc = d0.get("currentLocation") or {}
            order["driver_name"] = d0.get("name") or order.get("driver_name")
            order["status_key"] = current.get("key") or order.get("status_key")
            if loc.get("latitude") is not None and loc.get("longitude") is not None:
                order["lat"] = loc.get("latitude")
                order["lng"] = loc.get("longitude")
                order["location_updated_at"] = _iso()
            if not order.get("started_at"):
                order["started_at"] = _iso()
                started.append(order.get("order_number") or uuid_)
                _event(state, "driver_started_tracking", order.get("order_number") or uuid_, status=order.get("status_key"))
            order["last_poll_result"] = order.get("status_key") or "live"
            updated += 1
        elif data.get("status"):
            order["status_key"] = data.get("status")
            order["last_poll_result"] = data.get("status")
            updated += 1
    _save_state(state)
    return {"polled": polled, "updated": updated, "started": started}
