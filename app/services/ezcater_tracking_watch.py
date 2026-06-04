"""Isolated ezCater customer-tracker watchlist.

This service deliberately does not read or write Order rows. It stores a
small partner-only watchlist of customer tracking URLs, polls ezCater's public
delivery-tracking refresh endpoint, and returns manager-facing map/status data.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services.ezcater_live_tracker import extract_tracking_uuid, fetch_state


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


def list_watch() -> dict[str, Any]:
    state = _load_state()
    orders = [_public_order(o) for o in state.get("orders", [])]
    orders.sort(key=lambda r: (r.get("deliver_at") or "", r.get("order_number") or ""))
    return {"orders": orders, "events": state.get("events", [])[-40:]}


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
