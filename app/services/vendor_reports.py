"""Vendor report analytics for the store-scoped Vendors -> Reports page."""
from __future__ import annotations

import json
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_

from app.models import ProducePriceSnapshot, VendorRecentOrder


SUPPLY_VENDOR_LABELS = {
    "webstaurant": "Webstaurant",
    "performance-food": "Performance Food",
    "restaurant-depot": "Restaurant Depot",
    "specs": "Specs",
}

REPORT_VENDOR_OPTIONS = [
    {"value": "all", "label": "All Supply Vendors"},
    {"value": "produce", "label": "Produce"},
    *[
        {"value": slug, "label": label}
        for slug, label in SUPPLY_VENDOR_LABELS.items()
    ],
]

VALID_REPORT_VENDORS = {opt["value"] for opt in REPORT_VENDOR_OPTIONS}


def default_date_range(today: date | None = None) -> tuple[date, date]:
    """Default to a 30-day reporting window ending today."""
    end = today or date.today()
    return end - timedelta(days=29), end


def parse_report_dates(
    start_raw: str | None,
    end_raw: str | None,
    today: date | None = None,
) -> tuple[date, date, str | None]:
    default_start, default_end = default_date_range(today)
    try:
        start = date.fromisoformat(start_raw) if start_raw else default_start
        end = date.fromisoformat(end_raw) if end_raw else default_end
    except ValueError:
        return default_start, default_end, "Invalid date format. Use YYYY-MM-DD."
    if start > end:
        return default_start, default_end, "Start date must be on or before end date."
    if (end - start).days > 366:
        return default_start, default_end, "Range too long. Pick 366 days or less."
    return start, end, None


def normalize_vendor(value: str | None) -> str:
    vendor = (value or "all").strip().lower()
    return vendor if vendor in VALID_REPORT_VENDORS else "all"


def build_report(
    db,
    vendor: str,
    start: date,
    end: date,
    store_scope: str = "both",
    selected_item_key: str | None = None,
) -> dict[str, Any]:
    vendor = normalize_vendor(vendor)
    if vendor == "produce":
        return build_produce_report(db, start, end, store_scope, selected_item_key)
    return build_supply_report(db, vendor, start, end, store_scope, selected_item_key)


def build_supply_report(
    db,
    vendor: str,
    start: date,
    end: date,
    store_scope: str = "both",
    selected_item_key: str | None = None,
) -> dict[str, Any]:
    vendor_slugs = (
        list(SUPPLY_VENDOR_LABELS)
        if vendor == "all"
        else [vendor]
    )
    rows = _supply_orders(db, vendor_slugs, start, end, store_scope)

    order_rows: list[dict[str, Any]] = []
    item_acc: dict[tuple[str, str, str], dict[str, Any]] = {}
    vendor_breakdown: dict[str, dict[str, Any]] = {
        slug: _blank_breakdown(slug) for slug in vendor_slugs
    }
    daily: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"date": "", "orders": 0, "spend_cents": 0, "units": 0.0}
    )

    total_spend = 0
    total_units = 0.0
    line_count = 0
    orders_with_lines = 0

    for row in rows:
        order_dt = row.placed_at or row.created_at
        order_date = order_dt.date() if order_dt else None
        item_rows = _normalize_order_items(row.items_json)
        line_spend = sum((it.get("line_cents") or 0) for it in item_rows)
        order_total = row.total_cents if row.total_cents is not None else line_spend
        order_total = int(order_total or 0)
        total_spend += order_total
        if item_rows:
            orders_with_lines += 1

        vbreak = vendor_breakdown.setdefault(row.vendor, _blank_breakdown(row.vendor))
        vbreak["orders"] += 1
        vbreak["spend_cents"] += order_total
        vbreak["spend"] = money(vbreak["spend_cents"])

        if order_date:
            day_key = order_date.isoformat()
            daily[day_key]["date"] = day_key
            daily[day_key]["orders"] += 1
            daily[day_key]["spend_cents"] += order_total

        for it in item_rows:
            name = it["name"]
            sku = it.get("sku") or ""
            qty = it.get("qty_num")
            qty_val = float(qty) if qty is not None else 0.0
            unit_cents = it.get("unit_cents")
            line_cents = it.get("line_cents")
            if line_cents is None and unit_cents is not None and qty_val:
                line_cents = int(round(unit_cents * qty_val))
            if unit_cents is None and line_cents is not None and qty_val:
                unit_cents = int(round(line_cents / qty_val))
            item_key = (row.vendor, sku.lower(), _norm_name(name))
            encoded_item_key = _supply_item_key(row.vendor, sku, name)
            acc = item_acc.setdefault(item_key, {
                "item_key": encoded_item_key,
                "vendor": row.vendor,
                "vendor_label": SUPPLY_VENDOR_LABELS.get(row.vendor, row.vendor),
                "name": name,
                "sku": sku,
                "orders": set(),
                "units": 0.0,
                "spend_cents": 0,
                "unit_prices": [],
                "first_seen": None,
                "latest_seen": None,
                "latest_unit_cents": None,
                "price_points": [],
                "order_lines": [],
            })
            acc["orders"].add(row.id)
            acc["units"] += qty_val
            acc["spend_cents"] += int(line_cents or 0)
            if unit_cents is not None:
                seen_date = order_date or date.min
                acc["unit_prices"].append((seen_date, int(unit_cents)))
                if acc["latest_seen"] is None or seen_date >= acc["latest_seen"]:
                    acc["latest_seen"] = seen_date
                    acc["latest_unit_cents"] = int(unit_cents)
                if acc["first_seen"] is None or seen_date <= acc["first_seen"]:
                    acc["first_seen"] = seen_date
                acc["price_points"].append({
                    "date": _display_date(order_date),
                    "date_iso": order_date.isoformat() if order_date else "",
                    "vendor_label": SUPPLY_VENDOR_LABELS.get(row.vendor, row.vendor),
                    "order_number": row.order_number or "Order",
                    "unit_cents": int(unit_cents),
                    "unit": money(unit_cents),
                    "qty": qty_display(qty_val),
                    "line": money(line_cents) if line_cents is not None else "-",
                    "store": _display_store(row.store_scope),
                    "buyer": _buyer_label(row.customer_or_caterer, row.subject),
                    "status": row.status or "order",
                })
            acc["order_lines"].append({
                "date": _display_date(order_date),
                "date_iso": order_date.isoformat() if order_date else "",
                "vendor_label": SUPPLY_VENDOR_LABELS.get(row.vendor, row.vendor),
                "order_number": row.order_number or "Order",
                "store": _display_store(row.store_scope),
                "buyer": _buyer_label(row.customer_or_caterer, row.subject),
                "status": row.status or "order",
                "qty": qty_display(qty_val),
                "unit": money(unit_cents) if unit_cents is not None else "-",
                "line": money(line_cents) if line_cents is not None else "-",
                "subject": row.subject or "",
            })
            total_units += qty_val
            line_count += 1
            if order_date:
                daily[order_date.isoformat()]["units"] += qty_val

        order_rows.append({
            "id": row.id,
            "vendor": row.vendor,
            "vendor_label": SUPPLY_VENDOR_LABELS.get(row.vendor, row.vendor),
            "order_number": row.order_number or "Order",
            "date": _display_date(order_date),
            "date_iso": order_date.isoformat() if order_date else "",
            "store": _display_store(row.store_scope),
            "status": row.status or "order",
            "total_cents": order_total,
            "total": money(order_total),
            "line_count": len(item_rows),
            "subject": row.subject or "",
        })

    item_rows = [_finalize_supply_item(acc) for acc in item_acc.values()]
    item_rows.sort(key=lambda r: (-r["spend_cents"], -r["units"], r["name"].lower()))
    item_options = _item_options_from_rows(item_rows)
    acc_by_item_key = {acc["item_key"]: acc for acc in item_acc.values()}
    selected_detail = None
    if selected_item_key in acc_by_item_key:
        selected_detail = _supply_item_detail(acc_by_item_key[selected_item_key])
    selected_item_key = selected_detail["item_key"] if selected_detail else ""
    price_watch = [
        r for r in item_rows
        if r["price_delta_cents"] is not None and r["price_delta_cents"] != 0
    ]
    price_watch.sort(key=lambda r: -abs(r.get("price_delta_pct") or 0))
    daily_rows = _finalize_daily_rows(daily)
    breakdown_rows = list(vendor_breakdown.values())
    breakdown_rows.sort(key=lambda r: (-r["spend_cents"], r["vendor_label"]))

    summary = {
        "orders": len(rows),
        "spend_cents": total_spend,
        "spend": money(total_spend),
        "avg_order": money(round(total_spend / len(rows)) if rows else 0),
        "units": qty_display(total_units),
        "unique_items": len(item_rows),
        "line_count": line_count,
        "orders_with_lines": orders_with_lines,
    }

    return {
        "kind": "supply",
        "vendor": vendor,
        "vendor_label": "All Supply Vendors" if vendor == "all" else SUPPLY_VENDOR_LABELS[vendor],
        "summary": summary,
        "daily_rows": daily_rows,
        "vendor_breakdown": breakdown_rows,
        "item_options": item_options,
        "selected_item_key": selected_item_key,
        "selected_item": selected_detail,
        "top_items": item_rows[:25],
        "price_watch": price_watch[:20],
        "order_rows": order_rows[:40],
        "empty": not rows,
    }


def build_produce_report(
    db,
    start: date,
    end: date,
    store_scope: str = "both",
    selected_item_key: str | None = None,
) -> dict[str, Any]:
    completed = _load_completed_produce_orders()
    orders = [
        order for order in completed
        if _order_in_range(order, start, end) and _produce_store_matches(order, store_scope)
    ]

    item_acc: dict[tuple[str, str], dict[str, Any]] = {}
    daily: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"date": "", "orders": 0, "spend_cents": 0, "units": 0.0}
    )
    vendor_breakdown: dict[str, dict[str, Any]] = {
        "alvarado": _blank_produce_vendor("alvarado"),
        "jluna": _blank_produce_vendor("jluna"),
    }

    total_spend = 0
    total_units = 0.0
    order_rows = []
    for order in orders:
        order_date = _produce_order_date(order)
        cart = order.get("cart") or []
        order_cents = 0
        for line in cart:
            name = (line.get("canonical_name") or line.get("vendor_name") or "").strip()
            if not name:
                continue
            size = (line.get("canonical_size") or "").strip()
            vendor = (line.get("vendor") or "").strip().lower()
            qty = _parse_qty(line.get("qty")) or 0.0
            price_cents = _money_to_cents(line.get("price"))
            line_cents = int(round((price_cents or 0) * qty))
            order_cents += line_cents
            total_units += qty
            key = (name, size)
            acc = item_acc.setdefault(key, {
                "name": name,
                "size": size,
                "orders": set(),
                "units": 0.0,
                "spend_cents": 0,
                "vendors": set(),
                "latest_price_cents": None,
                "latest_seen": None,
            })
            acc["orders"].add(order.get("order_id") or "")
            acc["units"] += qty
            acc["spend_cents"] += line_cents
            if vendor:
                acc["vendors"].add(vendor)
                vb = vendor_breakdown.setdefault(vendor, _blank_produce_vendor(vendor))
                vb["orders"].add(order.get("order_id") or "")
                vb["spend_cents"] += line_cents
                vb["units"] += qty
            if price_cents is not None and (acc["latest_seen"] is None or order_date >= acc["latest_seen"]):
                acc["latest_seen"] = order_date
                acc["latest_price_cents"] = price_cents
        total_spend += order_cents
        if order_date:
            day_key = order_date.isoformat()
            daily[day_key]["date"] = day_key
            daily[day_key]["orders"] += 1
            daily[day_key]["spend_cents"] += order_cents
            daily[day_key]["units"] += sum((_parse_qty(l.get("qty")) or 0.0) for l in cart)
        order_rows.append({
            "order_number": order.get("order_id") or "Produce order",
            "date": _display_date(order_date),
            "date_iso": order_date.isoformat() if order_date else "",
            "store": _display_store(order.get("used_location") or order.get("selected_location")),
            "status": order.get("status") or "sent",
            "total_cents": order_cents,
            "total": money(order_cents),
            "line_count": len(cart),
            "subject": order.get("manager") or "",
        })

    item_rows = []
    for acc in item_acc.values():
        item_rows.append({
            "item_key": _produce_item_key(acc["name"], acc["size"]),
            "name": acc["name"],
            "size": acc["size"],
            "orders": len({x for x in acc["orders"] if x}),
            "units": qty_display(acc["units"]),
            "units_num": acc["units"],
            "spend_cents": acc["spend_cents"],
            "spend": money(acc["spend_cents"]),
            "vendors": ", ".join(_produce_vendor_label(v) for v in sorted(acc["vendors"])),
            "latest_unit": money(acc["latest_price_cents"]) if acc["latest_price_cents"] is not None else "-",
        })
    item_rows.sort(key=lambda r: (-r["spend_cents"], -r["units_num"], r["name"].lower()))

    price_rows, price_watch = _produce_price_rows(db, start, end)
    item_options = _produce_item_options(item_rows, price_rows)
    selected_detail = None
    selected_payload = _decode_item_key(selected_item_key)
    if selected_payload and selected_payload.get("kind") == "produce":
        selected_key = _produce_item_key(
            selected_payload.get("name") or "",
            selected_payload.get("size") or "",
        )
        if selected_key in {opt["value"] for opt in item_options}:
            selected_detail = _produce_item_detail(
                db,
                selected_payload.get("name") or "",
                selected_payload.get("size") or "",
                start,
                end,
                orders,
            )
    selected_item_key = selected_detail["item_key"] if selected_detail else ""
    daily_rows = _finalize_daily_rows(daily)
    breakdown_rows = []
    for row in vendor_breakdown.values():
        row["orders"] = len(row["orders"])
        row["spend"] = money(row["spend_cents"])
        row["units"] = qty_display(row["units"])
        breakdown_rows.append(row)
    breakdown_rows.sort(key=lambda r: (-r["spend_cents"], r["vendor_label"]))

    summary = {
        "orders": len(orders),
        "spend_cents": total_spend,
        "spend": money(total_spend),
        "avg_order": money(round(total_spend / len(orders)) if orders else 0),
        "units": qty_display(total_units),
        "unique_items": len(item_rows),
        "tracked_quote_items": len(price_rows),
        "price_snapshots": sum(r["snapshot_count"] for r in price_rows),
    }

    return {
        "kind": "produce",
        "vendor": "produce",
        "vendor_label": "Produce",
        "summary": summary,
        "daily_rows": daily_rows,
        "vendor_breakdown": breakdown_rows,
        "item_options": item_options,
        "selected_item_key": selected_item_key,
        "selected_item": selected_detail,
        "top_items": item_rows[:25],
        "price_rows": price_rows[:30],
        "price_watch": price_watch[:20],
        "order_rows": order_rows[:40],
        "empty": not orders and not price_rows,
    }


def money(cents: int | float | None) -> str:
    cents = int(round(cents or 0))
    return f"${cents / 100:,.2f}"


def qty_display(value: int | float | None) -> str:
    value = float(value or 0)
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def pct_display(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%"


def _supply_orders(db, vendors: list[str], start: date, end: date, store_scope: str):
    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end, time.max)
    q = db.query(VendorRecentOrder).filter(VendorRecentOrder.vendor.in_(vendors))
    if store_scope in ("tomball", "copperfield"):
        q = q.filter(
            (VendorRecentOrder.store_scope == store_scope) |
            (VendorRecentOrder.store_scope.is_(None))
        )
    q = q.filter(or_(
        and_(
            VendorRecentOrder.placed_at.is_not(None),
            VendorRecentOrder.placed_at >= start_dt,
            VendorRecentOrder.placed_at <= end_dt,
        ),
        and_(
            VendorRecentOrder.placed_at.is_(None),
            VendorRecentOrder.created_at >= start_dt,
            VendorRecentOrder.created_at <= end_dt,
        ),
    ))
    return (
        q.order_by(
            VendorRecentOrder.placed_at.desc().nullslast(),
            VendorRecentOrder.created_at.desc(),
        )
        .all()
    )


def _normalize_order_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_items = payload.get("items") or payload.get("lines") or []
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    out = []
    for raw in raw_items:
        if isinstance(raw, dict) and "_meta" in raw:
            continue
        if isinstance(raw, str):
            name = raw.strip()
            raw = {}
        elif isinstance(raw, dict):
            name = (
                raw.get("name")
                or raw.get("description")
                or raw.get("item")
                or raw.get("title")
                or ""
            ).strip()
        else:
            continue
        if not name:
            continue
        qty = _parse_qty(
            raw.get("qty") if isinstance(raw, dict) else None
        ) if isinstance(raw, dict) else None
        unit_cents = None
        line_cents = None
        if isinstance(raw, dict):
            unit_cents = _cents_field(raw.get("unit_price_cents"))
            if unit_cents is None:
                unit_cents = _money_to_cents(
                    raw.get("unit_price") or raw.get("price") or raw.get("unit")
                )
            line_cents = _cents_field(raw.get("subtotal_cents") or raw.get("line_total_cents"))
            if line_cents is None:
                line_cents = _money_to_cents(
                    raw.get("subtotal") or raw.get("line_total") or raw.get("total")
                )
        out.append({
            "name": name,
            "sku": (raw.get("sku") or raw.get("ref") or raw.get("item_number") or "").strip()
            if isinstance(raw, dict) else "",
            "qty_num": qty,
            "unit_cents": unit_cents,
            "line_cents": line_cents,
        })
    return out


def _finalize_supply_item(acc: dict[str, Any]) -> dict[str, Any]:
    prices = sorted(acc["unit_prices"], key=lambda x: x[0])
    first_price = prices[0][1] if prices else None
    latest_price = prices[-1][1] if prices else acc["latest_unit_cents"]
    low_price = min((p for _, p in prices), default=None)
    high_price = max((p for _, p in prices), default=None)
    avg_price = (
        round(sum(p for _, p in prices) / len(prices))
        if prices else None
    )
    delta = None
    delta_pct = None
    if first_price is not None and latest_price is not None:
        delta = latest_price - first_price
        if first_price:
            delta_pct = (delta / first_price) * 100
    return {
        "item_key": acc["item_key"],
        "vendor": acc["vendor"],
        "vendor_label": acc["vendor_label"],
        "name": acc["name"],
        "sku": acc["sku"],
        "orders": len(acc["orders"]),
        "units": acc["units"],
        "units_display": qty_display(acc["units"]),
        "spend_cents": acc["spend_cents"],
        "spend": money(acc["spend_cents"]),
        "latest_unit": money(latest_price) if latest_price is not None else "-",
        "avg_unit": money(avg_price) if avg_price is not None else "-",
        "low_unit": money(low_price) if low_price is not None else "-",
        "high_unit": money(high_price) if high_price is not None else "-",
        "first_unit": money(first_price) if first_price is not None else "-",
        "price_delta_cents": delta,
        "price_delta": (money(abs(delta)) if delta is not None else "-"),
        "price_delta_class": "up" if (delta or 0) > 0 else ("down" if (delta or 0) < 0 else "flat"),
        "price_delta_pct": delta_pct,
        "price_delta_pct_display": pct_display(delta_pct),
        "first_seen": _display_date(acc["first_seen"]),
        "latest_seen": _display_date(acc["latest_seen"]),
    }


def _produce_price_rows(db, start: date, end: date) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = (
        db.query(ProducePriceSnapshot)
        .filter(ProducePriceSnapshot.snapshot_date >= start.isoformat())
        .filter(ProducePriceSnapshot.snapshot_date <= end.isoformat())
        .order_by(
            ProducePriceSnapshot.canonical_name.asc(),
            ProducePriceSnapshot.canonical_size.asc(),
            ProducePriceSnapshot.vendor.asc(),
            ProducePriceSnapshot.snapshot_date.asc(),
        )
        .all()
    )
    grouped: dict[tuple[str, str | None], dict[str, list[ProducePriceSnapshot]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        grouped[(row.canonical_name, row.canonical_size)][row.vendor].append(row)

    price_rows: list[dict[str, Any]] = []
    price_watch: list[dict[str, Any]] = []
    for (name, size), by_vendor in grouped.items():
        latest = {}
        first = {}
        snapshot_count = 0
        for vendor, vendor_rows in by_vendor.items():
            if not vendor_rows:
                continue
            snapshot_count += len(vendor_rows)
            first[vendor] = vendor_rows[0]
            latest[vendor] = vendor_rows[-1]
            if len(vendor_rows) >= 2 and vendor_rows[0].price:
                old = vendor_rows[0]
                new = vendor_rows[-1]
                delta = new.price - old.price
                if delta:
                    price_watch.append({
                        "item_key": _produce_item_key(name, size or ""),
                        "vendor_label": _produce_vendor_label(vendor),
                        "name": name,
                        "size": size or "",
                        "first_unit": money(round(old.price * 100)),
                        "latest_unit": money(round(new.price * 100)),
                        "price_delta": money(round(abs(delta) * 100)),
                        "price_delta_class": "up" if delta > 0 else "down",
                        "price_delta_pct": (delta / old.price) * 100,
                        "price_delta_pct_display": pct_display((delta / old.price) * 100),
                        "first_seen": _display_date(_date_from_iso(old.snapshot_date)),
                        "latest_seen": _display_date(_date_from_iso(new.snapshot_date)),
                    })
        latest_prices = {
            vendor: row.price
            for vendor, row in latest.items()
            if row.price is not None
        }
        cheaper = min(latest_prices, key=latest_prices.get) if latest_prices else ""
        spread = None
        if len(latest_prices) >= 2:
            vals = list(latest_prices.values())
            spread = max(vals) - min(vals)
        price_rows.append({
            "item_key": _produce_item_key(name, size or ""),
            "name": name,
            "size": size or "",
            "alvarado": money(round(latest["alvarado"].price * 100)) if latest.get("alvarado") else "-",
            "jluna": money(round(latest["jluna"].price * 100)) if latest.get("jluna") else "-",
            "cheaper": _produce_vendor_label(cheaper) if cheaper else "-",
            "spread": money(round(spread * 100)) if spread is not None else "-",
            "latest_seen": _display_date(
                max((_date_from_iso(r.snapshot_date) for r in latest.values()), default=None)
            ),
            "snapshot_count": snapshot_count,
        })
    price_rows.sort(key=lambda r: (r["name"].lower(), r["size"].lower()))
    price_watch.sort(key=lambda r: -abs(r["price_delta_pct"]))
    return price_rows, price_watch


def _supply_item_detail(acc: dict[str, Any]) -> dict[str, Any]:
    price_points = sorted(
        acc["price_points"],
        key=lambda r: (r.get("date_iso") or "", r.get("order_number") or ""),
    )
    max_price = max((p["unit_cents"] for p in price_points), default=0)
    for point in price_points:
        point["bar_pct"] = round((point["unit_cents"] / max_price) * 100) if max_price else 0
    order_lines = sorted(
        acc["order_lines"],
        key=lambda r: (r.get("date_iso") or "", r.get("order_number") or ""),
        reverse=True,
    )
    finalized = _finalize_supply_item(acc)
    return {
        "kind": "supply",
        "item_key": acc["item_key"],
        "name": acc["name"],
        "subtitle": " · ".join(
            part for part in (acc["vendor_label"], acc["sku"] or None) if part
        ),
        "summary_cards": [
            {"label": "Latest Unit", "value": finalized["latest_unit"]},
            {"label": "Avg Unit", "value": finalized["avg_unit"]},
            {"label": "Ordered Units", "value": finalized["units_display"]},
            {"label": "Spend", "value": finalized["spend"]},
        ],
        "price_history": price_points,
        "order_lines": order_lines,
    }


def _produce_item_detail(
    db,
    name: str,
    size: str,
    start: date,
    end: date,
    orders: list[dict[str, Any]],
) -> dict[str, Any]:
    q = (
        db.query(ProducePriceSnapshot)
        .filter(ProducePriceSnapshot.canonical_name == name)
        .filter(ProducePriceSnapshot.snapshot_date >= start.isoformat())
        .filter(ProducePriceSnapshot.snapshot_date <= end.isoformat())
    )
    if size:
        q = q.filter(ProducePriceSnapshot.canonical_size == size)
    else:
        q = q.filter(or_(
            ProducePriceSnapshot.canonical_size.is_(None),
            ProducePriceSnapshot.canonical_size == "",
        ))
    rows = q.order_by(
        ProducePriceSnapshot.snapshot_date.asc(),
        ProducePriceSnapshot.vendor.asc(),
    ).all()

    by_date: dict[str, dict[str, Any]] = defaultdict(dict)
    for row in rows:
        by_date[row.snapshot_date][row.vendor] = row
    price_history = []
    max_price = 0.0
    for day in sorted(by_date):
        vendors = by_date[day]
        prices = {
            vendor: row.price
            for vendor, row in vendors.items()
            if row.price is not None
        }
        if prices:
            max_price = max(max_price, *prices.values())
        cheaper = min(prices, key=prices.get) if prices else ""
        spread = None
        if len(prices) >= 2:
            spread = max(prices.values()) - min(prices.values())
        price_history.append({
            "date": _display_date(_date_from_iso(day)),
            "date_iso": day,
            "alvarado": money(round(prices["alvarado"] * 100)) if "alvarado" in prices else "-",
            "jluna": money(round(prices["jluna"] * 100)) if "jluna" in prices else "-",
            "cheaper": _produce_vendor_label(cheaper) if cheaper else "-",
            "spread": money(round(spread * 100)) if spread is not None else "-",
            "best_price": min(prices.values()) if prices else 0,
            "best_price_display": money(round(min(prices.values()) * 100)) if prices else "-",
        })
    for point in price_history:
        point["bar_pct"] = round((point["best_price"] / max_price) * 100) if max_price else 0

    order_lines = []
    orders_by_date: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"units": 0.0, "spend_cents": 0, "buyers": set(), "orders": set()}
    )
    ordered_units = 0.0
    spend_cents = 0
    order_ids = set()
    for order in orders:
        order_date = _produce_order_date(order)
        for line in order.get("cart") or []:
            line_name = (line.get("canonical_name") or line.get("vendor_name") or "").strip()
            line_size = (line.get("canonical_size") or "").strip()
            if line_name != name or line_size != size:
                continue
            qty = _parse_qty(line.get("qty")) or 0.0
            unit_cents = _money_to_cents(line.get("price"))
            line_cents = int(round((unit_cents or 0) * qty))
            ordered_units += qty
            spend_cents += line_cents
            order_id = order.get("order_id") or "Produce order"
            order_ids.add(order_id)
            if order_date:
                date_bucket = orders_by_date[order_date.isoformat()]
                date_bucket["units"] += qty
                date_bucket["spend_cents"] += line_cents
                date_bucket["orders"].add(order_id)
                buyer = (order.get("manager") or "").strip()
                if buyer:
                    date_bucket["buyers"].add(buyer)
            order_lines.append({
                "date": _display_date(order_date),
                "date_iso": order_date.isoformat() if order_date else "",
                "vendor_label": _produce_vendor_label(line.get("vendor")),
                "order_number": order_id,
                "store": _display_store(order.get("used_location") or order.get("selected_location")),
                "buyer": order.get("manager") or "-",
                "status": order.get("status") or "sent",
                "qty": qty_display(qty),
                "unit": money(unit_cents) if unit_cents is not None else "-",
                "line": money(line_cents),
                "delivery_date": order.get("delivery_date") or "",
            })
    order_lines.sort(key=lambda r: (r.get("date_iso") or "", r.get("order_number") or ""), reverse=True)
    for point in price_history:
        bucket = orders_by_date.get(point["date_iso"])
        if not bucket:
            point["order_note"] = ""
            point["order_people"] = ""
            continue
        point["order_note"] = (
            f"{qty_display(bucket['units'])} units ordered · {money(bucket['spend_cents'])}"
        )
        point["order_people"] = ", ".join(sorted(bucket["buyers"])) or "-"

    latest = price_history[-1] if price_history else {}
    latest_best = "-"
    if latest:
        latest_best = f"{latest.get('best_price_display', '-')} · {latest.get('cheaper', '-')}"
    return {
        "kind": "produce",
        "item_key": _produce_item_key(name, size),
        "name": name,
        "subtitle": size or "Produce item",
        "summary_cards": [
            {"label": "Latest Best", "value": latest_best},
            {"label": "Snapshots", "value": str(len(price_history))},
            {"label": "Ordered Units", "value": qty_display(ordered_units)},
            {"label": "Spend", "value": money(spend_cents)},
        ],
        "price_history": price_history,
        "order_lines": order_lines,
        "order_count": len(order_ids),
    }


def _load_completed_produce_orders() -> list[dict[str, Any]]:
    try:
        from app.web import produce_order
        path = Path(produce_order.COMPLETED_ORDERS_FILE)
    except Exception:
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return []
    if isinstance(payload, dict):
        return [v for v in payload.values() if isinstance(v, dict)]
    if isinstance(payload, list):
        return [v for v in payload if isinstance(v, dict)]
    return []


def _order_in_range(order: dict[str, Any], start: date, end: date) -> bool:
    order_date = _produce_order_date(order)
    return bool(order_date and start <= order_date <= end)


def _produce_order_date(order: dict[str, Any]) -> date | None:
    for key in ("executed_at", "manager_confirmation_at", "submitted_at", "canceled_at"):
        parsed = _date_from_iso(order.get(key))
        if parsed:
            return parsed
    return _date_from_iso(order.get("delivery_date"))


def _produce_store_matches(order: dict[str, Any], store_scope: str) -> bool:
    if store_scope not in ("tomball", "copperfield"):
        return True
    store = (order.get("used_location") or order.get("selected_location") or "").strip().lower()
    aliases = {
        "tomball": {"tomball", "dos"},
        "copperfield": {"copperfield", "uno"},
    }
    return store in aliases[store_scope]


def _parse_qty(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _cents_field(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None


def _money_to_cents(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value) * 100))
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return int(round(float(match.group(0)) * 100))


def _norm_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _encode_item_key(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_item_key(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        padded = value + ("=" * (-len(value) % 4))
        payload = json.loads(urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _supply_item_key(vendor: str, sku: str, name: str) -> str:
    return _encode_item_key({
        "kind": "supply",
        "vendor": vendor,
        "sku": (sku or "").strip().lower(),
        "name": _norm_name(name),
    })


def _produce_item_key(name: str, size: str | None) -> str:
    return _encode_item_key({
        "kind": "produce",
        "name": (name or "").strip(),
        "size": (size or "").strip(),
    })


def _item_options_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for row in rows:
        key = row.get("item_key")
        if not key:
            continue
        parts = [row.get("name") or "Item"]
        if row.get("sku"):
            parts.append(row["sku"])
        if row.get("vendor_label"):
            parts.append(row["vendor_label"])
        seen[key] = " · ".join(parts)
    return [
        {"value": key, "label": label}
        for key, label in sorted(seen.items(), key=lambda kv: kv[1].lower())
    ]


def _produce_item_options(
    item_rows: list[dict[str, Any]],
    price_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for row in [*item_rows, *price_rows]:
        key = row.get("item_key")
        if not key:
            continue
        label = row.get("name") or "Produce item"
        if row.get("size"):
            label = f"{label} · {row['size']}"
        seen[key] = label
    return [
        {"value": key, "label": label}
        for key, label in sorted(seen.items(), key=lambda kv: kv[1].lower())
    ]


def _buyer_label(customer_or_caterer: str | None, subject: str | None) -> str:
    val = (customer_or_caterer or "").strip()
    if val:
        return val
    subj = (subject or "").strip()
    return subj[:90] if subj else "-"


def _display_date(value: date | datetime | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        value = value.date()
    return f"{value:%b} {value.day}, {value:%Y}"


def _date_from_iso(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _display_store(value: str | None) -> str:
    raw = (value or "both stores").strip()
    low = raw.lower()
    if low in ("tomball", "dos"):
        return "Tomball"
    if low in ("copperfield", "uno"):
        return "Copperfield"
    if low in ("both", "both stores"):
        return "Both Stores"
    return raw


def _blank_breakdown(slug: str) -> dict[str, Any]:
    return {
        "vendor": slug,
        "vendor_label": SUPPLY_VENDOR_LABELS.get(slug, slug),
        "orders": 0,
        "spend_cents": 0,
        "spend": money(0),
    }


def _blank_produce_vendor(slug: str) -> dict[str, Any]:
    return {
        "vendor": slug,
        "vendor_label": _produce_vendor_label(slug),
        "orders": set(),
        "spend_cents": 0,
        "spend": money(0),
        "units": 0.0,
    }


def _produce_vendor_label(slug: str | None) -> str:
    return {
        "alvarado": "Alvarado",
        "jluna": "J. Luna",
    }.get((slug or "").lower(), slug or "-")


def _finalize_daily_rows(daily: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    max_spend = max((v["spend_cents"] for v in daily.values()), default=0)
    for key in sorted(daily):
        row = daily[key]
        row["spend"] = money(row["spend_cents"])
        row["units_display"] = qty_display(row["units"])
        row["label"] = _display_date(_date_from_iso(key))
        row["bar_pct"] = round((row["spend_cents"] / max_spend) * 100) if max_spend else 0
        rows.append(row)
    return rows
