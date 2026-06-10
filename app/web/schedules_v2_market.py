"""Schedules V2 - Block 9: MANAGER offer/swap review + approval (ckai).

  GET  /<store>/schedules-v2/offers/list [?status]   -> this store's offers (JSON)
  POST /<store>/schedules-v2/offers/<id>/approve      -> MOVE shift.employee_id to the taker
  POST /<store>/schedules-v2/offers/<id>/deny
  GET  /<store>/schedules-v2/swaps/list [?status]     -> this store's swaps (JSON)
  POST /<store>/schedules-v2/swaps/<id>/approve       -> SWAP the two shifts' employee_ids
  POST /<store>/schedules-v2/swaps/<id>/deny

Rides store_bp (gates inherited) + @require_level('foh_manager'). Store-scoped: an
offer/swap belongs here if its (from_)shift's schedule.store_key == g.current_location.
/list reserves the bare path for ck's HTML pages.

APPROVE is the ONLY writer of shifts.employee_id in B9 (B4's table). It RE-VALIDATES
at approval time before moving: the offer must be 'taken' / the swap 'accepted'; the
shift(s) must still be held by the original employee(s) (else stale -> 409); the
taker/new-assignee must not have APPROVED time-off on the shift's date (re-checks
scheduling_timeoff.conflict -> 409) and (offers) must still be eligible. The move +
the status flip commit together. ("Approve own" isn't a case here: the approver is a
manager User, the offerer/taker are Employees - distinct principals.)
"""
from __future__ import annotations

from datetime import datetime

from flask import g, jsonify, request

from app.db import SessionLocal
from app.models import Schedule, Shift, ShiftOffer, ShiftSwap
from app.services import scheduling_offers, scheduling_timeoff
from app.web.permissions import current_user_id, require_level
from app.web.store_routes import store_bp

_MGR = "foh_manager"


def _store():
    return getattr(g, "current_location", None)


def _ser_offer(o):
    return {"id": o.id, "shift_id": o.shift_id,
            "offered_by": o.offered_by_employee_id, "taken_by": o.taken_by_employee_id,
            "status": o.status, "restricted": o.restricted,
            "expires_at": o.expires_at.isoformat() if o.expires_at else None,
            "reviewed_by": o.reviewed_by}


def _ser_swap(s):
    return {"id": s.id, "from_shift_id": s.from_shift_id, "to_shift_id": s.to_shift_id,
            "from_employee_id": s.from_employee_id, "to_employee_id": s.to_employee_id,
            "status": s.status,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            "reviewed_by": s.reviewed_by}


def _shift_in_store(db, shift_id):
    """The shift IFF its schedule is in the current store, else None."""
    sh = db.query(Shift).filter_by(id=shift_id).first()
    if sh is None:
        return None
    sched = db.query(Schedule).filter_by(id=sh.schedule_id).first()
    if sched is None or sched.store_key != _store():
        return None
    return sh


def _list_status(raw_status, kind):
    """Map UI filter labels onto the persisted offer/swap workflow states."""
    status = (raw_status or "").strip().lower()
    if not status or status == "all":
        return None
    if status == "pending":
        return "taken" if kind == "offer" else "accepted"
    return status


# ------------------------------------------------------------------ OFFERS
@store_bp.route("/schedules-v2/offers/list", methods=["GET"])
@require_level(_MGR)
def sv2_offers_list():
    """This store's offers (offers whose shift is in this store). ?status filters."""
    status = _list_status(request.args.get("status"), "offer")
    db = SessionLocal()
    try:
        q = (db.query(ShiftOffer)
               .join(Shift, Shift.id == ShiftOffer.shift_id)
               .join(Schedule, Schedule.id == Shift.schedule_id)
               .filter(Schedule.store_key == _store()))
        if status:
            q = q.filter(ShiftOffer.status == status)
        offers = q.order_by(ShiftOffer.created_at.desc()).all()
        return jsonify({"ok": True, "offers": [scheduling_offers.offer_card(db, o) for o in offers]}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/offers/<int:offer_id>/approve", methods=["POST"])
@require_level(_MGR)
def sv2_offer_approve(offer_id):
    """Approve a TAKEN offer -> move the shift to the taker. Re-validates first."""
    db = SessionLocal()
    try:
        o = db.query(ShiftOffer).filter_by(id=offer_id).first()
        if o is None:
            return jsonify({"ok": False, "error": "offer not found"}), 404
        sh = _shift_in_store(db, o.shift_id)
        if sh is None:
            return jsonify({"ok": False, "error": "offer not in this store"}), 404
        if o.status != "taken":
            return jsonify({"ok": False, "error": "offer must be taken before approval (it is %s)" % o.status}), 409
        if sh.employee_id != o.offered_by_employee_id:
            return jsonify({"ok": False, "error": "shift is no longer held by the offerer"}), 409
        if not scheduling_offers.is_eligible_taker(db, o, o.taken_by_employee_id):
            return jsonify({"ok": False, "error": "taker is no longer eligible for this shift"}), 409
        if sh.start_at:
            blocker = scheduling_timeoff.conflict(o.taken_by_employee_id, sh.start_at.date())
            if blocker:
                return jsonify({"ok": False, "error": blocker}), 409
        now = datetime.utcnow()
        sh.employee_id = o.taken_by_employee_id   # THE MOVE
        sh.status = "assigned"
        sh.updated_at = now
        o.status = "approved"
        o.reviewed_by = current_user_id()
        o.reviewed_at = now
        o.updated_at = now
        db.commit()
        return jsonify({"ok": True, "offer": _ser_offer(o),
                        "shift_id": sh.id, "now_assigned_to": sh.employee_id}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/offers/<int:offer_id>/deny", methods=["POST"])
@require_level(_MGR)
def sv2_offer_deny(offer_id):
    """Deny an open/taken offer (shift unchanged)."""
    db = SessionLocal()
    try:
        o = db.query(ShiftOffer).filter_by(id=offer_id).first()
        if o is None or _shift_in_store(db, o.shift_id) is None:
            return jsonify({"ok": False, "error": "offer not in this store"}), 404
        if o.status not in ("open", "taken"):
            return jsonify({"ok": False, "error": "cannot deny a %s offer" % o.status}), 409
        now = datetime.utcnow()
        o.status = "denied"
        o.reviewed_by = current_user_id()
        o.reviewed_at = now
        o.updated_at = now
        db.commit()
        return jsonify({"ok": True, "offer": _ser_offer(o)}), 200
    finally:
        db.close()


# ------------------------------------------------------------------- SWAPS
@store_bp.route("/schedules-v2/swaps/list", methods=["GET"])
@require_level(_MGR)
def sv2_swaps_list():
    """This store's swaps (scoped by the from_shift's store). ?status filters."""
    status = _list_status(request.args.get("status"), "swap")
    db = SessionLocal()
    try:
        q = (db.query(ShiftSwap)
               .join(Shift, Shift.id == ShiftSwap.from_shift_id)
               .join(Schedule, Schedule.id == Shift.schedule_id)
               .filter(Schedule.store_key == _store()))
        if status:
            q = q.filter(ShiftSwap.status == status)
        swaps = q.order_by(ShiftSwap.created_at.desc()).all()
        return jsonify({"ok": True, "swaps": [scheduling_offers.swap_card(db, s) for s in swaps]}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/swaps/<int:swap_id>/approve", methods=["POST"])
@require_level(_MGR)
def sv2_swap_approve(swap_id):
    """Approve an ACCEPTED swap -> swap the two shifts' employee_ids. Re-validates."""
    db = SessionLocal()
    try:
        s = db.query(ShiftSwap).filter_by(id=swap_id).first()
        if s is None:
            return jsonify({"ok": False, "error": "swap not found"}), 404
        fsh = _shift_in_store(db, s.from_shift_id)
        if fsh is None:
            return jsonify({"ok": False, "error": "swap not in this store"}), 404
        # B9 finding #1 (aick): the TO shift must ALSO be in THIS store - else a
        # single-store manager could move ANOTHER store's shift via a cross-store
        # swap (sv2_swap_approve was scoping only the FROM shift). Same-store only
        # here, matching the /candidates picker's intent; a genuine cross-store swap
        # would need a separate two-manager flow (out of scope).
        if _shift_in_store(db, s.to_shift_id) is None:
            return jsonify({"ok": False, "error": "swap not in this store"}), 404
        tsh = db.query(Shift).filter_by(id=s.to_shift_id).first()
        if tsh is None:
            return jsonify({"ok": False, "error": "the other shift no longer exists"}), 409
        if s.status != "accepted":
            return jsonify({"ok": False, "error": "swap must be accepted before approval (it is %s)" % s.status}), 409
        if fsh.employee_id != s.from_employee_id or tsh.employee_id != s.to_employee_id:
            return jsonify({"ok": False, "error": "a shift changed hands since the swap was proposed"}), 409
        if not scheduling_offers.is_eligible_for_shift(db, s.from_employee_id, tsh):
            return jsonify({"ok": False, "error": "from employee is no longer eligible for the other shift"}), 409
        if not scheduling_offers.is_eligible_for_shift(db, s.to_employee_id, fsh):
            return jsonify({"ok": False, "error": "to employee is no longer eligible for the offered shift"}), 409
        # re-check approved time-off for BOTH new assignments
        if tsh.start_at:
            b1 = scheduling_timeoff.conflict(s.from_employee_id, tsh.start_at.date())
            if b1:
                return jsonify({"ok": False, "error": b1}), 409
        if fsh.start_at:
            b2 = scheduling_timeoff.conflict(s.to_employee_id, fsh.start_at.date())
            if b2:
                return jsonify({"ok": False, "error": b2}), 409
        now = datetime.utcnow()
        fsh.employee_id = s.to_employee_id     # THE SWAP
        tsh.employee_id = s.from_employee_id
        fsh.updated_at = tsh.updated_at = now
        s.status = "approved"
        s.reviewed_by = current_user_id()
        s.reviewed_at = now
        s.updated_at = now
        db.commit()
        return jsonify({"ok": True, "swap": _ser_swap(s),
                        "from_shift_now": fsh.employee_id, "to_shift_now": tsh.employee_id}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/swaps/<int:swap_id>/deny", methods=["POST"])
@require_level(_MGR)
def sv2_swap_deny(swap_id):
    """Deny a proposed/accepted swap (shifts unchanged)."""
    db = SessionLocal()
    try:
        s = db.query(ShiftSwap).filter_by(id=swap_id).first()
        if s is None or _shift_in_store(db, s.from_shift_id) is None:
            return jsonify({"ok": False, "error": "swap not in this store"}), 404
        if s.status not in ("proposed", "accepted"):
            return jsonify({"ok": False, "error": "cannot deny a %s swap" % s.status}), 409
        now = datetime.utcnow()
        s.status = "denied"
        s.reviewed_by = current_user_id()
        s.reviewed_at = now
        s.updated_at = now
        db.commit()
        return jsonify({"ok": True, "swap": _ser_swap(s)}), 200
    finally:
        db.close()
