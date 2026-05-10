"""Parse + ingest the ezCater Caterer Portal "Order Data" report (xlsx).

Sam exports this from the ezCater portal whenever he wants to backfill orders
we either never received via webhook (pre-pipeline-cutover) or whose totals
got computed from raw_alias rather than the canonical "Food Total" column.

Sheet layout (as of 2026-05):
    Sheet 'Order Data', columns include:
        Order Number, Location, Street Address, City, State, Zip Code,
        Store Number, Store Name, Caterer Name, Event Date, Submitted At,
        Food Total, Promotion, Delivery Fee, Commission, Sales Tax,
        Sales Tax Remitted by ezCater, Tip, Payment Transaction Fee,
        Adjustments, Discounts, Misc Fees, Preferred Partner Program,
        Rewards, Caterer Total Due, Status, Order Paid By Caterer Payment
        Method, Source, Promotion Code, Driver, House Account ID, ...

We extract Order Number + Food Total (canonical food-only revenue, comparable
to Toast's check.amount) + Store Number + Event Date. Caterer Total Due is
also captured for reference but not used in reports yet.
"""
from __future__ import annotations

import io
import logging
from datetime import date, datetime
from typing import IO

import openpyxl

from app.db import SessionLocal
from app.models import Order

logger = logging.getLogger(__name__)


# Store Number from xlsx -> internal store_id used by the rest of the codebase
_STORE_NUM_TO_ID = {1: "store_1", 2: "store_2", 3: "store_3", 4: "store_4"}
# store_id -> location key
_STORE_TO_LOCATION = {
    "store_1": "copperfield", "store_3": "copperfield",
    "store_2": "tomball",     "store_4": "tomball",
}


def _coerce_date(v) -> str | None:
    """The xlsx ships Event Date as a datetime when openpyxl decodes it.
    Normalize to ISO yyyy-mm-dd string (matches Order.delivery_date format)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    if not s:
        return None
    # Try a few common formats just in case
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_export_xlsx(stream: IO[bytes]) -> list[dict]:
    """Return one dict per row in the 'Order Data' sheet."""
    wb = openpyxl.load_workbook(stream, data_only=True)
    sheet_name = next((s for s in wb.sheetnames if s.lower().strip() == "order data"),
                      wb.sheetnames[0])
    ws = wb[sheet_name]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    idx = {name: header.index(name) for name in header if name}
    out = []
    required = ("Order Number", "Food Total", "Store Number")
    for col in required:
        if col not in idx:
            raise ValueError(f"Required column missing from xlsx: {col!r}. Found: {list(idx)[:8]}")
    for raw in rows[1:]:
        if raw is None:
            continue
        order_number = raw[idx["Order Number"]]
        if not order_number:
            continue
        food_total = raw[idx["Food Total"]]
        if food_total is None:
            continue
        store_num_v = raw[idx["Store Number"]]
        try:
            store_num = int(store_num_v) if store_num_v is not None else None
        except (TypeError, ValueError):
            store_num = None
        store_id = _STORE_NUM_TO_ID.get(store_num)
        location = _STORE_TO_LOCATION.get(store_id) if store_id else None
        rec = {
            "order_number": str(order_number).strip(),
            "food_total": float(food_total),
            "caterer_total_due": float(raw[idx["Caterer Total Due"]] or 0)
                if "Caterer Total Due" in idx else None,
            "store_number": store_num,
            "store_id": store_id,
            "location": location,
            "event_date": _coerce_date(raw[idx["Event Date"]]) if "Event Date" in idx else None,
            "submitted_at": (raw[idx["Submitted At"]].isoformat()
                             if "Submitted At" in idx and isinstance(raw[idx["Submitted At"]], datetime)
                             else None),
            "status": str(raw[idx["Status"]] or "").strip() if "Status" in idx else "",
            "source": str(raw[idx["Source"]] or "").strip() if "Source" in idx else "",
            "client": str(raw[idx["Location"]] or "").strip() if "Location" in idx else "",
        }
        out.append(rec)
    return out


def apply_import(rows: list[dict]) -> dict:
    """Match each parsed row to an existing Order by external_order_id and
    update Order.total_amount with the Food Total. For orders not yet in our
    DB, create a stub Order row so /reports/sales picks them up.

    Returns a summary: {parsed, updated, created, skipped, examples}.
    """
    db = SessionLocal()
    try:
        updated = 0
        created = 0
        skipped = 0
        examples_updated = []
        examples_created = []
        for r in rows:
            on = r["order_number"]
            existing = db.query(Order).filter(Order.external_order_id == on).first()
            if existing:
                old = existing.total_amount
                existing.total_amount = r["food_total"]
                if r.get("client") and not existing.client:
                    existing.client = r["client"]
                if r.get("event_date") and not existing.delivery_date:
                    existing.delivery_date = r["event_date"]
                if r.get("store_id") and not existing.origin_store_id:
                    existing.origin_store_id = r["store_id"]
                updated += 1
                if len(examples_updated) < 5:
                    examples_updated.append({"order_number": on, "old": old, "new": r["food_total"]})
            else:
                if not r.get("event_date") or not r.get("store_id"):
                    skipped += 1
                    continue
                stub = Order(
                    external_order_id=on,
                    client=r.get("client") or None,
                    delivery_date=r["event_date"],
                    origin_store_id=r["store_id"],
                    reported_store_id=r["store_id"],
                    total_amount=r["food_total"],
                    status=("cancelled" if "cancel" in r.get("status", "").lower()
                            else "imported"),
                )
                db.add(stub)
                created += 1
                if len(examples_created) < 5:
                    examples_created.append({"order_number": on, "amount": r["food_total"],
                                             "store": r["store_id"], "date": r["event_date"]})
        db.commit()
        return {
            "parsed":  len(rows),
            "updated": updated,
            "created": created,
            "skipped": skipped,
            "examples_updated": examples_updated,
            "examples_created": examples_created,
        }
    finally:
        db.close()
