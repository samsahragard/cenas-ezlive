"""Phase 0 / Block 5 — smoke tests for app.services.ezcater_payroll.

Verifies the per-delivery pay formula handles every relevant combination
of tracked/untracked × under/over 20 miles × 5-star yes/no, plus the
bi-weekly period boundary math (anchor + 14-day rollover).
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import ezcater_payroll as payroll


def _order(tracking_status=None, pickup_miles=None):
    """Build a minimal SimpleNamespace that quacks like Order for compute_one."""
    return SimpleNamespace(
        external_order_id="X",
        delivery_date="2026-05-13",
        tracking_status=tracking_status,
        pickup_miles=pickup_miles,
    )


# ---- pay formula combinations ----

def test_untracked_pays_base_only():
    pay = payroll.compute_one(_order(tracking_status=None, pickup_miles=50), five_star=False)
    assert pay.total == payroll.BASE_PER_DELIVERY  # $25 base only


def test_tracked_under_20_miles_pays_base_plus_tracked():
    pay = payroll.compute_one(_order(tracking_status="Tracked", pickup_miles=10), five_star=False)
    assert pay.total == payroll.BASE_PER_DELIVERY + payroll.BONUS_TRACKED  # $35


def test_tracked_at_exactly_20_miles_no_bonus_yet():
    pay = payroll.compute_one(_order(tracking_status="Tracked", pickup_miles=20), five_star=False)
    assert pay.total == payroll.BASE_PER_DELIVERY + payroll.BONUS_TRACKED


def test_tracked_over_20_miles_adds_per_mile_bonus():
    # 25 miles - 20 threshold = 5 extra × $1.50 = $7.50
    pay = payroll.compute_one(_order(tracking_status="Tracked", pickup_miles=25), five_star=False)
    expected = payroll.BASE_PER_DELIVERY + payroll.BONUS_TRACKED + (5 * payroll.PER_MILE_OVER_20)
    assert pay.total == expected


def test_tracked_plus_five_star_adds_5():
    pay = payroll.compute_one(_order(tracking_status="Tracked", pickup_miles=10), five_star=True)
    expected = payroll.BASE_PER_DELIVERY + payroll.BONUS_TRACKED + payroll.FIVE_STAR_BONUS
    assert pay.total == expected


def test_untracked_five_star_no_bonus():
    """5-star only counts when also tracked (SPEC.md §payroll-rules)."""
    pay = payroll.compute_one(_order(tracking_status=None, pickup_miles=10), five_star=True)
    assert pay.total == payroll.BASE_PER_DELIVERY  # base only, no 5-star bonus


def test_ezcater_route_history_counts_as_tracked_and_auto_fills_driven_miles():
    route = SimpleNamespace(
        point_count=3,
        distance_miles=26.4,
        duration_minutes=42,
        started_at=None,
        ended_at=None,
    )
    pay = payroll.compute_one(_order(tracking_status=None, pickup_miles=25), route_summary=route)

    assert pay.bonus_tracked == payroll.BONUS_TRACKED
    assert pay.verified_miles == 5.0
    assert pay.driven_miles == 6.4
    assert pay.driven_miles_source == "ezcater"
    assert pay.route_miles == 26.4
    assert pay.route_minutes == 42
    assert pay.route_point_count == 3


def test_uploaded_driver_photos_surface_as_payroll_files():
    order = _order(tracking_status="Tracked", pickup_miles=10)
    order.setup_photo_url = "/static/uploads/driver_orders/1/2/delivery.jpg"
    order.parking_photo_url = "/static/uploads/driver_orders/1/2/parking.jpg"
    order.parking_cost = 12.5

    pay = payroll.compute_one(order)

    assert pay.files == [
        {
            "label": "Photo",
            "url": "/static/uploads/driver_orders/1/2/delivery.jpg",
            "title": "Delivery setup/proof photo",
        },
        {
            "label": "Receipt",
            "url": "/static/uploads/driver_orders/1/2/parking.jpg",
            "title": "Parking receipt ($12.50)",
        },
    ]


def test_manager_payroll_template_includes_uploaded_file_links():
    template = Path("app/templates/partials/_paycheck_periods.html").read_text(encoding="utf-8")

    assert "<th>Files</th>" in template
    assert "{% if d.files %}" in template
    assert 'href="{{ file.url }}"' in template
    assert "{{ file.label }}" in template


# ---- period math ----

def test_period_containing_anchor_returns_anchor():
    start, end, check = payroll.period_containing(payroll.ANCHOR_START)
    assert start == payroll.ANCHOR_START
    assert end == payroll.ANCHOR_START + timedelta(days=13)


def test_period_containing_advances_by_14():
    """Day 14 of the anchor period is the start of period 2."""
    next_start = payroll.ANCHOR_START + timedelta(days=14)
    start, end, _ = payroll.period_containing(next_start)
    assert start == next_start


def test_check_date_is_5_days_after_period_end():
    start, end, check = payroll.period_containing(payroll.ANCHOR_START)
    assert check == end + timedelta(days=payroll.CHECK_OFFSET_DAYS)


def test_previous_period_walks_back_14_days():
    start, end, _ = payroll.period_containing(payroll.ANCHOR_START + timedelta(days=14))
    prev_start, _, _ = payroll.previous_period(start)
    assert prev_start == payroll.ANCHOR_START
