"""Sanitized Toast Analytics aggregates for dashboards and assistant tools."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.toast_analytics_client import ToastAnalyticsClient, period_to_ymd_range


VALID_PERIODS = {"today", "week", "last_week"}
LABOR_RATIO_MIN_ORDERS = 10
LABOR_RATIO_MIN_NET_SALES = 500.0


def _format_ymd(value: str) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _date_range_label(start_ymd: str, end_ymd: str) -> str:
    start = _format_ymd(start_ymd)
    end = _format_ymd(end_ymd)
    return start if start == end else f"{start} to {end}"


def _labor_ratio_guard(orders_count: int, net_sales: float) -> dict[str, Any]:
    ok = orders_count >= LABOR_RATIO_MIN_ORDERS and net_sales >= LABOR_RATIO_MIN_NET_SALES
    note = ""
    if not ok:
        note = (
            "this period is below the denominator guard "
            f"({orders_count} {'order' if orders_count == 1 else 'orders'}, "
            f"${net_sales:,.2f} net sales)"
        )
    return {
        "ok": ok,
        "min_orders": LABOR_RATIO_MIN_ORDERS,
        "min_net_sales": LABOR_RATIO_MIN_NET_SALES,
        "note": note,
    }


def analytics_summary_payload(
    period: str = "today",
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Return aggregate-only Toast Analytics data for one approved period."""
    period = normalize_period(period)
    start_ymd, end_ymd, label = period_to_ymd_range(period)
    ana = client or ToastAnalyticsClient.shared()

    metrics = ana.metrics(start_ymd, end_ymd, [])
    labor_rows = ana.labor(start_ymd, end_ymd, [], group_by=["JOB"])
    menu_rows = ana.menu(start_ymd, end_ymd, [])

    net_sales = sum(float(m.get("netSalesAmount") or 0) for m in metrics)
    gross_sales = sum(float(m.get("grossSalesAmount") or 0) for m in metrics)
    discount_amt = sum(float(m.get("discountAmount") or 0) for m in metrics)
    void_amt = sum(float(m.get("voidOrdersAmount") or 0) for m in metrics)
    refund_amt = sum(float(m.get("refundAmount") or 0) for m in metrics)
    orders_count = sum(int(m.get("ordersCount") or 0) for m in metrics)
    guest_count = sum(int(m.get("guestCount") or 0) for m in metrics)
    labor_hours = sum(float(m.get("hourlyJobTotalHours") or 0) for m in metrics)
    labor_pay = sum(float(m.get("hourlyJobTotalPay") or 0) for m in metrics)
    avg_order = (net_sales / orders_count) if orders_count else 0.0
    sales_per_labor_hour = (net_sales / labor_hours) if labor_hours else 0.0
    labor_ratio_pct = (labor_pay / net_sales * 100.0) if net_sales else 0.0
    labor_ratio_guard = _labor_ratio_guard(orders_count, net_sales)

    by_job: dict[str, dict[str, float | str]] = {}
    for row in labor_rows:
        title = str(row.get("jobTitle") or "Other").strip() or "Other"
        if title not in by_job:
            by_job[title] = {"label": title, "value": 0.0, "hours": 0.0}
        by_job[title]["value"] = float(by_job[title]["value"]) + float(row.get("totalCost") or 0)
        by_job[title]["hours"] = float(by_job[title]["hours"]) + float(row.get("totalHours") or 0)
    labor_by_job = sorted(
        [v for v in by_job.values() if float(v["value"]) > 0],
        key=lambda r: -float(r["value"]),
    )

    menu_qty = sum(float(r.get("quantitySold") or 0) for r in menu_rows)
    menu_avg_price = (
        sum(float(r.get("netSalesAmount") or 0) for r in menu_rows) / menu_qty
        if menu_qty else 0.0
    )
    menu_waste_amount = sum(float(r.get("wasteAmount") or 0) for r in menu_rows)
    menu_waste_count = sum(float(r.get("wasteCount") or 0) for r in menu_rows)

    restaurants_in_data = sorted(
        {m.get("restaurantGuid") for m in metrics if m.get("restaurantGuid")}
    )
    scope_note = (
        "Copperfield only - Tomball is not on the Toast Analytics plan."
        if len(restaurants_in_data) <= 1 else
        f"{len(restaurants_in_data)} locations included."
    )

    return {
        "period": period,
        "label": label,
        "date_range": {
            "start": start_ymd,
            "end": end_ymd,
            "label": _date_range_label(start_ymd, end_ymd),
        },
        "date_range_label": _date_range_label(start_ymd, end_ymd),
        "scope_note": scope_note,
        "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "sales": {
            "net": round(net_sales, 2),
            "gross": round(gross_sales, 2),
            "discount": round(discount_amt, 2),
            "void": round(void_amt, 2),
            "refund": round(refund_amt, 2),
            "avg_order": round(avg_order, 2),
            "sales_per_labor_hour": round(sales_per_labor_hour, 2),
            "orders": orders_count,
            "guests": guest_count,
        },
        "labor": {
            "hours": round(labor_hours, 2),
            "cost": round(labor_pay, 2),
            "ratio_pct": round(labor_ratio_pct, 1),
            "ratio_denominator_ok": labor_ratio_guard["ok"],
            "ratio_guard": labor_ratio_guard,
            "by_job": [
                {
                    "label": str(r["label"]),
                    "value": round(float(r["value"]), 2),
                    "hours": round(float(r["hours"]), 2),
                }
                for r in labor_by_job
            ],
        },
        "menu": {
            "quantity_sold": round(menu_qty, 0),
            "avg_price": round(menu_avg_price, 2),
            "waste_amount": round(menu_waste_amount, 2),
            "waste_count": round(menu_waste_count, 0),
        },
    }


def normalize_period(period: str | None) -> str:
    value = str(period or "today").strip().lower()
    return value if value in VALID_PERIODS else "today"
