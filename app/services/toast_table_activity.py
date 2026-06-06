"""Sanitized Toast table/check activity for owner-operator assistant tools."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback only
    ZoneInfo = None  # type: ignore[assignment]

from app.services.toast_client import ToastClient, restaurant_guids

log = logging.getLogger(__name__)

try:
    _CT = ZoneInfo("America/Chicago") if ZoneInfo else timezone(timedelta(hours=-5))
except Exception:  # pragma: no cover - depends on host tzdata package
    _CT = timezone(timedelta(hours=-5))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) >= 5 and text[-5] in "+-" and ":" not in text[-5:]:
        text = text[:-5] + text[-5:-2] + ":" + text[-2:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_ct(value: datetime) -> str:
    local = value.astimezone(_CT)
    hour = local.hour % 12 or 12
    return f"{local:%Y-%m-%d} {hour}:{local:%M} {local:%p} CT"


def normalize_location(location: str | None) -> str | None:
    value = str(location or "").strip().casefold()
    aliases = {
        "tomball": "tomball",
        "dos": "tomball",
        "dos mas": "tomball",
        "copperfield": "copperfield",
        "uno": "copperfield",
        "uno mas": "copperfield",
        "both": None,
        "all": None,
    }
    return aliases.get(value, value or None)


def _location_label(location: str | None) -> str:
    labels = {
        "tomball": "Tomball",
        "copperfield": "Copperfield",
    }
    return labels.get(location or "", "all locations")


def _table_guid(order: dict[str, Any]) -> str | None:
    table = order.get("table")
    if isinstance(table, dict):
        return str(table.get("guid") or "").strip() or None
    return None


def _table_name_from_order(order: dict[str, Any]) -> str | None:
    table = order.get("table")
    if isinstance(table, dict):
        for key in ("name", "tableNumber", "number"):
            value = str(table.get(key) or "").strip()
            if value:
                return value
    elif table:
        return str(table).strip() or None
    return None


def _table_name_map(client: Any, location: str, restaurant_guid: str) -> tuple[dict[str, str], bool]:
    try:
        tables = client.fetch_tables(location, restaurant_guid)
    except Exception:  # noqa: BLE001
        log.exception("toast table activity: table config fetch failed for %s", location)
        return {}, False
    table_map: dict[str, str] = {}
    for table in tables or []:
        if not isinstance(table, dict):
            continue
        guid = str(table.get("guid") or "").strip()
        name = str(table.get("name") or "").strip()
        if guid and name:
            table_map[guid] = name
    return table_map, True


def _employee_name(employee: dict[str, Any]) -> str | None:
    full = " ".join(
        str(employee.get(key) or "").strip()
        for key in ("firstName", "lastName")
        if str(employee.get(key) or "").strip()
    ).strip()
    if full:
        return full
    for key in ("name", "email"):
        value = str(employee.get(key) or "").strip()
        if value:
            return value
    return None


def _employee_name_map(client: Any, location: str, restaurant_guid: str) -> tuple[dict[str, str], bool]:
    try:
        employees = client.fetch_employees(location, restaurant_guid)
    except Exception:  # noqa: BLE001
        log.exception("toast table activity: employee fetch failed for %s", location)
        return {}, False
    employee_map: dict[str, str] = {}
    for employee in employees or []:
        if not isinstance(employee, dict):
            continue
        guid = str(employee.get("guid") or "").strip()
        name = _employee_name(employee)
        if guid and name:
            employee_map[guid] = name
    return employee_map, True


def _ref_guid(value: Any) -> str | None:
    if isinstance(value, dict):
        return str(value.get("guid") or "").strip() or None
    return None


def _ref_name(value: Any, employee_map: dict[str, str]) -> str | None:
    if not isinstance(value, dict):
        return None
    guid = _ref_guid(value)
    if guid and employee_map.get(guid):
        return employee_map[guid]
    return _employee_name(value)


def latest_table_activity_payload(
    location: str | None = None,
    *,
    client: Any | None = None,
    refresh_orders: bool = True,
    business_date: str | None = None,
) -> dict[str, Any]:
    """Return the latest in-store table open event from Toast orders.

    The payload intentionally excludes customer, check GUID, and raw table GUID
    values. For partner/operator sessions it includes the safe Toast employee
    display name tied to the order/check when available.
    """
    toast = client or ToastClient.shared()
    guids = restaurant_guids()
    requested_location = normalize_location(location)
    if requested_location:
        locations = {
            requested_location: guids[requested_location],
        } if requested_location in guids else {}
    else:
        locations = dict(guids)

    today_ct = _now_utc().astimezone(_CT)
    bd = business_date or today_ct.strftime("%Y%m%d")
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    orders_scanned = 0
    table_checks = 0

    for loc, guid in locations.items():
        table_map, table_config_available = _table_name_map(toast, loc, guid)
        employee_map, employee_lookup_available = _employee_name_map(toast, loc, guid)
        try:
            orders = toast.fetch_orders_for_date(loc, guid, bd, refresh=refresh_orders)
        except Exception:  # noqa: BLE001
            log.exception("toast table activity: order fetch failed for %s", loc)
            warnings.append(f"{_location_label(loc)} orders were unavailable.")
            continue
        orders_scanned += len(orders or [])
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            if order.get("voided") or order.get("deleted"):
                continue
            if str(order.get("source") or "").strip() != "In Store":
                continue
            table_guid = _table_guid(order)
            table_name = _table_name_from_order(order)
            if table_guid:
                table_name = table_map.get(table_guid) or table_name
            if not table_guid and not table_name:
                continue
            for check in order.get("checks") or []:
                if not isinstance(check, dict):
                    continue
                if check.get("voided") or check.get("deleted"):
                    continue
                opened_at = _parse_iso(check.get("openedDate")) or _parse_iso(order.get("openedDate"))
                if not opened_at:
                    continue
                opened_by_name = _ref_name(check.get("openedBy"), employee_map)
                server_name = _ref_name(
                    check.get("server") or order.get("server"),
                    employee_map,
                )
                table_checks += 1
                records.append({
                    "location": loc,
                    "location_label": _location_label(loc),
                    "opened_at": opened_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "opened_at_local": _format_ct(opened_at),
                    "table_name": table_name,
                    "table_label_available": bool(table_name),
                    "table_config_available": table_config_available,
                    "server_name": server_name,
                    "opened_by_name": opened_by_name,
                    "employee_lookup_available": employee_lookup_available,
                    "display_number": str(check.get("displayNumber") or order.get("displayNumber") or "").strip() or None,
                })

    records.sort(key=lambda row: str(row.get("opened_at") or ""), reverse=True)
    latest = records[0] if records else None
    location_label = _location_label(requested_location)
    return {
        "generated_at": _now_iso(),
        "data_class": "toast_table_activity_sanitized",
        "business_date": bd,
        "location": requested_location or "all",
        "location_label": location_label,
        "latest": latest,
        "counts": {
            "orders_scanned": orders_scanned,
            "in_store_table_checks": table_checks,
        },
        "warnings": warnings,
    }
