"""Schedules V2 - Block 9: employee shift-offer / swap / marketplace API (ckai).

OFFERS (/employee/shift-offers/*):
  POST /employee/shift-offers              {shift_id, expires_at?, unrestricted?}  -> create (own assigned shift)
  POST /employee/shift-offers/<id>/take    -> take an eligible OPEN offer
  POST /employee/shift-offers/<id>/cancel  -> cancel own OPEN/TAKEN offer
  GET  /employee/shift-offers/list         -> my offers (made + taken)
MARKETPLACE:
  GET  /employee/shift-marketplace/list    -> OPEN offers I'm eligible to take (not mine)
SWAPS (/employee/shift-swaps/*):
  POST /employee/shift-swaps/propose       {from_shift_id, to_shift_id, expires_at?}
  POST /employee/shift-swaps/<id>/accept   -> the OTHER employee accepts
  POST /employee/shift-swaps/<id>/cancel   -> the proposer cancels
  GET  /employee/shift-swaps/list          -> my swaps (proposed to/from me)

Attaches to employee_auth bp; employee-session-scoped (_require_emp -> 401). The
3 prefixes are in auth.py EXEMPT (clean 401 JSON). A manager APPROVES before any
shifts.employee_id actually moves (schedules_v2_market.py); these endpoints only
drive the request state machine. GET data lives at /list so ck's HTML pages own
the bare paths (the B5-B8 split). State guards on every transition (directive:
can't offer a non-own/non-assigned shift, can't take a non-open/ineligible offer,
can't accept someone else's swap, etc.).
"""
from __future__ import annotations

from datetime import datetime

from flask import jsonify, request, session

from app.db import SessionLocal
from app.models import Schedule, Shift, ShiftOffer, ShiftSwap
from app.services import scheduling_offers
from app.web.employee_auth import employee_auth


def _require_emp():
    eid = session.get("employee_id")
    if not eid:
        return None, (jsonify({"ok": False, "error": "login required"}), 401)
    return eid, None


def _parse_exp(v):
    """Optional ISO expires_at: (dt|None, None) or (None, errmsg)."""
    if v is None or not str(v).strip():
        return None, None
    try:
        return datetime.fromisoformat(str(v).strip()), None
    except ValueError:
        return None, "expires_at must be ISO datetime (YYYY-MM-DDTHH:MM)"


def _parse_incentive(v):
    """Optional cash incentive in DOLLARS -> (cents|None, None) or (None, errmsg).
    Empty/None = no money. Bounded $0..$500 to catch typos. A displayed
    incentive only -- the app never moves money (employees settle offline)."""
    if v is None or str(v).strip() == "":
        return None, None
    try:
        dollars = float(str(v).strip().lstrip("$"))
    except ValueError:
        return None, "incentive must be a dollar amount"
    if dollars < 0:
        return None, "incentive cannot be negative"
    if dollars > 500:
        return None, "incentive cannot exceed $500"
    cents = int(round(dollars * 100))
    return (cents if cents > 0 else None), None


def _ser_offer(o):
    return {"id": o.id, "shift_id": o.shift_id,
            "status": o.status, "restricted": o.restricted,
            "incentive_cents": o.incentive_cents,
            "expires_at": o.expires_at.isoformat() if o.expires_at else None}


def _ser_swap(s):
    return {"id": s.id, "from_shift_id": s.from_shift_id, "to_shift_id": s.to_shift_id,
            "status": s.status,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None}


# ------------------------------------------------------------------ OFFERS
@employee_auth.route("/employee/shift-offers", methods=["POST"])
def emp_offer_create():
    """Offer up one of your own assigned shifts."""
    emp_id, err = _require_emp()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    shift_id = data.get("shift_id")
    if not shift_id:
        return jsonify({"ok": False, "error": "shift_id required"}), 400
    exp, e1 = _parse_exp(data.get("expires_at"))
    if e1:
        return jsonify({"ok": False, "error": e1}), 400
    unrestricted = bool(data.get("unrestricted", False))
    incentive_cents, e2 = _parse_incentive(data.get("incentive"))
    if e2:
        return jsonify({"ok": False, "error": e2}), 400
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        sh = db.query(Shift).filter_by(id=shift_id).first()
        if sh is None:
            return jsonify({"ok": False, "error": "shift not found"}), 404
        if sh.employee_id != emp_id:
            return jsonify({"ok": False, "error": "not your shift"}), 403
        if sh.status != "assigned":
            return jsonify({"ok": False, "error": "only an assigned shift can be offered"}), 409
        dup = (db.query(ShiftOffer)
                 .filter(ShiftOffer.shift_id == shift_id,
                         ShiftOffer.status.in_(["open", "taken"])).first())
        if dup is not None:
            return jsonify({"ok": False, "error": "this shift already has an active offer"}), 409
        o = ShiftOffer(shift_id=shift_id, offered_by_employee_id=emp_id, status="open",
                       restricted=not unrestricted, incentive_cents=incentive_cents,
                       expires_at=exp, created_at=now, updated_at=now)
        db.add(o)
        db.commit()
        return jsonify({"ok": True, "offer": _ser_offer(o)}), 201
    finally:
        db.close()


@employee_auth.route("/employee/shift-offers/<int:offer_id>/take", methods=["POST"])
def emp_offer_take(offer_id):
    """Take an OPEN offer you're eligible for (-> 'taken', pending manager approval)."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        o = db.query(ShiftOffer).filter_by(id=offer_id).first()
        if o is None:
            return jsonify({"ok": False, "error": "offer not found"}), 404
        if o.status != "open":
            return jsonify({"ok": False, "error": "offer is %s, not open" % o.status}), 409
        if not scheduling_offers.is_eligible_taker(db, o, emp_id):
            return jsonify({"ok": False, "error": "you are not eligible to take this shift"}), 403
        o.taken_by_employee_id = emp_id
        o.status = "taken"
        o.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "offer": _ser_offer(o)}), 200
    finally:
        db.close()


@employee_auth.route("/employee/shift-offers/<int:offer_id>/cancel", methods=["POST"])
def emp_offer_cancel(offer_id):
    """Cancel your own OPEN/TAKEN offer."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        o = db.query(ShiftOffer).filter_by(id=offer_id).first()
        if o is None:
            return jsonify({"ok": False, "error": "offer not found"}), 404
        if o.offered_by_employee_id != emp_id:
            return jsonify({"ok": False, "error": "not your offer"}), 403
        if o.status not in ("open", "taken"):
            return jsonify({"ok": False, "error": "cannot cancel a %s offer" % o.status}), 409
        o.status = "cancelled"
        o.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "offer": _ser_offer(o)}), 200
    finally:
        db.close()


@employee_auth.route("/employee/shift-offers/list", methods=["GET"])
def emp_offer_list():
    """My offers - ones I made + ones I took."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        mine = (db.query(ShiftOffer)
                  .filter((ShiftOffer.offered_by_employee_id == emp_id) |
                          (ShiftOffer.taken_by_employee_id == emp_id))
                  .order_by(ShiftOffer.created_at.desc()).all())
        return jsonify({"ok": True, "offers": [scheduling_offers.offer_card(db, o) for o in mine]}), 200
    finally:
        db.close()


# ------------------------------------------------------------- MARKETPLACE
@employee_auth.route("/employee/shift-marketplace/list", methods=["GET"])
def emp_marketplace():
    """OPEN offers this employee is eligible to take (not their own)."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        offers = scheduling_offers.eligible_open_offers(db, emp_id)
        return jsonify({"ok": True, "offers": [scheduling_offers.offer_card(db, o) for o in offers]}), 200
    finally:
        db.close()


# ------------------------------------------------------------------- SWAPS
@employee_auth.route("/employee/shift-swaps/propose", methods=["POST"])
def emp_swap_propose():
    """Propose swapping your shift (from) for another employee's shift (to)."""
    emp_id, err = _require_emp()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    from_id, to_id = data.get("from_shift_id"), data.get("to_shift_id")
    if not from_id or not to_id:
        return jsonify({"ok": False, "error": "from_shift_id + to_shift_id required"}), 400
    if from_id == to_id:
        return jsonify({"ok": False, "error": "cannot swap a shift with itself"}), 400
    exp, e1 = _parse_exp(data.get("expires_at"))
    if e1:
        return jsonify({"ok": False, "error": e1}), 400
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        fsh = db.query(Shift).filter_by(id=from_id).first()
        tsh = db.query(Shift).filter_by(id=to_id).first()
        if fsh is None or tsh is None:
            return jsonify({"ok": False, "error": "shift not found"}), 404
        if fsh.employee_id != emp_id:
            return jsonify({"ok": False, "error": "from_shift is not yours"}), 403
        if not tsh.employee_id or tsh.employee_id == emp_id:
            return jsonify({"ok": False, "error": "to_shift must be assigned to another employee"}), 400
        s = ShiftSwap(from_shift_id=from_id, to_shift_id=to_id, from_employee_id=emp_id,
                      to_employee_id=tsh.employee_id, status="proposed", expires_at=exp,
                      created_at=now, updated_at=now)
        db.add(s)
        db.commit()
        return jsonify({"ok": True, "swap": _ser_swap(s)}), 201
    finally:
        db.close()


@employee_auth.route("/employee/shift-swaps/<int:swap_id>/accept", methods=["POST"])
def emp_swap_accept(swap_id):
    """The other employee accepts a proposed swap (-> 'accepted', pending approval)."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        s = db.query(ShiftSwap).filter_by(id=swap_id).first()
        if s is None:
            return jsonify({"ok": False, "error": "swap not found"}), 404
        if s.to_employee_id != emp_id:
            return jsonify({"ok": False, "error": "only the other employee can accept this swap"}), 403
        if s.status != "proposed":
            return jsonify({"ok": False, "error": "swap is %s, not proposed" % s.status}), 409
        s.status = "accepted"
        s.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "swap": _ser_swap(s)}), 200
    finally:
        db.close()


@employee_auth.route("/employee/shift-swaps/<int:swap_id>/cancel", methods=["POST"])
def emp_swap_cancel(swap_id):
    """The proposer cancels their own PROPOSED/ACCEPTED swap."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        s = db.query(ShiftSwap).filter_by(id=swap_id).first()
        if s is None:
            return jsonify({"ok": False, "error": "swap not found"}), 404
        if s.from_employee_id != emp_id:
            return jsonify({"ok": False, "error": "not your swap"}), 403
        if s.status not in ("proposed", "accepted"):
            return jsonify({"ok": False, "error": "cannot cancel a %s swap" % s.status}), 409
        s.status = "cancelled"
        s.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "swap": _ser_swap(s)}), 200
    finally:
        db.close()


@employee_auth.route("/employee/shift-swaps/list", methods=["GET"])
def emp_swap_list():
    """My swaps - proposed by me or to me."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        mine = (db.query(ShiftSwap)
                  .filter((ShiftSwap.from_employee_id == emp_id) |
                          (ShiftSwap.to_employee_id == emp_id))
                  .order_by(ShiftSwap.created_at.desc()).all())
        return jsonify({"ok": True, "swaps": [scheduling_offers.swap_card(db, s) for s in mine]}), 200
    finally:
        db.close()


@employee_auth.route("/employee/shift-swaps/candidates", methods=["GET"])
def emp_swap_candidates():
    """Other employees' assigned FUTURE shifts you could swap against (to_shift
    options) - rich shift cards + the holder's name. ?from_shift_id narrows to that
    shift's store (must be your own shift); omitted = all your stores."""
    emp_id, err = _require_emp()
    if err:
        return err
    from_shift_id = (request.args.get("from_shift_id") or "").strip()
    db = SessionLocal()
    try:
        if from_shift_id:
            try:
                fsid = int(from_shift_id)
            except ValueError:
                return jsonify({"ok": False, "error": "from_shift_id must be an integer"}), 400
            fsh = db.query(Shift).filter_by(id=fsid).first()
            if fsh is None or fsh.employee_id != emp_id:
                return jsonify({"ok": False, "error": "from_shift_id must be your own shift"}), 400
            store = scheduling_offers.shift_store(db, fsh)
            stores = [store] if store else []
        else:
            stores = list(scheduling_offers.employee_stores(db, emp_id))
        if not stores:
            return jsonify({"ok": True, "candidates": []}), 200
        now = datetime.utcnow()
        rows = (db.query(Shift)
                  .join(Schedule, Schedule.id == Shift.schedule_id)
                  .filter(Schedule.store_key.in_(stores),
                          Shift.employee_id.isnot(None),
                          Shift.employee_id != emp_id,
                          Shift.start_at >= now)
                  .order_by(Shift.start_at).all())
        out = []
        for sh in rows:
            card = scheduling_offers.shift_card(db, sh.id)
            card["employee"] = scheduling_offers.emp_ref(db, sh.employee_id)
            out.append(card)
        return jsonify({"ok": True, "candidates": out}), 200
    finally:
        db.close()
