"""Schedules V2 - Block 9: shift offers/swaps service helpers (ckai).

Eligibility (who may take an offer - the marketplace filter + the take re-check)
and the expiry sweep the per-minute cron calls. The state transitions + the
shifts.employee_id moves live in the route files; this is the shared read-logic +
the cron sweep.
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.db import SessionLocal
from app.models import (
    Employee,
    EmployeePosition,
    EmployeeStoreAssignment,
    Position,
    Schedule,
    Shift,
    ShiftOffer,
    ShiftSwap,
)

log = logging.getLogger(__name__)


def employee_stores(db, employee_id) -> set:
    return {a.store_key for a in
            db.query(EmployeeStoreAssignment).filter_by(employee_id=employee_id).all()}


def employee_positions(db, employee_id) -> set:
    return {ep.position_id for ep in
            db.query(EmployeePosition).filter_by(employee_id=employee_id).all()}


def shift_store(db, shift) -> str | None:
    """The LOCATION store_key of a shift (via its schedule)."""
    sched = db.query(Schedule).filter_by(id=shift.schedule_id).first()
    return sched.store_key if sched else None


def is_eligible_taker(db, offer, employee_id) -> bool:
    """Can employee_id take this offer? The offerer can never take their own; an
    UNRESTRICTED offer is open to anyone; a RESTRICTED offer needs the same store
    AND (the shift has no position OR the employee holds that position)."""
    if offer.offered_by_employee_id == employee_id:
        return False
    if not offer.restricted:
        return True
    sh = db.query(Shift).filter_by(id=offer.shift_id).first()
    if sh is None:
        return False
    store = shift_store(db, sh)
    if store is None or store not in employee_stores(db, employee_id):
        return False
    if sh.position_id is None:
        return True
    return sh.position_id in employee_positions(db, employee_id)


def eligible_open_offers(db, employee_id) -> list:
    """The marketplace: OPEN offers this employee may take, excluding their own."""
    offers = db.query(ShiftOffer).filter(ShiftOffer.status == "open").all()
    return [o for o in offers if is_eligible_taker(db, o, employee_id)]


def expire_due() -> dict:
    """Per-minute cron sweep: flip OPEN/TAKEN offers + PROPOSED/ACCEPTED swaps
    whose expires_at has passed -> 'expired'. Returns {expired_offers,
    expired_swaps}. Rides ix_shift_offers_status_exp / ix_shift_swaps_status_exp."""
    db = SessionLocal()
    eo = es = 0
    try:
        now = datetime.utcnow()
        for o in (db.query(ShiftOffer)
                    .filter(ShiftOffer.status.in_(["open", "taken"]),
                            ShiftOffer.expires_at.isnot(None),
                            ShiftOffer.expires_at <= now).all()):
            o.status = "expired"
            o.updated_at = now
            eo += 1
        for s in (db.query(ShiftSwap)
                    .filter(ShiftSwap.status.in_(["proposed", "accepted"]),
                            ShiftSwap.expires_at.isnot(None),
                            ShiftSwap.expires_at <= now).all()):
            s.status = "expired"
            s.updated_at = now
            es += 1
        db.commit()
        if eo or es:
            log.info("[shift-market] expiry cron: offers=%d swaps=%d", eo, es)
        return {"expired_offers": eo, "expired_swaps": es}
    finally:
        db.close()


# --------------------------------------------------------------------------
# Rich card serializers (the LIST endpoints embed shift detail + names so ck can
# render "Devon - Tue Jun 9 9a-5p Server" cards, not bare ids). ckai #1998.
# --------------------------------------------------------------------------
def shift_card(db, shift_id) -> dict | None:
    """A shift as a display card: id + times + position name + store (location)."""
    sh = db.query(Shift).filter_by(id=shift_id).first()
    if sh is None:
        return None
    pos_name = None
    if sh.position_id:
        p = db.query(Position).filter_by(id=sh.position_id).first()
        pos_name = p.name if p else None
    sched = db.query(Schedule).filter_by(id=sh.schedule_id).first()
    return {"id": sh.id,
            "start_at": sh.start_at.isoformat() if sh.start_at else None,
            "end_at": sh.end_at.isoformat() if sh.end_at else None,
            "position_name": pos_name,
            "store": sched.store_key if sched else None}


def emp_ref(db, employee_id) -> dict | None:
    """{id, name} for an employee, or None."""
    if not employee_id:
        return None
    e = db.query(Employee).filter_by(id=employee_id).first()
    return {"id": employee_id, "name": (e.full_name if e else None)}


def offer_card(db, o) -> dict:
    """An offer enriched for display (names + the shift card)."""
    return {"id": o.id, "status": o.status, "restricted": o.restricted,
            "expires_at": o.expires_at.isoformat() if o.expires_at else None,
            "offered_by": emp_ref(db, o.offered_by_employee_id),
            "taken_by": emp_ref(db, o.taken_by_employee_id),
            "shift": shift_card(db, o.shift_id)}


def swap_card(db, s) -> dict:
    """A swap enriched for display (both employees + both shift cards)."""
    return {"id": s.id, "status": s.status,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            "from_employee": emp_ref(db, s.from_employee_id),
            "to_employee": emp_ref(db, s.to_employee_id),
            "from_shift": shift_card(db, s.from_shift_id),
            "to_shift": shift_card(db, s.to_shift_id)}
