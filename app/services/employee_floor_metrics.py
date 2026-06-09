"""Pure employee-floor data model and metrics helpers.

No Flask, database, or Toast client imports live here. The employee console can
feed these helpers demo fixtures today and mapped Toast payloads later without
copying calculation logic into templates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping

CheckStatus = Literal["open", "closed"]
ItemKind = Literal["cocktail", "nonAlcoholic", "food", "side", "dessert"]
PaymentMethod = Literal["cash", "credit", "gift", "other", "unknown"]
PerformanceRange = Literal["today", "week", "month", "last30"]


@dataclass(frozen=True)
class EmployeeFloorEmployee:
    id: int | None
    name: str
    role: str | None = None
    location: str | None = None


@dataclass(frozen=True)
class EmployeeFloorLocation:
    key: str
    label: str


@dataclass(frozen=True)
class EmployeeFloorShift:
    starts_at: Any | None = None
    ends_at: Any | None = None
    hours: float | None = None
    base_pay: float | None = None


@dataclass(frozen=True)
class EmployeeFloorItem:
    name: str
    quantity: float = 1
    kind: ItemKind = "food"
    fired_at: Any | None = None


@dataclass(frozen=True)
class EmployeeFloorPayment:
    method: PaymentMethod = "unknown"
    tip: float | None = None
    paid_at: Any | None = None


@dataclass(frozen=True)
class EmployeeFloorCheck:
    id: str | int
    status: CheckStatus
    covers: int = 0
    total: float = 0.0
    tip: float | None = None
    payment_method: PaymentMethod = "unknown"
    seated_at: Any | None = None
    drinks_fired_at: Any | None = None
    kitchen_fired_at: Any | None = None
    closed_at: Any | None = None
    items: list[EmployeeFloorItem] = field(default_factory=list)
    payments: list[EmployeeFloorPayment] = field(default_factory=list)


@dataclass(frozen=True)
class EmployeeFloorTable:
    name: str
    checks: list[EmployeeFloorCheck] = field(default_factory=list)


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _time_delta_minutes(later: Any | None, earlier: Any | None) -> float | None:
    if later is None or earlier is None:
        return None
    if hasattr(later, "__sub__") and not isinstance(later, (int, float)) and not isinstance(earlier, (int, float)):
        try:
            return max(0.0, (later - earlier).total_seconds() / 60.0)
        except Exception:
            return None
    try:
        return max(0.0, float(later) - float(earlier))
    except (TypeError, ValueError):
        return None


def average(values: Iterable[float | int | None]) -> float | None:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def format_money(amount: float | int | None) -> str:
    if amount is None:
        return "Not available"
    return f"${float(amount):,.2f}"


def flatten_checks(day_or_tables: Any) -> list[Any]:
    tables = _value(day_or_tables, "tables", default=day_or_tables)
    checks: list[Any] = []
    for table in tables or []:
        checks.extend(_value(table, "checks", default=[]) or [])
    return checks


def _items(check: Any) -> list[Any]:
    return list(_value(check, "items", default=[]) or [])


def _item_qty(item: Any) -> float:
    return _num(_value(item, "quantity", "q", default=1), 1.0)


def _item_kind(item: Any) -> str:
    return str(_value(item, "kind", "k", default="food") or "food")


def _has_item_kind(check: Any, *kinds: str) -> bool:
    wanted = set(kinds)
    return any(_item_kind(item) in wanted for item in _items(check))


def _payment_method(check: Any) -> str:
    return str(_value(check, "payment_method", "pay", default="unknown") or "unknown").lower()


def _status(check: Any) -> str:
    return str(_value(check, "status", default="open") or "open").lower()


def check_tip_value(check: Any) -> float | None:
    tip = _value(check, "tip", default=None)
    if tip is not None:
        return _num(tip)
    payments = _value(check, "payments", default=[]) or []
    tips = [
        _value(payment, "tip", default=None)
        for payment in payments
        if _value(payment, "tip", default=None) is not None
    ]
    if tips:
        return sum(_num(tip) for tip in tips)
    return None


def calculate_tip_percent(check: Any) -> float | None:
    total = _num(_value(check, "total", default=0))
    tip = check_tip_value(check)
    if total <= 0 or tip is None:
        return None
    return tip / total * 100.0


def calculate_drink_attach(checks: Iterable[Any]) -> float | None:
    rows = list(checks)
    if not rows:
        return None
    attached = sum(1 for check in rows if _has_item_kind(check, "cocktail"))
    return attached / len(rows) * 100.0


def calculate_dessert_attach(checks: Iterable[Any]) -> float | None:
    rows = list(checks)
    if not rows:
        return None
    attached = sum(1 for check in rows if _has_item_kind(check, "dessert"))
    return attached / len(rows) * 100.0


def calculate_tips_per_hour(tips: float | int | None, hours: float | int | None) -> float | None:
    h = _num(hours)
    if h <= 0 or tips is None:
        return None
    return _num(tips) / h


def calculate_base_plus_tips(base_pay: float | int | None, tips: float | int | None) -> float | None:
    if base_pay is None and tips is None:
        return None
    return _num(base_pay) + _num(tips)


def _recorded_tip_total(checks: Iterable[Any]) -> float:
    return sum(_num(tip) for tip in (check_tip_value(check) for check in checks) if tip is not None)


def _pending_tip_estimate(checks: Iterable[Any], rate: float) -> float:
    pending = 0.0
    for check in checks:
        if _status(check) != "open":
            continue
        total = _num(_value(check, "total", default=0))
        if total > 0:
            pending += total * rate
    return pending


def _bar_drink_count(checks: Iterable[Any]) -> float:
    count = 0.0
    for check in checks:
        for item in _items(check):
            if _item_kind(item) == "cocktail":
                count += _item_qty(item)
    return count


def calculate_day_stats(day_or_tables: Any, *, pending_tip_rate: float = 0.18, hours: float | None = None, base_pay: float | None = None) -> dict[str, Any]:
    checks = flatten_checks(day_or_tables)
    tables = _value(day_or_tables, "tables", default=day_or_tables) or []
    total_checks = len(checks)
    open_checks = sum(1 for check in checks if _status(check) == "open")
    closed_checks = sum(1 for check in checks if _status(check) == "closed")
    sales = sum(_num(_value(check, "total", default=0)) for check in checks)
    recorded_tips = _recorded_tip_total(checks)
    pending_tips = _pending_tip_estimate(checks, pending_tip_rate)
    covers = sum(int(_num(_value(check, "covers", default=0))) for check in checks)
    avg_check = sales / total_checks if total_checks else None
    drink_attach = calculate_drink_attach(checks)
    dessert_attach = calculate_dessert_attach(checks)
    tips_per_hour = calculate_tips_per_hour(recorded_tips + pending_tips, hours)

    drink_waits = [
        _time_delta_minutes(_value(check, "drinks_fired_at", "drinks", default=None), _value(check, "seated_at", "seated", default=None))
        for check in checks
    ]
    kitchen_waits = [
        _time_delta_minutes(_value(check, "kitchen_fired_at", "kitchen", default=None), _value(check, "seated_at", "seated", default=None))
        for check in checks
    ]
    table_turns = [
        _time_delta_minutes(_value(check, "closed_at", "closed", default=None), _value(check, "seated_at", "seated", default=None))
        for check in checks
        if _status(check) == "closed"
    ]

    return {
        "total_checks": total_checks,
        "total_tables": len(list(tables)),
        "open_checks": open_checks,
        "closed_checks": closed_checks,
        "sales": round(sales, 2),
        "recorded_tips": round(recorded_tips, 2),
        "pending_estimated_tips": round(pending_tips, 2),
        "covers": covers,
        "average_check": round(avg_check, 2) if avg_check is not None else None,
        "drink_attach_pct": round(drink_attach, 1) if drink_attach is not None else None,
        "bar_drink_count": _bar_drink_count(checks),
        "dessert_attach_pct": round(dessert_attach, 1) if dessert_attach is not None else None,
        "seat_to_first_drink_avg_min": round(avg, 1) if (avg := average(drink_waits)) is not None else None,
        "seat_to_kitchen_fire_avg_min": round(avg, 1) if (avg := average(kitchen_waits)) is not None else None,
        "average_table_turn_min": round(avg, 1) if (avg := average(table_turns)) is not None else None,
        "tips_per_hour": round(tips_per_hour, 2) if tips_per_hour is not None else None,
        "base_plus_tips": round(total, 2) if (total := calculate_base_plus_tips(base_pay, recorded_tips + pending_tips)) is not None else None,
    }


def calculate_range_stats(days_or_tables: Iterable[Any], *, pending_tip_rate: float = 0.18, hours: float | None = None, base_pay: float | None = None) -> dict[str, Any]:
    all_tables = []
    for day in days_or_tables or []:
        all_tables.extend(_value(day, "tables", default=day) or [])
    return calculate_day_stats(all_tables, pending_tip_rate=pending_tip_rate, hours=hours, base_pay=base_pay)


def map_toast_checks_to_floor_data(toast_checks: Iterable[Mapping[str, Any]]) -> list[EmployeeFloorCheck]:
    """Future Toast adapter placeholder.

    The live app already exposes sanitized employee table timelines elsewhere.
    When this console receives raw Toast-shaped check data, map only employee-
    safe fields here: check status, covers, totals, item names/kinds, payment
    method, and service timestamps. Do not pass card details, customer data, raw
    Toast ids, or manager-only fields through this adapter.
    """
    mapped: list[EmployeeFloorCheck] = []
    for row in toast_checks or []:
        mapped.append(EmployeeFloorCheck(
            id=row.get("display_number") or row.get("id") or "",
            status="closed" if row.get("closed_at") else "open",
            covers=int(_num(row.get("covers"), 0)),
            total=_num(row.get("total"), 0.0),
            tip=row.get("tip"),
            payment_method=str(row.get("payment_method") or "unknown").lower(),  # type: ignore[arg-type]
            seated_at=row.get("seated_at") or row.get("opened_at"),
            drinks_fired_at=row.get("drinks_fired_at") or row.get("drink_rang_at"),
            kitchen_fired_at=row.get("kitchen_fired_at") or row.get("food_rang_at"),
            closed_at=row.get("closed_at"),
        ))
    return mapped
