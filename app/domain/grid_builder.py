# build table-like for frontend
from __future__ import annotations

import re
from typing import TypedDict, List, Dict

from app.domain.master_sheet_map import VIEW_ROWS, RowSpec, FlatMap


class GridColumn(TypedDict):
    order_id: str
    values: FlatMap


class ViewGrid(TypedDict):
    view: str
    rows: List[RowSpec]
    columns: List[GridColumn]

def _row_has_any_value(row_key: str, columns: list[GridColumn]) -> bool:
    for col in columns:
        value = col["values"].get(row_key, "")
        if value not in ("", None):
            return True
    return False

def _filter_empty_rows(rows: list[RowSpec], columns: list[GridColumn]) -> list[RowSpec]:
    return [row for row in rows if _row_has_any_value(row["key"], columns)]


def _compute_row_totals(rows: list[RowSpec], columns: list[GridColumn]) -> FlatMap:
    totals: FlatMap = {"meta.order_id": "Total"}
    for row in rows:
        key = row["key"]
        total = 0.0
        found = False
        for col in columns:
            raw = str(col["values"].get(key, "") or "").split("|")[0].strip()
            if not raw:
                continue
            try:
                total += float(raw)
                found = True
            except ValueError:
                if row.get("section") != "Sides":
                    continue
                side_total = 0.0
                side_found = False
                for part in raw.split("+"):
                    match = re.match(r"\s*(\d+(?:\.\d+)?)\b", part)
                    if match:
                        side_total += float(match.group(1))
                        side_found = True
                if side_found:
                    total += side_total
                    found = True
        if found:
            if total == int(total):
                totals[key] = str(int(total))
            else:
                totals[key] = f"{total:.2f}".rstrip("0").rstrip(".")
    return totals


def build_view_grid(
    view_name: str,
    per_order_outputs: list[dict[str, object]],
    collapse_empty_rows: bool = False,
) -> ViewGrid:
    rows = VIEW_ROWS[view_name]
    columns: list[GridColumn] = []

    for order_bundle in per_order_outputs:
        order_id = str(order_bundle["order_id"])
        views = order_bundle["views"]
        values = views[view_name]

        columns.append({
            "order_id": order_id,
            "values": values,
        })

    if collapse_empty_rows:
        rows = _filter_empty_rows(rows, columns)

    if view_name == "kitchen":
        total_values = _compute_row_totals(rows, columns)
        columns = [{"order_id": "Total", "values": total_values}] + columns

    return {
        "view": view_name,
        "rows": rows,
        "columns": columns,
    }       


def build_all_view_grids(
    per_order_outputs: list[dict[str, object]],
    collapse_empty_rows: bool = False,
) -> dict[str, ViewGrid]:
    return {
        "master": build_view_grid("master", per_order_outputs, collapse_empty_rows=collapse_empty_rows),
        "kitchen": build_view_grid("kitchen", per_order_outputs, collapse_empty_rows=collapse_empty_rows),
        "driver": build_view_grid("driver", per_order_outputs, collapse_empty_rows=collapse_empty_rows),
        "prep_expo": build_view_grid("prep_expo", per_order_outputs, collapse_empty_rows=collapse_empty_rows),
    }
