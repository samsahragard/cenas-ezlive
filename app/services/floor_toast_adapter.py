"""Cenas Floor OS - Toast adapter.

The Floor OS UI is fed by a single shape: a ``FloorDay`` dict containing
``EmployeeFloor*`` dataclasses (see ``employee_floor_metrics``). This module
maps Toast payloads onto that shape so the templates do not need to know
anything about Toast.

Status today:
- Live Toast integration is wired through ``app/services/toast_reports.py`` for
  manager surfaces. The employee-scoped reads use
  ``employee_table_timelines.server_table_timelines_for_guids`` and the
  ``CenaToastLink`` model for the (employee_id -> Toast employee GUID) join.
- This module is intentionally a thin, side-effect-free adapter: it does NOT
  fetch from Toast itself; the caller decides whether to call Toast (live mode)
  or the demo fixture (demo mode) and hands the rows into the mapper below.

What is NOT done yet (Toast TODOs):
1. Confirm exact field names for Toast's ``check`` payload as it reaches this
   service. Today's manager-facing helpers tend to flatten/rename fields - we
   need a thin, named contract before we trust the live path.
2. Confirm the per-item ``kind`` derivation. We currently rely on the menu
   category to bucket a line item into cocktail / nonAlcoholic / food / side /
   dessert. ``app/services/menu_kind.py`` (or equivalent) should own the
   category-to-kind table; until that is wired we degrade to ``food`` on
   unknowns so attach metrics undercount instead of overcounting.
3. Drink-fire and kitchen-fire timestamps: Toast exposes a per-item fire time
   but not a single check-level "drinks fired" marker. The adapter must pick
   the earliest drink-item fire as the drink-fire time, and the earliest
   non-drink-item fire as the kitchen-fire time. We carry ``None`` until then.
4. Sanitization. The employee surface MUST NOT see customer PII, card numbers,
   raw GUIDs, manager-only fields, or other employees' totals. The adapter
   strips everything not on the allow-list below.

Anything not implementable safely today returns ``None`` so the UI degrades to
honest empty states rather than fabricating numbers.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.services.employee_floor_metrics import (
    EmployeeFloorCheck,
    EmployeeFloorItem,
    EmployeeFloorTable,
    ItemKind,
    PaymentMethod,
)

# Allow-list of fields the adapter is permitted to read from a Toast check.
# Anything else (card details, customer name, etc) is dropped at the boundary.
_CHECK_ALLOW_LIST = frozenset({
    "id", "display_number", "status",
    "covers", "guest_count", "guests",
    "total", "subtotal",
    "tip",
    "payment_method", "pay",
    "seated_at", "opened_at",
    "drinks_fired_at", "drink_rang_at",
    "kitchen_fired_at", "food_rang_at",
    "closed_at",
    "items", "lines", "line_items",
    "table", "table_name", "table_id",
})

_ITEM_ALLOW_LIST = frozenset({
    "name", "display_name", "menu_name",
    "quantity", "qty", "q",
    "kind", "category", "menu_category",
    "fired_at",
})

# Coarse menu-category -> ItemKind. Production code should replace this with
# the canonical Cena menu table; we keep it conservative so attach metrics
# undercount on unknowns rather than overcounting.
_KIND_BY_CATEGORY = {
    "cocktail": "cocktail",
    "cocktails": "cocktail",
    "spirit": "cocktail",
    "spirits": "cocktail",
    "liquor": "cocktail",
    "wine": "cocktail",
    "beer": "cocktail",
    "na": "nonAlcoholic",
    "non-alcoholic": "nonAlcoholic",
    "non_alcoholic": "nonAlcoholic",
    "nonalcoholic": "nonAlcoholic",
    "softdrink": "nonAlcoholic",
    "soft_drink": "nonAlcoholic",
    "fountain": "nonAlcoholic",
    "coffee": "nonAlcoholic",
    "tea": "nonAlcoholic",
    "water": "nonAlcoholic",
    "dessert": "dessert",
    "desserts": "dessert",
    "sweet": "dessert",
    "side": "side",
    "sides": "side",
    "salsa": "side",
    "food": "food",
    "entree": "food",
    "lunch": "food",
    "dinner": "food",
    "appetizer": "food",
    "main": "food",
}


def _strip(mapping: Mapping[str, Any], allow: frozenset[str]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if k in allow}


def _coerce_payment(value: Any) -> PaymentMethod:
    label = str(value or "").strip().lower()
    if not label or label in {"unknown", "none"}:
        return "unknown"
    if label in {"cash"}:
        return "cash"
    if label in {"gift", "gift_card", "giftcard"}:
        return "gift"
    if label in {"credit", "card", "visa", "mastercard", "amex", "discover", "debit"}:
        return "credit"
    return "other"


def _coerce_kind(raw: Any) -> ItemKind:
    label = str(raw or "").strip().lower()
    if not label:
        return "food"
    if label in {"cocktail", "nonAlcoholic", "food", "side", "dessert"}:
        return label  # type: ignore[return-value]
    return _KIND_BY_CATEGORY.get(label, "food")  # type: ignore[return-value]


def map_toast_item_to_floor_item(row: Mapping[str, Any]) -> EmployeeFloorItem:
    """Map a single Toast line item -> EmployeeFloorItem. Drops anything outside
    the allow-list."""
    safe = _strip(row, _ITEM_ALLOW_LIST)
    name = str(safe.get("display_name") or safe.get("menu_name") or safe.get("name") or "Item")
    qty_raw = safe.get("quantity", safe.get("qty", safe.get("q", 1)))
    try:
        quantity = float(qty_raw) if qty_raw is not None else 1.0
    except (TypeError, ValueError):
        quantity = 1.0
    kind = _coerce_kind(safe.get("kind") or safe.get("category") or safe.get("menu_category"))
    return EmployeeFloorItem(
        name=name,
        quantity=quantity,
        kind=kind,
        fired_at=safe.get("fired_at"),
    )


def map_toast_check_to_floor_check(row: Mapping[str, Any]) -> EmployeeFloorCheck:
    """Map a single Toast check -> EmployeeFloorCheck."""
    safe = _strip(row, _CHECK_ALLOW_LIST)
    status_raw = str(safe.get("status") or "").lower()
    if status_raw in {"open", "closed"}:
        status = status_raw
    else:
        status = "closed" if safe.get("closed_at") else "open"

    covers_raw = safe.get("covers", safe.get("guest_count", safe.get("guests", 0)))
    try:
        covers = int(float(covers_raw or 0))
    except (TypeError, ValueError):
        covers = 0

    items_in = (safe.get("items") or safe.get("lines") or safe.get("line_items") or [])
    items = [map_toast_item_to_floor_item(it) for it in items_in if isinstance(it, Mapping)]

    return EmployeeFloorCheck(
        id=safe.get("display_number") or safe.get("id") or "",
        status=status,  # type: ignore[arg-type]
        covers=covers,
        total=float(safe.get("total") or safe.get("subtotal") or 0.0),
        # Cash-tip-unknown stays None. Do NOT coerce a missing tip to 0 -- the
        # employee surface treats None as "neutral / unknown" and 0 as "zero
        # tip", and those mean very different things on a cash-paid check.
        tip=safe.get("tip"),
        payment_method=_coerce_payment(safe.get("payment_method") or safe.get("pay")),
        seated_at=safe.get("seated_at") or safe.get("opened_at"),
        drinks_fired_at=safe.get("drinks_fired_at") or safe.get("drink_rang_at"),
        kitchen_fired_at=safe.get("kitchen_fired_at") or safe.get("food_rang_at"),
        closed_at=safe.get("closed_at"),
        items=items,
    )


def map_toast_checks_to_floor_day(
    toast_checks: Iterable[Mapping[str, Any]],
    *,
    label: str = "Today",
    sub: str = "",
    hours_worked: float | None = None,
    target_tips: float | None = None,
) -> dict[str, Any]:
    """Bucket Toast checks by table name -> FloorDay dict.

    Caller passes the Toast result. We:
      1. Strip every check through the allow-list (no PII, no card data).
      2. Group by table name.
      3. Return the FloorDay shape that the metrics + templates consume.
    """
    by_table: dict[str, list[EmployeeFloorCheck]] = {}
    for raw in toast_checks or []:
        if not isinstance(raw, Mapping):
            continue
        check = map_toast_check_to_floor_check(raw)
        table_name = str(raw.get("table_name") or raw.get("table") or raw.get("table_id") or "Open")
        by_table.setdefault(table_name, []).append(check)

    tables = [EmployeeFloorTable(name=name, checks=checks) for name, checks in by_table.items()]
    return {
        "label": label,
        "sub": sub,
        "source": "Toast live",
        "is_live": True,
        "hours_worked": hours_worked,
        "target_tips": target_tips,
        "tables": tables,
    }


def map_toast_employee_to_floor_profile(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map a Toast employee row -> the small dict the Floor OS templates use.
    Today this is a thin pass-through; the goal is to gate the field list."""
    name = str(row.get("display_name") or row.get("name") or "Team member")
    return {
        "name": name,
        "role": row.get("role"),
        "location": row.get("location"),
    }


def map_toast_shift_to_floor_shift(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map a Toast shift row -> the small dict the Floor OS Shifts tab uses."""
    return {
        "starts_at": row.get("starts_at") or row.get("start"),
        "ends_at": row.get("ends_at") or row.get("end"),
        "position_name": row.get("position_name") or row.get("position"),
        "hours": row.get("hours"),
        "status": row.get("status") or "scheduled",
    }
