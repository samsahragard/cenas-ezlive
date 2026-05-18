"""One-shot read for Sam: list upcoming orders + their assigned drivers.

Returns a JSON-friendly dict with one entry per upcoming order (today + future),
showing client, delivery datetime, location, the internal assigned driver
(if any), and the ezCater-assigned courier name (if any).

Surfaced via /sam/cena/run-list-upcoming-orders trigger endpoint.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import SessionLocal  # noqa: E402
from app.models import Driver, Order  # noqa: E402


LOCATION_LABEL = {"store_1": "Copperfield", "store_2": "Tomball"}


def main() -> int:
    """When CENA_INCLUDE_RECENT=1 env var is set, also include the last 30
    days of completed/delivered orders. Lets Sam check if ezcater_driver_name
    is populated for past orders (webhook health) vs only missing for upcoming
    ones (ezCater hasn't assigned yet)."""
    import os
    include_recent = os.getenv("CENA_INCLUDE_RECENT") == "1"
    today_iso = datetime.now().strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        q = db.query(Order).filter(Order.status != "cancelled")
        if include_recent:
            from datetime import timedelta
            since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            q = q.filter(Order.delivery_date >= since)
        else:
            q = q.filter(Order.delivery_date >= today_iso)
        orders = q.order_by(Order.delivery_date.asc(), Order.deliver_at.asc()).all()

        driver_by_id = {d.id: d.name for d in db.query(Driver).all()}

        rows = []
        for o in orders:
            internal_driver = driver_by_id.get(o.assigned_driver_id) if o.assigned_driver_id else None
            rows.append({
                "order_id": o.external_order_id,
                "delivery_date": o.delivery_date,
                "deliver_at": o.deliver_at,
                "location": LOCATION_LABEL.get(o.origin_store_id) or o.origin_store_id,
                "client": o.client,
                "internal_driver": internal_driver,
                "internal_driver_legacy_string": o.assigned_driver,
                "ezcater_driver": o.ezcater_driver_name,
                "status": o.status,
            })

        payload = {
            "count": len(rows),
            "as_of": datetime.utcnow().isoformat() + "Z",
            "orders": rows,
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
