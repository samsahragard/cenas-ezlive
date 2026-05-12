"""Driver Rockstar scoring + tier computation.

Per SPEC.md §8-9: rolling 30-day, 6-metric score totalling 100 points.
Nightly recompute (4 AM) stores a DriverScore row per active driver
and stamps the latest snapshot back onto Driver.current_score /
Driver.current_tier for fast read-side queries.

Metric weights:
    Tracking compliance   25 pts
    On-time delivery      25 pts
    Cancellation rate     20 pts
    Photo proof on setup  10 pts
    Manager response time 10 pts
    Customer star rating  10 pts
                          ---
                          100 pts

Tier breakpoints:
    0-59    new (also: <20 lifetime deliveries → new regardless of score)
    60-79   trusted
    80-94   rockstar
    95-100  top_rockstar

Drivers with zero data in a metric window get full credit for that
metric — punishing them for the absence of evidence would be wrong.
The lifetime-deliveries floor handles the genuinely-new-driver case.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    Cancellation,
    Driver,
    DriverScore,
    ManagerMessage,
    Order,
)

logger = logging.getLogger(__name__)

# ---- constants (SPEC.md §8) ----
WINDOW_DAYS = 30
TRACKING_MAX = 25
ON_TIME_MAX = 25
CANCELLATION_MAX = 20
PHOTO_MAX = 10
RESPONSE_MAX = 10
STAR_MAX = 10

# Tier breakpoints (inclusive lower bound)
TIER_NEW = "new"
TIER_TRUSTED = "trusted"
TIER_ROCKSTAR = "rockstar"
TIER_TOP_ROCKSTAR = "top_rockstar"
NEW_TIER_LIFETIME_FLOOR = 20  # <20 lifetime deliveries → new regardless

# Manager response timing
RESPONSE_FAST_THRESHOLD_SECONDS = 300  # 5 min = "fast"


@dataclass
class ScoreBreakdown:
    """The six metric components + total, returned by compute_driver_score()."""

    tracking: int
    on_time: int
    cancellation: int
    photo: int
    response: int
    star: int
    total: int
    tier: str


# ---- tier math ----

def compute_tier(score: int, lifetime_delivery_count: int) -> str:
    """Map score+lifetime to a tier name.
    NEW-tier lifetime floor wins regardless of score per SPEC §8."""
    if lifetime_delivery_count < NEW_TIER_LIFETIME_FLOOR:
        return TIER_NEW
    if score >= 95:
        return TIER_TOP_ROCKSTAR
    if score >= 80:
        return TIER_ROCKSTAR
    if score >= 60:
        return TIER_TRUSTED
    return TIER_NEW


# ---- per-metric scorers ----
# Each function takes a Driver + a (window_start, window_end) date pair
# (Python date objects, inclusive on both ends). They query Order /
# Cancellation / ManagerMessage directly via the passed Session.
#
# Window matching note: Order.delivery_date is a String("YYYY-MM-DD")
# in our schema (legacy decision). We compare on the ISO string form
# of the date bounds — lexicographic and chronological order match.

def _date_iso(d: date) -> str:
    return d.isoformat()


def _driver_deliveries_query(db: Session, driver: Driver, ws: date, we: date):
    """Base query: this driver's `delivered` orders within the window."""
    return (
        db.query(Order)
        .filter(Order.assigned_driver_id == driver.id)
        .filter(Order.status == "delivered")
        .filter(Order.delivery_date >= _date_iso(ws))
        .filter(Order.delivery_date <= _date_iso(we))
    )


def tracking_score(db: Session, driver: Driver, ws: date, we: date) -> int:
    deliveries = _driver_deliveries_query(db, driver, ws, we)
    total = deliveries.count()
    if total == 0:
        return TRACKING_MAX
    tracked = deliveries.filter(Order.tracking_status == "Tracked").count()
    return round(TRACKING_MAX * (tracked / total))


def on_time_score(db: Session, driver: Driver, ws: date, we: date) -> int:
    """Linear scale: 95%+ on-time = full credit, 80% or below = 0.
    Considers only deliveries that have both a window_end and an actual
    delivered timestamp (rows lacking either are skipped — not penalized)."""
    deliveries = (
        _driver_deliveries_query(db, driver, ws, we)
        .filter(Order.delivery_window_end.isnot(None))
        .filter(Order.delivered_actual_at.isnot(None))
        .all()
    )
    if not deliveries:
        return ON_TIME_MAX
    on_time = sum(
        1 for d in deliveries if d.delivered_actual_at <= d.delivery_window_end
    )
    pct = on_time / len(deliveries)
    # Linear: pct=0.95 → 1.0, pct=0.80 → 0.0, else clamp
    fraction = max(0.0, min(1.0, (pct - 0.80) / 0.15))
    return round(ON_TIME_MAX * fraction)


def cancellation_score(db: Session, driver: Driver, ws: date, we: date) -> int:
    """Banded: 0 cancels=20, 1=14, 2=6, 3+=0.
    Counts only driver-initiated cancellations within the window."""
    cancels = (
        db.query(Cancellation)
        .filter(Cancellation.driver_id == driver.id)
        .filter(Cancellation.cancelled_by == "driver")
        .filter(
            Cancellation.cancelled_at >= datetime.combine(ws, datetime.min.time())
        )
        .filter(
            Cancellation.cancelled_at <= datetime.combine(we, datetime.max.time())
        )
        .count()
    )
    if cancels == 0:
        return CANCELLATION_MAX
    if cancels == 1:
        return 14
    if cancels == 2:
        return 6
    return 0


def photo_score(db: Session, driver: Driver, ws: date, we: date) -> int:
    deliveries = _driver_deliveries_query(db, driver, ws, we)
    total = deliveries.count()
    if total == 0:
        return PHOTO_MAX
    with_photo = deliveries.filter(Order.setup_photo_url.isnot(None)).count()
    return round(PHOTO_MAX * (with_photo / total))


def response_score(db: Session, driver: Driver, ws: date, we: date) -> int:
    """Linear: 90%+ fast = full credit, 50%- = 0.
    Only counts messages sent during an active delivery (during_active_delivery=True)."""
    msgs = (
        db.query(ManagerMessage)
        .filter(ManagerMessage.driver_id == driver.id)
        .filter(ManagerMessage.during_active_delivery == True)  # noqa: E712
        .filter(
            ManagerMessage.sent_at >= datetime.combine(ws, datetime.min.time())
        )
        .filter(
            ManagerMessage.sent_at <= datetime.combine(we, datetime.max.time())
        )
    )
    total = msgs.count()
    if total == 0:
        return RESPONSE_MAX
    fast = msgs.filter(
        ManagerMessage.replied_within_seconds <= RESPONSE_FAST_THRESHOLD_SECONDS
    ).count()
    pct = fast / total
    # Linear: pct=0.90 → 1.0, pct=0.50 → 0.0
    fraction = max(0.0, min(1.0, (pct - 0.5) / 0.4))
    return round(RESPONSE_MAX * fraction)


def star_score(db: Session, driver: Driver, ws: date, we: date) -> int:
    """Linear: 4.9 avg = full credit, 4.0 = 0.
    Skips deliveries without a customer_rating."""
    avg = (
        db.query(func.avg(Order.customer_rating))
        .filter(Order.assigned_driver_id == driver.id)
        .filter(Order.status == "delivered")
        .filter(Order.customer_rating.isnot(None))
        .filter(Order.delivery_date >= _date_iso(ws))
        .filter(Order.delivery_date <= _date_iso(we))
        .scalar()
    )
    if avg is None:
        return STAR_MAX
    fraction = max(0.0, min(1.0, (avg - 4.0) / 0.9))
    return round(STAR_MAX * fraction)


# ---- driver-level compute ----

def compute_driver_score(
    db: Session, driver: Driver, ws: date | None = None, we: date | None = None
) -> ScoreBreakdown:
    """Compute one driver's full score breakdown for the given window.
    Default window = trailing 30 days ending today."""
    if we is None:
        we = date.today()
    if ws is None:
        ws = we - timedelta(days=WINDOW_DAYS)

    tracking = tracking_score(db, driver, ws, we)
    on_time = on_time_score(db, driver, ws, we)
    cancellation = cancellation_score(db, driver, ws, we)
    photo = photo_score(db, driver, ws, we)
    response = response_score(db, driver, ws, we)
    star = star_score(db, driver, ws, we)
    total = tracking + on_time + cancellation + photo + response + star
    tier = compute_tier(total, driver.lifetime_delivery_count or 0)
    return ScoreBreakdown(tracking, on_time, cancellation, photo, response, star, total, tier)


# ---- nightly job ----

def recompute_all_driver_scores() -> dict:
    """Recompute every active driver's score, write a DriverScore row, and
    update Driver.current_score / Driver.current_tier in place. Returns a
    summary dict for logging. Safe to call from a cron / scheduler.

    Emits a 'tier_changed' log line on each driver whose tier moved — the
    real notification wiring (push, email) can subscribe to that log
    pattern or be added later as a side-effect hook here.
    """
    db = SessionLocal()
    if db is None:
        logger.warning("recompute_all_driver_scores: SessionLocal is None — skipping")
        return {"scored": 0, "tier_changes": 0}

    today = date.today()
    ws = today - timedelta(days=WINDOW_DAYS)
    scored = 0
    tier_changes = 0

    try:
        # status was added in migration 15; existing rows backfilled to 'active'
        # via the boot-time ALTER. Skip suspended/terminated.
        drivers = db.query(Driver).filter(Driver.status == "active").all()
        for d in drivers:
            old_tier = d.current_tier
            bd = compute_driver_score(db, d, ws, today)
            row = DriverScore(
                driver_id=d.id,
                computed_at=datetime.utcnow(),
                window_start=ws,
                window_end=today,
                score=bd.total,
                tier=bd.tier,
                tracking_pts=bd.tracking,
                on_time_pts=bd.on_time,
                cancellation_pts=bd.cancellation,
                photo_pts=bd.photo,
                response_pts=bd.response,
                star_pts=bd.star,
            )
            db.add(row)
            d.current_score = bd.total
            d.current_tier = bd.tier
            scored += 1
            if old_tier and old_tier != bd.tier:
                tier_changes += 1
                logger.info(
                    "tier_changed driver_id=%s from=%s to=%s score=%d",
                    d.id, old_tier, bd.tier, bd.total,
                )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("recompute_all_driver_scores failed")
        raise
    finally:
        db.close()

    return {"scored": scored, "tier_changes": tier_changes}
