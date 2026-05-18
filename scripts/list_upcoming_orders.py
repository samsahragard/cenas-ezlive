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
    today_iso = datetime.now().strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        orders = (db.query(Order)
            .filter(Order.delivery_date >= today_iso)
            .filter(Order.status != "cancelled")
            .order_by(Order.delivery_date.asc(), Order.deliver_at.asc())
            .all())

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
