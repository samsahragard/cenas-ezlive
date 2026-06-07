"""Projected earnings for driver-facing delivery cards.

This is the estimate shown before payroll is finalized. It assumes the
delivery will be tracked, so under-threshold routes show the $35 minimum
($25 base + $10 tracked bonus), while routes over 20 miles add the mileage
bonus.
"""
from __future__ import annotations

from app.models import Order
from app.services.ezcater_payroll import (
    BASE_PER_DELIVERY,
    BONUS_TRACKED,
    MILES_THRESHOLD,
    PER_MILE_OVER_20,
)


def projected_driver_pay(order: Order) -> float:
    miles = order.pickup_miles or 0.0
    extra_miles = max(0.0, miles - MILES_THRESHOLD)
    bonus_miles = round(extra_miles * PER_MILE_OVER_20, 2)
    return round(BASE_PER_DELIVERY + BONUS_TRACKED + bonus_miles, 2)
