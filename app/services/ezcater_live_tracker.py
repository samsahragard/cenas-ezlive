"""ezCater live-tracking poller.

Source endpoint: https://delivery-management.ezcater.com/delivery_tracking/v1/...
Public, no auth — just a User-Agent header. Two flavors:
  /delivery/<tracking_uuid>          -> full state (driver, addrs, ETA, alerts)
  /delivery_refresh/<tracking_uuid>  -> light poll (driver location + status + ETA)

The tracking UUID is distinct from our internal external_delivery_id; it's
the value embedded in the customer-facing tracker URL ezCater emails
("https://delivery-tracking.ezcater.com/delivery/<uuid>"). Per Sam's
2026-05-11 note, this UUID only becomes useful after the driver hits
"start" in their ezCater driver app, so an order can have a tracking_id
in our DB but the API returns {"status": "expired"} until tracking begins.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import object_session

from app.db import SessionLocal
from app.models import Order

logger = logging.getLogger(__name__)

_BASE = "https://delivery-management.ezcater.com/delivery_tracking/v1"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/148.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}

# UUID regex (8-4-4-4-12 hex). The tracker URL pattern is:
#   https://delivery-tracking.ezcater.com/delivery/<uuid>
# We accept either a full URL or a bare UUID on the input.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def extract_tracking_uuid(text: str | None) -> str | None:
    if not text:
        return None
    m = _UUID_RE.search(text)
    return m.group(0).lower() if m else None


def fetch_state(tracking_uuid: str, refresh_only: bool = True) -> dict | None:
    """Returns the parsed JSON body, or None on transport error. Soft on
    HTTP errors — they just return None so callers can keep going."""
    if not tracking_uuid:
        return None
    endpoint = "delivery_refresh" if refresh_only else "delivery"
    url = f"{_BASE}/{endpoint}/{tracking_uuid}"
    req = urllib.request.Request(url, headers=_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        logger.warning("ezcater live %s HTTP %s for %s", endpoint, e.code, tracking_uuid[:8])
        return None
    except Exception:
        logger.exception("ezcater live %s failed for %s", endpoint, tracking_uuid[:8])
        return None


def poll_one(order: Order) -> dict | None:
    """Hit the refresh endpoint, update Order with driver lat/lng + status
    key + last-updated timestamp. Returns the raw body so callers can show
    extra detail; or None if there's nothing to poll."""
    if not order.delivery_tracking_id:
        return None
    body = fetch_state(order.delivery_tracking_id, refresh_only=True)
    if not body:
        return None
    data = (body or {}).get("data") or {}
    drivers = data.get("drivers") or []
    if drivers:
        d0 = drivers[0]
        loc = d0.get("currentLocation") or {}
        order.ezcater_driver_lat = loc.get("latitude")
        order.ezcater_driver_lng = loc.get("longitude")
        order.ezcater_status_key = (d0.get("currentStatus") or {}).get("key")
        if d0.get("name") and not order.ezcater_driver_name:
            order.ezcater_driver_name = str(d0.get("name"))
    elif data.get("status") in ("expired", "completed"):
        order.ezcater_status_key = data["status"]
    order.ezcater_status_updated_at = datetime.utcnow()
    try:
        db = object_session(order)
        if db is not None:
            from app.services.ezcater_route_history import record_tracking_sample
            record_tracking_sample(db, order, body)
    except Exception:
        logger.exception("ezcater route-history capture failed for order_id=%s", getattr(order, "id", None))
    return body


def poll_active(limit: int = 25) -> dict:
    """Poll every Order with a non-null delivery_tracking_id whose latest
    status isn't 'expired' / 'completed' / 'delivered'. Capped at `limit`
    per call so a click doesn't fan out unbounded calls to ezCater."""
    db = SessionLocal()
    try:
        q = (db.query(Order)
               .filter(Order.delivery_tracking_id.isnot(None))
               .filter((Order.ezcater_status_key.is_(None)) |
                       (~Order.ezcater_status_key.in_(("expired", "completed", "delivered"))))
               .limit(limit))
        rows = q.all()
        updated, no_data = 0, 0
        for o in rows:
            body = poll_one(o)
            if body:
                updated += 1
            else:
                no_data += 1
        db.commit()
        return {"polled": len(rows), "updated": updated, "no_data": no_data}
    finally:
        db.close()
