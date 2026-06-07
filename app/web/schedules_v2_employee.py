"""Schedules V2 - Block 5: the employee schedule DATA + ACTION endpoints (aick).

The B5 split mirrors B4 (ck owns the PAGE shell in employee_schedule_page.py;
aick owns the JSON the page's client JS fetches). Like that page, this module
ATTACHES to the existing employee_auth blueprint (decorator side effect) so all
/employee/* routes share one blueprint + URL namespace (the B2 house pattern);
employee_auth.py itself stays untouched. app/__init__.py imports this module
BEFORE ezempauth.install(app) so the routes are registered.

ENDPOINTS (paths LOCKED to ck's client config #1923):
  GET  /employee/my-schedule/shifts [?week=YYYY-MM-DD]
        -> {ok, employee:{id,full_name}, shifts:[...]} for THIS week + NEXT week
           only (published schedules). ?week= narrows to that one week IFF it is
           this/next week, else [] (an employee can never see other weeks).
  GET  /employee/shift/<id>          -> {ok, shift:{...}} for the employee's own
           published shift. Foreign shift -> 403; unknown/unpublished -> 404.
  POST /employee/shifts/<id>/accept  -> records 'accepted' (no body; idempotent).
  POST /employee/shifts/<id>/decline -> {reason} required (400 on empty); records
           'declined' + the reason (surfaces to the assigning manager in B6/UI).

AUTH / ISOLATION: each endpoint self-guards session['employee_id'] (401 JSON with
no employee session) and every read/write is scoped to that employee. A shift
that is not the caller's own -> 403 (never reveals another employee's data).
/employee/my-schedule/shifts is in auth.py EXEMPT_PREFIXES (via the
/employee/my-schedule prefix ck added) so the unauth response is this 401 JSON,
not the staff-keypad redirect; the accept/decline/detail routes ride the
employee session's auth_ok through the global gate and self-guard the same way.
"""
from __future__ import annotations

from datetime import date as _date, datetime, timedelta

from flask import jsonify, request, session

from app.db import SessionLocal
from app.models import (
    Employee,
    Position,
    Schedule,
    Shift,
    ShiftAcceptance,
    ShiftOffer,
    ShiftTag,
    Tag,
)
from app.web.employee_auth import employee_auth


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _require_emp():
    """(employee_id, None) for a logged-in employee, else (None, (json, 401))."""
    eid = session.get("employee_id")
    if not eid:
        return None, (jsonify({"ok": False, "error": "login required"}), 401)
    return eid, None


def _week_bounds(today):
    """(this_week_start, next_week_start) as dates. Weeks key to SATURDAY to match the
    manager Week Builder (schedules_v2_week.html WEEK_START_DOW=6): schedules are stored
    with a Saturday week_start, so the employee MUST query Saturdays. The old code anchored
    to Monday, so the employee never matched their own Saturday-keyed schedule -> my-schedule
    came up empty for everyone (pre-existing bug). Sat=weekday 5; days since the week's
    Saturday = (today.weekday() - 5) % 7."""
    this_sat = today - timedelta(days=(today.weekday() - 5) % 7)
    return this_sat, this_sat + timedelta(days=7)


def _serialize_shift(sh, week_start, position_name, tag_names, response, offer=None):
    """One shift as the API dict the client expects. `response` is the legacy
    accept/decline state (kept for back-compat; no longer surfaced in the UI).
    `offer` is THIS employee's active release of the shift -- {id, status} where
    status is 'open' (in the marketplace) or 'taken' (a teammate picked it up,
    pending manager approval) -- else None."""
    return {
        "id": sh.id,
        "schedule_id": sh.schedule_id,
        "week_start": week_start.isoformat() if week_start else None,
        "start_at": sh.start_at.isoformat() if sh.start_at else None,
        "end_at": sh.end_at.isoformat() if sh.end_at else None,
        "break_minutes": sh.break_minutes,
        "position_name": position_name,
        "tag_names": tag_names,
        "notes": sh.notes,
        "response": response,
        "offer": offer,
    }


def _own_published_shift(db, shift_id, emp_id):
    """(shift, schedule, None) when shift_id is the employee's OWN shift in a
    PUBLISHED schedule; else (None, None, (json, code)). Unknown -> 404,
    foreign (someone else's / unassigned) -> 403, draft/unpublished -> 404
    (a draft is invisible to employees)."""
    sh = db.query(Shift).filter_by(id=shift_id).first()
    if sh is None:
        return None, None, (jsonify({"ok": False, "error": "shift not found"}), 404)
    if sh.employee_id != emp_id:
        return None, None, (jsonify({"ok": False, "error": "not your shift"}), 403)
    sched = db.query(Schedule).filter_by(id=sh.schedule_id).first()
    if sched is None or sched.status != "published":
        return None, None, (jsonify({"ok": False, "error": "shift not available"}), 404)
    return sh, sched, None


def _upsert_response(db, shift_id, emp_id, response, reason):
    """Create or flip this employee's accept/decline row for the shift. A later
    accept clears any prior decline reason; a later decline sets the new reason.
    The UNIQUE(shift_id, employee_id) constraint keeps it one row."""
    now = datetime.utcnow()
    a = (db.query(ShiftAcceptance)
           .filter_by(shift_id=shift_id, employee_id=emp_id).first())
    if a is None:
        db.add(ShiftAcceptance(shift_id=shift_id, employee_id=emp_id,
                               response=response, reason=reason,
                               created_at=now, updated_at=now))
    else:
        a.response = response
        a.reason = reason
        a.updated_at = now


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@employee_auth.route("/employee/my-schedule/shifts", methods=["GET"])
def emp_my_schedule_shifts():
    """This week + next week of the employee's PUBLISHED shifts. Rides
    ix_shifts_emp_start (employee_id leading + start_at order)."""
    emp_id, err = _require_emp()
    if err:
        return err

    this_monday, next_monday = _week_bounds(datetime.utcnow().date())
    allowed = {this_monday, next_monday}
    wk = (request.args.get("week") or "").strip()
    if wk:
        try:
            w = _date.fromisoformat(wk)
        except ValueError:
            return jsonify({"ok": False, "error": "week must be YYYY-MM-DD"}), 400
        weeks = [w] if w in allowed else []  # an employee never sees other weeks
    else:
        weeks = [this_monday, next_monday]

    db = SessionLocal()
    try:
        emp = db.query(Employee).filter_by(id=emp_id).first()
        emp_out = {"id": emp_id, "full_name": (emp.full_name if emp else None)}
        if not weeks:
            return jsonify({"ok": True, "employee": emp_out, "shifts": []}), 200

        scheds = (db.query(Schedule)
                    .filter(Schedule.week_start.in_(weeks),
                            Schedule.status == "published").all())
        week_by_sched = {s.id: s.week_start for s in scheds}
        if not week_by_sched:
            return jsonify({"ok": True, "employee": emp_out, "shifts": []}), 200

        rows = (db.query(Shift)
                  .filter(Shift.employee_id == emp_id,
                          Shift.schedule_id.in_(list(week_by_sched.keys())),
                          # per-shift publish: only PUBLISHED shifts are visible to the
                          # employee -- new/edited-but-unpublished shifts stay hidden until
                          # the manager publishes them. (1a backfilled existing published
                          # weeks, so no regression.)
                          Shift.published_at.isnot(None))
                  .order_by(Shift.start_at).all())
        shift_ids = [sh.id for sh in rows]

        pos_ids = {sh.position_id for sh in rows if sh.position_id}
        pos_name = ({p.id: p.name for p in
                     db.query(Position).filter(Position.id.in_(pos_ids)).all()}
                    if pos_ids else {})

        tags_by_shift = {}
        if shift_ids:
            sts = db.query(ShiftTag).filter(ShiftTag.shift_id.in_(shift_ids)).all()
            tag_ids = {st.tag_id for st in sts}
            tname = ({t.id: t.name for t in
                      db.query(Tag).filter(Tag.id.in_(tag_ids)).all()}
                     if tag_ids else {})
            for st in sts:
                tags_by_shift.setdefault(st.shift_id, []).append(tname.get(st.tag_id))

        resp_by_shift = {}
        if shift_ids:
            for a in (db.query(ShiftAcceptance)
                        .filter(ShiftAcceptance.employee_id == emp_id,
                                ShiftAcceptance.shift_id.in_(shift_ids)).all()):
                resp_by_shift[a.shift_id] = a.response

        # reframe: this employee's ACTIVE release per shift -- open = in the
        # marketplace, taken = a teammate grabbed it (pending manager approval).
        # Reuses ckai's ShiftOffer (B9), scoped to offers THIS employee made, so it
        # never leaks another employee's marketplace activity.
        offer_by_shift = {}
        if shift_ids:
            for o in (db.query(ShiftOffer)
                        .filter(ShiftOffer.offered_by_employee_id == emp_id,
                                ShiftOffer.shift_id.in_(shift_ids),
                                ShiftOffer.status.in_(["open", "taken"])).all()):
                offer_by_shift[o.shift_id] = {"id": o.id, "status": o.status}

        out = [_serialize_shift(sh, week_by_sched.get(sh.schedule_id),
                                pos_name.get(sh.position_id),
                                tags_by_shift.get(sh.id, []),
                                resp_by_shift.get(sh.id),
                                offer_by_shift.get(sh.id))
               for sh in rows]
        return jsonify({"ok": True, "employee": emp_out, "shifts": out}), 200
    finally:
        db.close()


@employee_auth.route("/employee/shift/<int:shift_id>", methods=["GET"])
def emp_shift_detail(shift_id):
    """Detail for one of the employee's own published shifts (foreign -> 403)."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        sh, sched, ferr = _own_published_shift(db, shift_id, emp_id)
        if ferr:
            return ferr
        position_name = None
        if sh.position_id:
            p = db.query(Position).filter_by(id=sh.position_id).first()
            position_name = p.name if p else None
        tag_names = []
        sts = db.query(ShiftTag).filter_by(shift_id=sh.id).all()
        if sts:
            tids = [st.tag_id for st in sts]
            tname = {t.id: t.name for t in
                     db.query(Tag).filter(Tag.id.in_(tids)).all()}
            tag_names = [tname.get(st.tag_id) for st in sts]
        a = (db.query(ShiftAcceptance)
               .filter_by(shift_id=sh.id, employee_id=emp_id).first())
        resp = a.response if a else None
        return jsonify({"ok": True,
                        "shift": _serialize_shift(sh, sched.week_start,
                                                  position_name, tag_names, resp)}), 200
    finally:
        db.close()


@employee_auth.route("/employee/shifts/<int:shift_id>/accept", methods=["POST"])
def emp_shift_accept(shift_id):
    """Employee confirms one of their own published shifts (idempotent)."""
    emp_id, err = _require_emp()
    if err:
        return err
    db = SessionLocal()
    try:
        sh, sched, ferr = _own_published_shift(db, shift_id, emp_id)
        if ferr:
            return ferr
        _upsert_response(db, shift_id, emp_id, "accepted", None)
        db.commit()
        return jsonify({"ok": True, "shift_id": shift_id, "response": "accepted"}), 200
    finally:
        db.close()


@employee_auth.route("/employee/shifts/<int:shift_id>/decline", methods=["POST"])
def emp_shift_decline(shift_id):
    """Employee declines one of their own published shifts. A reason is REQUIRED
    (400 on empty/whitespace) and is recorded for the assigning manager."""
    emp_id, err = _require_emp()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"ok": False, "error": "A reason is required to decline."}), 400
    db = SessionLocal()
    try:
        sh, sched, ferr = _own_published_shift(db, shift_id, emp_id)
        if ferr:
            return ferr
        _upsert_response(db, shift_id, emp_id, "declined", reason)
        db.commit()
        return jsonify({"ok": True, "shift_id": shift_id,
                        "response": "declined", "reason": reason}), 200
    finally:
        db.close()
