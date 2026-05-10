"""Per-location ezCater revenue aggregation for /reports/sales + labor.

ezCater orders never touch Toast, so we maintain our own revenue feed off
the Order table (populated by the webhook pipeline). The total_amount on
each Order row is precomputed at ingest time from the unit prices baked
into OrderItem.raw_alias (see ezcater_pricing.compute_order_total).

Used by:
- toast_reports.third_party_sales_report — adds an "ezCater" channel
- toast_reports.labor_report — adds ezCater revenue to the % denominator
"""
from __future__ import annotations

from datetime import date
from typing import Iterable

from app.db import SessionLocal
from app.models import Order

# origin_store_id (Toast-style "store_N") -> friendly location key used by the reports
_STORE_TO_LOCATION = {
    "store_1": "copperfield",
    "store_3": "copperfield",
    "store_2": "tomball",
    "store_4": "tomball",
}


def fetch_ezcater_orders(start: date, end: date,
                         location_filter: str | None = None) -> list[dict]:
    """Return ezCater orders delivered in [start, end] (inclusive).

    Each entry: {date: 'YYYY-MM-DD', location: 'tomball'|'copperfield',
                 amount: float, external_order_id: str}.
    Skips cancelled orders, orders missing total_amount, and orders whose
    origin_store_id can't be mapped to a known location.
    """
    db = SessionLocal()
    try:
        rows = (db.query(Order)
                .filter(Order.external_order_id.isnot(None))
                .filter(Order.delivery_date.isnot(None))
                .filter(Order.total_amount.isnot(None))
                .filter(Order.status != "cancelled")
                .all())
    finally:
        db.close()
    out = []
    for o in rows:
        try:
            dt = date.fromisoformat(o.delivery_date)
        except (TypeError, ValueError):
            continue
        if not (start <= dt <= end):
            continue
        loc = _STORE_TO_LOCATION.get(o.origin_store_id)
        if loc is None:
            continue
        if location_filter and location_filter != "both" and loc != location_filter:
            continue
        out.append({
            "date": dt.isoformat(),
            "location": loc,
            "amount": float(o.total_amount or 0.0),
            "external_order_id": o.external_order_id,
        })
    return out


def total_ezcater_revenue(start: date, end: date,
                          location_filter: str | None = None) -> float:
    """Convenience: just the dollar total. Used by labor_report's denominator."""
    return round(sum(r["amount"] for r in fetch_ezcater_orders(start, end, location_filter)), 2)
