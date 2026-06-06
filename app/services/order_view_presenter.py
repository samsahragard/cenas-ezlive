from __future__ import annotations


_EMPTY_DISPLAY_VALUES = {"", "N/A", "-", "\u2014", "0", "0.0", "0.00"}


def _display_value(raw: object) -> str:
    if raw is None:
        return ""
    return str(raw).strip()


def _has_display_value(raw: object) -> bool:
    return _display_value(raw) not in _EMPTY_DISPLAY_VALUES


def _card_header_fields(values: dict[str, object]) -> list[str]:
    fields = []
    for key in ("meta.deliver_at", "meta.client", "meta.kitchen_ready", "meta.driver"):
        value = _display_value(values.get(key, ""))
        if value and value not in fields:
            fields.append(value)
    return fields


def build_combined_order_card_views(grids: dict[str, object]) -> dict[str, list[dict[str, object]]]:
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
            fields: list[dict[str, str]] = []
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
                })
            cards.append({
                "order_id": order_id,
                "header_fields": _card_header_fields(values),
                "fields": fields,
            })
        views[view_name] = cards
    return views
