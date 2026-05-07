# app/domain/rules_utils.py
from __future__ import annotations
from typing import Optional
from app.domain.schemas import KitchenLineItem


def oz_to_lb(oz: float) -> float:
    # keep as a utility; centralized conversion
    return round(oz / 16.0, 2)
  
def make_weight_line(
    name: str,
    total_oz: float,
    per_qty: float | None = None,
    container: str = "none",
) -> KitchenLineItem:
    total_oz = round(float(total_oz), 1)
    total_lb = round(total_oz / 16.0, 2)

    return {
        "name": name,
        "measure_type": "weight",
        "per_qty": per_qty,
        "unit": "oz",
        "total": total_oz,
        "display_total": f"{total_lb} lb / {total_oz} oz",
        "container": container
    }

def individual_summary(qty: int, label: str) -> str:
    return f"Individual - {qty} {label}"

def make_count_line(
        name: str,
        total: float,
        unit: str,
        per_qty: float | None = None,
) -> KitchenLineItem:
    total_int = int(round(total))
    return {
        "name": name,
        "measure_type": "count",
        "per_qty": per_qty,
        "unit": unit,
        "total": total_int,
        "display_total": f"{total_int} {unit}",
        "container": None,
    }
