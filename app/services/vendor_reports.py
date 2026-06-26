"""Vendor report analytics for the store-scoped Vendors -> Reports page."""
from __future__ import annotations

import json
import re
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
) -> dict[str, Any]:
    vendor = normalize_vendor(vendor)
    if vendor == "produce":
        return build_produce_report(db, start, end, store_scope)
    return build_supply_report(db, vendor, start, end, store_scope)


def build_supply_report(
    db,
    vendor: str,
    start: date,
    end: date,
    store_scope: str = "both",
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
            acc = item_acc.setdefault(item_key, {
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
