"""Phase 0 / Block 5 — smoke tests for app.domain.delivery_timing.

The route-planning math is pure (no Google API hits) once you pass it the
already-resolved drive minutes. We verify the structural guards (different
origin store / different date → infeasible) plus a feasible vs not-feasible
case for the two-stop math.
"""
from __future__ import annotations

import pytest

from app.domain.delivery_timing import compute_best_two_stop_route


def _common(**overrides):
    """Base set of plausible two-stop args; override individual fields per test."""
    args = dict(
        origin_store_id_a="store_2",
        origin_store_id_b="store_2",
        date_str_a="2026-05-13",
        date_str_b="2026-05-13",
        order_a_id="A",
        order_a_deliver_at="12:00 PM",
        order_a_window_start="11:30 AM",
        store_to_a_minutes=15,
        order_b_id="B",
        order_b_deliver_at="12:30 PM",
        order_b_window_start="12:00 PM",
        store_to_b_minutes=20,
        a_to_b_minutes=15,
        b_to_a_minutes=15,
    )
    args.update(overrides)
    return args


def test_different_origin_stores_not_feasible():
    result = compute_best_two_stop_route(**_common(
        origin_store_id_a="store_1",
        origin_store_id_b="store_2",
    ))
    assert result["feasible"] is False
    assert "different origin stores" in result["flags"]


def test_different_dates_not_feasible():
    result = compute_best_two_stop_route(**_common(
        date_str_a="2026-05-13",
        date_str_b="2026-05-14",
    ))
    assert result["feasible"] is False
    assert "different delivery dates" in result["flags"]


def test_feasible_pair_returns_two_stops():
    result = compute_best_two_stop_route(**_common())
    assert result["feasible"] is True
    assert len(result["stops"]) == 2
    assert result["stops"][0]["minutes_late"] == 0
    assert result["stops"][1]["minutes_late"] == 0
    assert result["depart_store_at"]  # non-empty
    assert result["pickup_at"]


def test_infeasible_when_second_stop_too_far_after_deadline():
    # Drive A->B = 600 min; second stop's deadline is 30 min after first.
    # Result should still come back structured, just feasible=False with
    # a 'late' flag.
    result = compute_best_two_stop_route(**_common(
        a_to_b_minutes=600,
        b_to_a_minutes=600,
    ))
    assert result["feasible"] is False
    # At least one of the two routes (A->B or B->A) must surface lateness.
    assert any("late" in f for f in result["flags"])


def test_route_picks_better_direction_when_one_is_feasible():
    """A->B fast, B->A slow — expect the optimizer to return the A->B order."""
    result = compute_best_two_stop_route(**_common(
        a_to_b_minutes=10,    # fast forward
        b_to_a_minutes=240,   # slow reverse
    ))
    assert result["feasible"] is True
    # The first stop in the returned plan should be A (lower drive total wins)
    assert result["stops"][0]["order_id"] == "A"
