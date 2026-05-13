"""Phase 0 / Block 5 — smoke tests for app.services.driver_scoring.

Focuses on the pure tier-mapping function. The DB-backed
compute_driver_score is exercised indirectly via a single happy-path
end-to-end test using the in-memory fixture from conftest.
"""
from __future__ import annotations

import pytest

from app.services import driver_scoring as scoring


# ---- pure tier math ----

@pytest.mark.parametrize("score, lifetime, expected", [
    # Below lifetime floor → always "new" regardless of score
    (100, 0,  scoring.TIER_NEW),
    (100, 19, scoring.TIER_NEW),
    (60,  10, scoring.TIER_NEW),
    # At/above lifetime floor → score-based mapping
    (0,   20, scoring.TIER_NEW),
    (59,  20, scoring.TIER_NEW),
    (60,  20, scoring.TIER_TRUSTED),
    (79,  20, scoring.TIER_TRUSTED),
    (80,  20, scoring.TIER_ROCKSTAR),
    (94,  20, scoring.TIER_ROCKSTAR),
    (95,  20, scoring.TIER_TOP_ROCKSTAR),
    (100, 999, scoring.TIER_TOP_ROCKSTAR),
])
def test_compute_tier_boundaries(score, lifetime, expected):
    assert scoring.compute_tier(score, lifetime) == expected


def test_metric_max_weights_sum_to_100():
    """Guards SPEC.md §8 — total points must be 100."""
    total = (
        scoring.TRACKING_MAX
        + scoring.ON_TIME_MAX
        + scoring.CANCELLATION_MAX
        + scoring.PHOTO_MAX
        + scoring.RESPONSE_MAX
        + scoring.STAR_MAX
    )
    assert total == 100


def test_lifetime_floor_constant():
    """Hard-coded floor of 20 lifetime deliveries — locks in SPEC value."""
    assert scoring.NEW_TIER_LIFETIME_FLOOR == 20


# ---- happy-path full compute on a brand-new driver ----

def test_compute_driver_score_brand_new_driver(db_session):
    """A driver with no deliveries / no data → full credit on each
    metric (we don't punish absence of evidence), but tier still
    pins to 'new' because lifetime is below the floor."""
    from datetime import date, timedelta
    from app.models import Driver

    d = Driver(name="Test Driver", location="tomball", lifetime_delivery_count=0)
    db_session.add(d)
    db_session.commit()

    today = date.today()
    breakdown = scoring.compute_driver_score(
        db_session, d, ws=today - timedelta(days=30), we=today
    )
    assert breakdown.total == 100, f"expected 100 (full credit on zero data), got {breakdown.total}"
    assert breakdown.tier == scoring.TIER_NEW, "lifetime floor must win"
