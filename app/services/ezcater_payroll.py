"""Payroll calculation for ezCater drivers.

Sam's rules (2026-05-10):
    Base:       $25  per delivery (always)
    Bonus:      $10  per delivery (only if tracking_status == 'Tracked')
    Extra mi:   $2.00 / mile over 20 (only if 'Tracked', one-way, kitchen
                -> first drop-off; for multi-stop routes the 1st->2nd leg
                rarely qualifies because miles reset to 0). Rate raised
                from $1.50 to $2.00 per Sam 2026-05-28.
    5-star:     $5   if the delivery got a 5-star review on ezCater AND
                tracking_status == 'Tracked'. ezCater's API doesn't expose
                reviews, so this comes from a manual flag for now.

  Pay period:  bi-weekly. Anchor 2026-04-26 → 2026-05-09; check date
               5 days after the period end (= 2026-05-14 for the anchor).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from dataclasses import dataclass, field
import re

from app.db import SessionLocal
from app.models import Order, DriverLog

# ---- constants ----
ANCHOR_START = date(2026, 4, 26)    # Sunday
PERIOD_LENGTH_DAYS = 14
CHECK_OFFSET_DAYS = 5               # period_end + 5 = check date

BASE_PER_DELIVERY = 25.00
BONUS_TRACKED = 10.00
PER_MILE_OVER_20 = 2.00
FIVE_STAR_BONUS = 5.00
MILES_THRESHOLD = 20.0

# Sam #1492 (2026-05-28): an order only flows into payroll 2h after its
# delivery time has passed — that's when a manager should verify + pay it.
READY_DELAY_HOURS = 2


# ---- pay period math ----

def period_containing(d: date) -> tuple[date, date, date]:
    """Return (start, end, check_date) for the bi-weekly period containing d."""
    delta = (d - ANCHOR_START).days
    period_index = delta // PERIOD_LENGTH_DAYS
    start = ANCHOR_START + timedelta(days=period_index * PERIOD_LENGTH_DAYS)
    end = start + timedelta(days=PERIOD_LENGTH_DAYS - 1)
    check_date = end + timedelta(days=CHECK_OFFSET_DAYS)
    return start, end, check_date


def previous_period(start: date) -> tuple[date, date, date]:
    return period_containing(start - timedelta(days=1))


# ---- driver name matching ----
#
# ezCater driver names ship in messy formats — "CK #1 - Jose Gonzalez",
# "CK#1 ANA ISA Perez", "Ck #1  Bryan Ruiz", "CK# 1 BRITNEY MATHIS", etc.
# A signed-up Driver in our DB has a clean `name`. We normalize on both
# sides for matching:
#   - lowercase
#   - drop everything before the CK#X prefix (the prefix tells us the
#     home kitchen but the rest of the name is what matters for matching)
#   - collapse whitespace
#   - drop dashes/underscores

_CK_PREFIX_RE = re.compile(r"^\s*ck\s*#?\s*[12]\s*-?\s*", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def normalize_driver_name(raw: str | None) -> str:
    if not raw:
        return ""
    s = raw.lower().strip()
    s = _CK_PREFIX_RE.sub("", s)        # drop "CK #1 -" / "CK#2 -" / etc.
    s = s.replace("-", " ").replace("_", " ")
    s = _WS_RE.sub(" ", s).strip()
    return s


# ---- pay calc ----

@dataclass
class DeliveryPay:
    order_number: str
    delivery_date: str | None
    tracking_status: str | None
    pickup_miles: float | None
    five_star: bool
    base: float
    bonus_tracked: float
    # Three miles columns, all "extra over 20" (Sam #1492/#1503):
    #   E-Miles = expected, derived from the route distance (display-only)
    #   D-Miles = driven, manager-entered, no automatic source (display-only)
    #   V-Miles = verified by a manager on the Ez Drivers page -> THIS pays
    expected_miles: float
    driven_miles: float | None
    verified_miles: float
    extra_miles: float          # legacy alias == verified_miles (the paying miles)
    bonus_miles: float
    bonus_five_star: float
    total: float
    notes: str = ""
    # Raw manager-entered note (no auto "Untracked" fallback) — what the edit
    # form pre-fills, so saving doesn't persist the display-only fallback text.
    notes_input: str = ""
    # Order primary key, so the manager edit form can post payroll input back
    # to the exact row (None for non-Order inputs / synthetic rows).
    order_id: int | None = None
    # Payroll-readiness gate (Sam #1492): True once it's been >= 2h since the
    # delivery time. ready_at_ct is a CT display string for the "pending" badge.
    ready: bool = True
    ready_at_ct: str = ""


def _is_tracked(tracking_status: str | None) -> bool:
    if not tracking_status:
        return False
    return tracking_status.strip().lower() == "tracked"


def payroll_ready(order) -> tuple[bool, datetime | None]:
    """Sam #1492: an order is payroll-ready 2h after its delivery time.

    Returns (ready, ready_at) where ready_at is the naive-UTC moment it
    becomes editable. Delivery timestamps are stored naive-UTC; prefer the
    actual delivered time, fall back to the booked window end. If neither is
    known we can't gate it, so treat it as ready (don't block a manager on a
    historical row that simply lacks a timestamp). getattr keeps this safe for
    non-Order inputs."""
    stamp = (getattr(order, "delivered_actual_at", None)
             or getattr(order, "delivery_window_end", None))
    if stamp is None:
        return True, None
    ready_at = stamp + timedelta(hours=READY_DELAY_HOURS)
    return (datetime.utcnow() >= ready_at), ready_at


def _ready_at_ct(ready_at: datetime | None) -> str:
    """Format a naive-UTC instant as a short America/Chicago string for the
    'pending — ready <when>' badge. Best-effort; falls back to UTC."""
    if ready_at is None:
        return ""
    try:
        from zoneinfo import ZoneInfo
        return (ready_at.replace(tzinfo=ZoneInfo("UTC"))
                        .astimezone(ZoneInfo("America/Chicago"))
                        .strftime("%a %m/%d %I:%M %p"))
    except Exception:
        return ready_at.strftime("%a %m/%d %H:%M UTC")


def compute_one(order: Order, five_star: bool = False) -> DeliveryPay:
    tracked = _is_tracked(order.tracking_status)
    miles = order.pickup_miles or 0.0
    # Keep the paying calc on the UNROUNDED miles so the total stays
    # byte-identical to the pre-redesign formula; round only for display.
    expected_raw = max(0.0, miles - MILES_THRESHOLD)

    # Auto (estimate) values — what the formula paid before manager input
    # existed. Each is overridden per-field below; when no manager value is set
    # the result is byte-identical to the old calc (total AND display fields).
    auto_verified_raw = expected_raw if tracked else 0.0
    auto_bonus_on = tracked
    auto_five_pay = tracked and five_star

    # Manager payroll inputs (Sam #1492/#1503). getattr keeps this safe for
    # rows predating the column backfill and for non-Order inputs. A set value
    # wins; None means "not verified yet" -> fall back to the auto estimate.
    mgr_verified = getattr(order, "pay_verified_miles", None)
    mgr_bonus = getattr(order, "pay_bonus_tracked", None)
    mgr_five = getattr(order, "pay_five_star", None)
    mgr_driven = getattr(order, "pay_driven_miles", None)
    mgr_notes = getattr(order, "pay_notes", None)

    verified_raw = mgr_verified if mgr_verified is not None else auto_verified_raw
    bonus_on = mgr_bonus if mgr_bonus is not None else auto_bonus_on
    # Whether $5 pays vs whether the star flag shows. Splitting them keeps the
    # displayed flag byte-identical to the old raw `five_star` when unset, while
    # a manager 5-star explicitly pays $5 (manager override wins over tracking).
    five_pay = mgr_five if mgr_five is not None else auto_five_pay
    five_flag = mgr_five if mgr_five is not None else five_star

    bonus_miles = round(verified_raw * PER_MILE_OVER_20, 2)

    # E-Miles: expected extra miles over 20, from the route distance. Shown
    # whether or not the order tracked (it's "what the address says we'd owe").
    expected_miles = round(expected_raw, 2)
    # V-Miles display value (what pays, rounded for the column).
    verified_miles = round(verified_raw, 2)
    # D-Miles: extra miles actually driven. No automatic source, so it's blank
    # until a manager enters it; display-only, never pays.
    driven_miles = round(mgr_driven, 2) if mgr_driven is not None else None

    bonus_tracked_v = BONUS_TRACKED if bonus_on else 0.0
    bonus_five_star = FIVE_STAR_BONUS if five_pay else 0.0
    total = round(BASE_PER_DELIVERY + bonus_tracked_v + bonus_miles + bonus_five_star, 2)

    ready, ready_at = payroll_ready(order)
    return DeliveryPay(
        order_number=order.external_order_id or "?",
        delivery_date=order.delivery_date,
        tracking_status=order.tracking_status,
        pickup_miles=order.pickup_miles,
        five_star=five_flag,
        base=BASE_PER_DELIVERY,
        bonus_tracked=bonus_tracked_v,
        expected_miles=expected_miles,
        driven_miles=driven_miles,
        verified_miles=verified_miles,
        extra_miles=verified_miles,
        bonus_miles=bonus_miles,
        bonus_five_star=bonus_five_star,
        total=total,
        notes=mgr_notes if mgr_notes else ((order.tracking_status or "—") if not tracked else ""),
        notes_input=mgr_notes or "",
        order_id=getattr(order, "id", None),
        ready=ready,
        ready_at_ct=_ready_at_ct(ready_at) if not ready else "",
    )


@dataclass
class PaycheckPeriod:
    period_start: date
    period_end: date
    check_date: date
    deliveries: list[DeliveryPay] = field(default_factory=list)
    grand_total: float = 0.0


def _driver_id_from_log(driver_log: DriverLog, db) -> bool:
    """Look up DriverLog.five_star for an order's date+driver. Returns True
    if there's a manual flag stored. Imperfect — we match on driver_name +
    pickup_date string equality; if the log was entered with a different
    name spelling we miss it. Will replace with a proper join once Driver
    is linked to Order rows."""
    return bool(driver_log and driver_log.five_star)


def paycheck_for(driver_name: str, period_start: date, period_end: date) -> PaycheckPeriod:
    """Build a paycheck summary for `driver_name` covering [period_start, period_end]."""
    db = SessionLocal()
    try:
        norm_target = normalize_driver_name(driver_name)
        start_iso = period_start.isoformat()
        end_iso = period_end.isoformat()

        # Pull every order in the date window whose ezcater_driver_name
        # normalizes to the target (covers the half-dozen spelling variants
        # ezCater ships for the same person).
        candidates = (db.query(Order)
                        .filter(Order.delivery_date >= start_iso)
                        .filter(Order.delivery_date <= end_iso)
                        .filter(Order.ezcater_driver_name.isnot(None))
                        .filter(Order.status != "cancelled")
                        .order_by(Order.delivery_date.asc())
                        .all())
        matched = [o for o in candidates if normalize_driver_name(o.ezcater_driver_name) == norm_target]

        # Five-star data: look up DriverLog rows for this driver name + date
        # to grab any manually-flagged 5★ entries. Imperfect (string match) —
        # acceptable until we add a proper foreign key.
        log_lookup = {}
        if matched:
            dates = {o.delivery_date for o in matched if o.delivery_date}
            logs = (db.query(DriverLog)
                      .filter(DriverLog.pickup_date.in_(dates))
                      .all())
            for L in logs:
                if normalize_driver_name(L.driver_name) == norm_target:
                    log_lookup[(L.driver_name, L.pickup_date, L.order_link or "")] = L

        rows = []
        for o in matched:
            # Best-effort five_star lookup — match the log row by pickup_date
            # + driver_name. If multiple logs match, pick the one with the
            # matching order_link (if any), else the first.
            five_star = False
            for (lname, ldate, llink), L in log_lookup.items():
                if ldate == o.delivery_date and (not llink or llink in (o.external_order_id or "")):
                    five_star = bool(L.five_star)
                    break
            rows.append(compute_one(o, five_star=five_star))

        return PaycheckPeriod(
            period_start=period_start,
            period_end=period_end,
            check_date=period_end + timedelta(days=CHECK_OFFSET_DAYS),
            deliveries=rows,
            grand_total=round(sum(r.total for r in rows), 2),
        )
    finally:
        db.close()


def paycheck_history(driver_name: str, periods: int = 6) -> list[PaycheckPeriod]:
    """Return the last `periods` bi-weekly paychecks for this driver, newest
    first. Includes the current period (containing today) at index 0."""
    today = date.today()
    current_start, _, _ = period_containing(today)
    out = []
    cur = current_start
    for _ in range(periods):
        start = cur
        end = start + timedelta(days=PERIOD_LENGTH_DAYS - 1)
        out.append(paycheck_for(driver_name, start, end))
        cur = start - timedelta(days=PERIOD_LENGTH_DAYS)
    return out
