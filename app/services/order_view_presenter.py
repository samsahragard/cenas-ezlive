from __future__ import annotations

import re


_EMPTY_DISPLAY_VALUES = {"", "N/A", "-", "\u2014", "0", "0.0", "0.00"}
_NUMERIC_DISPLAY_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_NUMBER_TOKEN_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _display_value(raw: object) -> str:
    if raw is None:
        return ""
    return str(raw).strip()


def _has_display_value(raw: object) -> bool:
    return _display_value(raw) not in _EMPTY_DISPLAY_VALUES


def _is_numeric_display_value(raw: object) -> bool:
    value = _display_value(raw).replace(",", "")
    return bool(_NUMERIC_DISPLAY_RE.fullmatch(value))


def _print_value_parts(raw: object) -> list[dict[str, str]]:
    value = _display_value(raw)
    if not value:
        return []
    parts: list[dict[str, str]] = []
    cursor = 0
    for match in _NUMBER_TOKEN_RE.finditer(value):
        if match.start() > cursor:
            parts.append({"kind": "text", "text": value[cursor:match.start()]})
        parts.append({"kind": "number", "text": match.group(0)})
        cursor = match.end()
    if cursor < len(value):
        parts.append({"kind": "text", "text": value[cursor:]})
    if not parts:
        parts.append({"kind": "text", "text": value})
    return parts


def _print_density(fields: list[dict[str, object]]) -> str:
    printable_rows = [field for field in fields if field["key"] not in {
        "meta.store_origin",
        "meta.client",
        "meta.ask_for",
        "meta.phone",
        "meta.address",
    }]
    text_weight = sum(
        max(0, len(_display_value(field.get("value"))) - 12) // 14
        for field in printable_rows
        if not bool(field.get("is_numeric"))
    )
    score = len(printable_rows) + text_weight
    if score >= 30:
        return "tight"
    if score >= 22:
        return "compact"
    return "normal"


def _dropdown_driver_label(
    order_id: str,
    values: dict[str, object],
    header_driver_by_order: dict[str, object] | None,
) -> str:
    if header_driver_by_order is not None and order_id in header_driver_by_order:
        return _display_value(header_driver_by_order.get(order_id)) or "no driver"
    return _display_value(values.get("meta.ezcater_driver", "")) or "no driver"


def _card_header_fields(
    values: dict[str, object],
    order_id: str,
    header_driver_by_order: dict[str, object] | None = None,
) -> list[str]:
    fields = []
    kitchen_ready = _display_value(values.get("meta.kitchen_ready", ""))
    if _has_display_value(kitchen_ready):
        fields.append(kitchen_ready)
    fields.append(_dropdown_driver_label(order_id, values, header_driver_by_order))
    return fields


def build_combined_order_card_views(
    grids: dict[str, object],
    *,
    header_driver_by_order: dict[str, object] | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Convert combined day grids into sparse per-order cards for the web view.

    The XLSX export keeps the dense matrix. The browser page is easier to read
    as one card per order, with rows removed only when that card has no value.
    Column order is preserved because build_grids_for_orders already sorts
    bundles by dispatch/kitchen timing.
    """
    views: dict[str, list[dict[str, object]]] = {}
    for view_name, grid in grids.items():
        rows = list(grid["rows"])
        cards: list[dict[str, object]] = []
        for col in grid["columns"]:
            order_id = str(col["order_id"])
            if order_id == "Total":
                continue
            values = dict(col["values"])
            fields: list[dict[str, object]] = []
            for row in rows:
                key = str(row["key"])
                value = _display_value(values.get(key, ""))
                if not _has_display_value(value):
                    continue
                if key == "meta.order_id" and value == order_id:
                    continue
                fields.append({
                    "key": key,
                    "label": str(row["label"]),
                    "section": str(row["section"]),
                    "value": value,
                    "is_numeric": _is_numeric_display_value(value),
                    "print_parts": _print_value_parts(value),
                })
            cards.append({
                "order_id": order_id,
                "header_fields": _card_header_fields(values, order_id, header_driver_by_order),
                "fields": fields,
                "print_density": _print_density(fields),
            })
        views[view_name] = cards
    return views
