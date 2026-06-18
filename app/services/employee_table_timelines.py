"""Employee-scoped Toast table/check timelines.

Own-view only: every live query starts from a confirmed CenaToastLink and every
profile-DB read starts from cena_employee_<id>.sqlite. The public payload omits
raw Toast ids, customer data, card details, prices, sales/check totals, taxes,
discounts, and tip amounts.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.services import toast_reports
from app.services.toast_webhook_store import DEFAULT_EMPLOYEE_PROFILE_DB_DIR

log = logging.getLogger(__name__)


def central_business_dates() -> tuple[str, str, str]:
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    yesterday = today - timedelta(days=1)
    return today.strftime("%Y%m%d"), yesterday.strftime("%Y%m%d"), today.isoformat()


def _day_business_date(day: str | None) -> tuple[str, str]:
    today_bd, yesterday_bd, _ = central_business_dates()
    requested = (day or "today").strip().lower()
    if requested in {"yesterday", "last night"}:
        return "yesterday", yesterday_bd
    return "today", today_bd


def _profile_db_path(cena_employee_id: int, profile_dir: str | os.PathLike[str] | None = None) -> Path:
    base = Path(profile_dir or os.getenv("TOAST_EMPLOYEE_PROFILE_DB_DIR") or DEFAULT_EMPLOYEE_PROFILE_DB_DIR)
    return base / f"cena_employee_{int(cena_employee_id)}.sqlite"


def _connect_profile(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _iso_or_none(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if dt.tzinfo is None:
        return dt.isoformat()
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _seconds_between_iso(later: str | None, earlier: str | None) -> int | None:
    if not later or not earlier:
        return None
    try:
        hi = datetime.fromisoformat(str(later).replace("Z", "+00:00"))
        lo = datetime.fromisoformat(str(earlier).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int(round((hi - lo).total_seconds())))


def _payment_method_from_scalar(value: str | None) -> str | None:
    return toast_reports._payment_method_type({"type": value})


def _payment_status_from_scalar(value: str | None) -> str | None:
    return toast_reports._payment_status({"paymentStatus": value})


def _selection_group_from_name(name: str | None) -> str:
    return toast_reports._selection_group({"displayName": name or ""}, {})


def _safe_json(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _profile_table_timelines(
    cena_employee_id: int,
    business_date: str,
    *,
    profile_dir: str | os.PathLike[str] | None = None,
    limit: int = 20,
) -> dict[str, Any] | None:
    path = _profile_db_path(cena_employee_id, profile_dir)
    if not path.exists():
        return None

    with _connect_profile(path) as conn:
        orders = conn.execute(
            """
            SELECT order_guid, store_key, business_date, table_name, opened_date,
                   closed_date, paid_date, payment_status
            FROM related_order_current
            WHERE business_date = ?
            ORDER BY COALESCE(opened_date, paid_date, closed_date) DESC
            """,
            (business_date,),
        ).fetchall()
        order_guids = [row["order_guid"] for row in orders if row["order_guid"]]
        if not order_guids:
            return {
                "ok": True,
                "source": "profile_db",
                "business_date": business_date,
                "day": "history",
                "tickets": 0,
                "timelines": [],
                "raw_payloads_included": False,
            }
        placeholders = ",".join("?" for _ in order_guids)
        checks = conn.execute(
            f"""
            SELECT check_guid, order_guid, store_key, display_number, payment_status,
                   opened_date, closed_date, paid_date, voided, deleted
            FROM related_check_current
            WHERE order_guid IN ({placeholders}) AND business_date = ?
            ORDER BY COALESCE(opened_date, paid_date, closed_date) DESC, display_number
            """,
            (*order_guids, business_date),
        ).fetchall()
        selections = conn.execute(
            f"""
            SELECT check_guid, order_guid, display_name, quantity, voided
            FROM related_selection_current
            WHERE order_guid IN ({placeholders}) AND business_date = ?
            ORDER BY order_guid, check_guid, display_name
            """,
            (*order_guids, business_date),
        ).fetchall()
        payments = conn.execute(
            f"""
            SELECT check_guid, order_guid, payment_type, payment_status, paid_date
            FROM related_payment_current
            WHERE order_guid IN ({placeholders}) AND business_date = ?
            ORDER BY order_guid, check_guid, paid_date
            """,
            (*order_guids, business_date),
        ).fetchall()
        facts = conn.execute(
            """
            SELECT fact_type, order_guid, check_guid, occurred_at, summary_json
            FROM toast_fact
            WHERE cena_employee_id = ? AND business_date = ?
              AND fact_type IN ('item_added', 'payment_added', 'check_closed')
            ORDER BY occurred_at
            """,
            (int(cena_employee_id), business_date),
        ).fetchall()

    order_by_guid = {row["order_guid"]: row for row in orders}
    selection_times: dict[tuple[str | None, str | None, str], list[str]] = {}
    for fact in facts:
        if fact["fact_type"] != "item_added":
            continue
        summary = _safe_json(fact["summary_json"])
        name = str(summary.get("name") or "").strip()
        if not name:
            continue
        key = (fact["order_guid"], fact["check_guid"], name.casefold())
        selection_times.setdefault(key, []).append(_iso_or_none(fact["occurred_at"]) or "")

    selections_by_check: dict[str | None, list[dict[str, Any]]] = {}
    for row in selections:
        if row["voided"]:
            continue
        name = str(row["display_name"] or "").strip()
        if not name:
            continue
        key = (row["order_guid"], row["check_guid"], name.casefold())
        created_at = None
        if selection_times.get(key):
            created_at = selection_times[key].pop(0) or None
        try:
            qty = float(row["quantity"] or 1)
        except (TypeError, ValueError):
            qty = 1.0
        selections_by_check.setdefault(row["check_guid"], []).append({
            "name": name,
            "quantity": qty,
            "created_at": created_at,
            "group": _selection_group_from_name(name),
        })

    payments_by_check: dict[str | None, list[dict[str, Any]]] = {}
    seen_payment: set[tuple[str | None, str | None, str | None, str | None]] = set()
    for row in payments:
        method = _payment_method_from_scalar(row["payment_type"])
        if not method:
            continue
        status = _payment_status_from_scalar(row["payment_status"])
        key = (row["check_guid"], row["order_guid"], method, status)
        if key in seen_payment:
            continue
        seen_payment.add(key)
        payments_by_check.setdefault(row["check_guid"], []).append({
            "method": method,
            "status": status,
            "paid_at": _iso_or_none(row["paid_date"]),
        })

    timelines: list[dict[str, Any]] = []
    for check in checks:
        if check["voided"] or check["deleted"]:
            continue
        order = order_by_guid.get(check["order_guid"])
        if not order:
            continue
        check_selections = selections_by_check.get(check["check_guid"], [])
        opened_at = _iso_or_none(check["opened_date"] or order["opened_date"])
        closed_at = _iso_or_none(check["closed_date"] or order["closed_date"])
        drink_at = next((s.get("created_at") for s in check_selections if s.get("group") == "drink" and s.get("created_at")), None)
        food_at = next((s.get("created_at") for s in check_selections if s.get("group") == "food" and s.get("created_at")), None)
        timelines.append({
            "location": order["store_key"] or check["store_key"],
            "table_name": order["table_name"],
            "display_number": check["display_number"],
            "status": "closed" if closed_at else "open",
            "opened_at": opened_at,
            "closed_at": closed_at,
            "duration_secs": _seconds_between_iso(closed_at, opened_at),
            "drink_rang_at": drink_at,
            "food_rang_at": food_at,
            "selections": check_selections,
            "selection_groups": toast_reports._selection_counts(check_selections),
            "payment_methods": payments_by_check.get(check["check_guid"], []),
        })

    timelines.sort(key=lambda row: row.get("opened_at") or "", reverse=True)
    return {
        "ok": True,
        "source": "profile_db",
        "business_date": business_date,
        "tickets": len(timelines),
        "timelines": timelines[:max(0, int(limit or 0))],
        "raw_payloads_included": False,
    }


def employee_table_timelines_payload(
    cena_employee_id: int,
    links,
    *,
    day: str | None = "today",
    limit: int = 20,
    profile_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    day_key, business_date = _day_business_date(day)
    guids = {link.toast_id for link in links if getattr(link, "toast_id", None)}
    stores = {
        (link.store_key or "").strip().lower()
        for link in links
        if (link.store_key or "").strip()
    }
    loc_filter = next(iter(stores)) if len(stores) == 1 else None

    if day_key != "today":
        profile_payload = _profile_table_timelines(
            cena_employee_id,
            business_date,
            profile_dir=profile_dir,
            limit=limit,
        )
        if profile_payload is not None and profile_payload.get("tickets", 0) > 0:
            profile_payload["day"] = day_key
            profile_payload["used_profile_db"] = True
            return profile_payload

    try:
        live = toast_reports.server_table_timelines_for_guids(
            guids,
            loc_filter,
            business_date,
            refresh=(day_key == "today"),
            limit=limit,
        )
    except Exception:
        log.warning(
            "employee tables: Toast timeline fallback failed for employee %s day=%s",
            cena_employee_id,
            day_key,
            exc_info=True,
        )
        return {
            "ok": True,
            "day": day_key,
            "business_date": business_date,
            "source": "toast_fallback_error" if day_key != "today" else "toast_live_error",
            "used_profile_db": False,
            "tickets": 0,
            "timelines": [],
            "raw_payloads_included": False,
        }
    live = dict(live)
    live["ok"] = True
    live["day"] = day_key
    live["source"] = "toast_live" if day_key == "today" else "toast_fallback"
    live["used_profile_db"] = False
    return live
