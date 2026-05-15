"""One-way miles from kitchen (Copperfield / Tomball) to delivery address.

Backs the payroll bonus rule Sam defined 2026-05-10: drivers earn an
extra $1.50 per mile over 20 from the pickup kitchen to the first drop-off
(only for tracked deliveries). Uses Google Routes API computeRouteMatrix —
same endpoint as scripts/ezcater_distance.py but pulled in-process so we
can batch-update orders without shelling out.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

from app.db import SessionLocal
from app.models import Order

logger = logging.getLogger(__name__)

KITCHEN_ADDRESSES = {
    "copperfield": "15650 FM 529, Houston, TX 77095",
    "tomball":     "27727 Tomball Pkwy, Tomball, TX 77375",
}

_ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
_FIELD_MASK = "originIndex,destinationIndex,distanceMeters,duration,status,condition"
_SECRETS_FILE = Path(r"C:\Users\sam\.openclaw\.secrets\google_api_key.txt")
_METERS_PER_MILE = 1609.344


def _get_api_key() -> str | None:
    val = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if val:
        return val
    if _SECRETS_FILE.exists():
        return _SECRETS_FILE.read_text(encoding="utf-8").strip()
    return os.environ.get("GOOGLE_API_KEY", "").strip() or None


# Sam's policy (permanent, 2026-05-15): we ALWAYS compute pickup_miles
# ourselves via Google Routes against KITCHEN_ADDRESSES. NEVER trust the
# miles field ezCater sends in webhooks or XLSX imports. ezCater computes
# miles from the storefront-of-record (ghost address for store_3/store_4),
# which is wrong for our physical-kitchen-collapsed routing model and
# would mislead driver pay calculation.
#
# Future maintainers: do NOT add an ezCater-miles fallback here. If a new
# ingest path ever passes a miles value, discard it. Only the call below
# is the canonical source of pickup_miles. See samai #1488 for context.


def compute_one_way_miles(pickup_kitchen: str, drop_off_address: str) -> float | None:
    """Return one-way driving miles from the pickup kitchen to the drop-off,
    or None if we can't resolve (missing API key / Google error / malformed
    address). Single API call per invocation."""
    origin = KITCHEN_ADDRESSES.get((pickup_kitchen or "").lower())
    if not origin or not drop_off_address:
        return None
    api_key = _get_api_key()
    if not api_key:
        logger.warning("compute_one_way_miles: no GOOGLE_MAPS_API_KEY configured")
        return None
    body = {
        "origins": [{"waypoint": {"address": origin}}],
        "destinations": [{"waypoint": {"address": drop_off_address}}],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
    }
    req = urllib.request.Request(
        _ROUTES_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": _FIELD_MASK,
            "User-Agent": "CenasKitchen-payroll/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        logger.warning("Routes API HTTP %s for %r -> %r", e.code, origin[:40], drop_off_address[:60])
        return None
    except Exception:
        logger.exception("Routes API call failed")
        return None
    # Response is a list with one entry (1 origin × 1 destination).
    rows = resp if isinstance(resp, list) else [resp]
    for row in rows:
        if row.get("condition") != "ROUTE_EXISTS":
            continue
        meters = row.get("distanceMeters")
        if meters is None:
            continue
        return round(meters / _METERS_PER_MILE, 2)
    return None


def backfill_pending(limit: int = 50) -> dict:
    """Find Orders that have a pickup_kitchen + delivery_address but no
    pickup_miles yet, call Routes API for each, and store the result. Capped
    at `limit` per call so a single click doesn't burn through a huge
    historical backlog in one shot (Google has per-second rate limits and
    each call costs ~$0.005). Returns {processed, updated, skipped, failed}."""
    db = SessionLocal()
    try:
        pending = (db.query(Order)
                   .filter(Order.pickup_miles.is_(None))
                   .filter(Order.pickup_kitchen.isnot(None))
                   .filter(Order.delivery_address.isnot(None))
                   .limit(limit)
                   .all())
        updated = 0
        skipped = 0
        failed = 0
        for o in pending:
            miles = compute_one_way_miles(o.pickup_kitchen, o.delivery_address)
            if miles is None:
                failed += 1
                continue
            o.pickup_miles = miles
            updated += 1
        db.commit()
        return {
            "processed": len(pending),
            "updated": updated,
            "failed": failed,
            "skipped": skipped,
        }
    finally:
        db.close()
