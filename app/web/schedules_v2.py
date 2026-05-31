"""Schedules V2 - Block 4: manager DRAFT schedule-creation endpoints.

These routes ride the EXISTING store_bp blueprint (URL prefix /<store_slug>/),
so they inherit store_bp's gates for free:
  - _pull_store sets g.current_store (404 on an unknown store slug)
  - _per_store_gate enforces the audience gate BEFORE the view runs: a gm scoped
    to Tomball hitting /<copperfield>/... gets 403/redirect with ZERO rows touched
    (403-before-mutation). partner/corporate are unrestricted.
On top of that, @require_level("foh_manager") gates to MANAGERS (gm/manager/km/
assistant_km/corporate_chef/prep_manager/foh_manager + partner/corporate);
expo/driver get 403 and employees (SMS sessions, no keypad user) get redirected
to login -> they cannot create or read drafts here.

aick owns this file; ck builds the week-view UI against these endpoints. B7/B8
hooks (scheduling_timeoff.conflict / scheduling_availability.warning) are called
on shift create/update as no-op stubs now (ckai fills them in B7/B8).
"""
from __future__ import annotations

from datetime import date as _date, datetime

from flask import g, jsonify, request

from app.db import SessionLocal
from app.models import (
    Employee,
    EmployeeStoreAssignment,
    Position,
    Schedule,
    Shift,
    ShiftTag,
    Tag,
)
from app.services import scheduling_alarms, scheduling_availability, scheduling_timeoff
from app.web.permissions import current_user_id, require_level
from app.web.store_routes import store_bp

_MGR = "foh_manager"  # lowest manager level allowed to manage schedules


def _store() -> str | None:
    """The store_key stored on schedules/shifts is the LOCATION
    ('tomball'/'copperfield'), NOT the URL slug ('dos'/'uno') - so it joins with
    employee_store_assignments.store_key + User.store_scope, which are
    location-keyed (B2 contract + B3 migration). The audience GATE still keys off
    the slug via store_bp._per_store_gate; only storage/filtering uses location.
    (ckai #1887 cross-block fix.)"""
    return getattr(g, "current_location", None)


def _dt(s):
    return datetime.fromisoformat(s) if s else None


@store_bp.route("/schedules-v2/employees/add", methods=["POST"])
@require_level(_MGR)
def sv2_employee_add():
    """B11 email-onboarding (#1, aick): a manager admin-adds an employee with
    NAME + EMAIL only ("basic info", Sam #2088 / samai #2097). Creates the
    Employee (active; no passcode, no phone) and fires the email setup invite
    (ckai's employee_setup.send_setup_invite -> mints a one-time token + emails
    the /employee/setup/<token> link via the orders@ SMTP). The employee then
    self-completes their profile + sets a 5-digit passcode at that link.

    Deliberately does NOT set store/position here: those are MANAGER-set via a
    separate route (samai's privilege boundary #2097 - an employee self-assigning
    store/position would self-grant scheduling reach). A new hire onboards fully
    (setup -> passcode -> login) and simply sees an empty schedule until a manager
    assigns them. Email is the login IDENTITY, so duplicates are rejected."""
    data = request.get_json(silent=True) or {}
    full_name = (data.get("full_name") or "").strip()
    email = (data.get("email") or "").strip()
    if not full_name:
        return jsonify({"ok": False, "error": "Name is required."}), 400
    # basic email sanity (real validation is delivery): one @, a dotted domain
    parts = email.split("@")
    if len(parts) != 2 or not parts[0] or "." not in parts[1] or parts[1].endswith("."):
        return jsonify({"ok": False, "error": "A valid email is required."}), 400

    db = SessionLocal()
    try:
        # Email is the login identity (login + invite key off it), so it must be
        # unique. No DB UNIQUE on employees.email, so guard here (case-insensitive,
        # small-table scan like _find_employee_by_identifier).
        el = email.lower()
        if any((e.email or "").lower() == el for e in db.query(Employee).all()):
            return jsonify({"ok": False,
                            "error": "An employee with that email already exists."}), 409
        emp = Employee(full_name=full_name, email=email, active=True)
        db.add(emp)
        db.commit()
        emp_id = emp.id
    finally:
        db.close()

    # Fire the invite AFTER our session closes (send_setup_invite opens its own).
    # It never raises into us (logs the link on SMTP failure), so the add still
    # succeeds + a re-add/re-invite just issues a fresh token.
    from app.web.employee_setup import send_setup_invite
    send_setup_invite(emp_id)
    return jsonify({"ok": True, "employee_id": emp_id,
                    "message": "Invitation emailed to %s." % email}), 200


@store_bp.route("/schedules-v2/schedule/new", methods=["POST"])
@require_level(_MGR)
def sv2_schedule_new():
    data = request.get_json(silent=True) or {}
    ws = (data.get("week_start") or "").strip()
    try:
        week_start = _date.fromisoformat(ws)
    except Exception:
        return jsonify({"ok": False, "error": "week_start required (YYYY-MM-DD)"}), 400
    db = SessionLocal()
    try:
        existing = db.query(Schedule).filter_by(store_key=_store(), week_start=week_start).first()
        if existing:
            return jsonify({"ok": False, "error": "a schedule for that week already exists",
                            "id": existing.id}), 409
        now = datetime.utcnow()
        sched = Schedule(store_key=_store(), week_start=week_start, status="draft",
                         created_by=current_user_id(), created_at=now, updated_at=now)
        db.add(sched)
        db.commit()
        return jsonify({"ok": True, "id": sched.id, "store": _store(),
                        "week_start": week_start.isoformat(), "status": "draft"}), 201
    finally:
        db.close()


@store_bp.route("/schedules-v2/schedule", methods=["GET"])
@require_level(_MGR)
def sv2_schedule_list():
    db = SessionLocal()
    try:
        q = db.query(Schedule).filter_by(store_key=_store())
        wk = request.args.get("week")
        if wk:
            try:
                q = q.filter_by(week_start=_date.fromisoformat(wk))
            except Exception:
                pass
        out = [{"id": s.id, "week_start": s.week_start.isoformat(), "status": s.status,
                "published_at": s.published_at.isoformat() if s.published_at else None}
               for s in q.order_by(Schedule.week_start.desc()).all()]
        return jsonify({"ok": True, "store": _store(), "schedules": out}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/board", methods=["GET"])
@require_level(_MGR)
def sv2_board():
    """Single round-trip for the week-view grid (ck #1893): the week's schedule +
    its shifts + the store roster + positions + tags. No schedule for the week ->
    schedule:null + shifts:[] (UI shows a create-draft CTA)."""
    wk = (request.args.get("week") or "").strip()
    try:
        week_start = _date.fromisoformat(wk)
    except Exception:
        return jsonify({"ok": False, "error": "week required (YYYY-MM-DD)"}), 400
    store = _store()  # location ("tomball"/"copperfield")
    db = SessionLocal()
    try:
        sched = db.query(Schedule).filter_by(store_key=store, week_start=week_start).first()
        shifts = []
        if sched:
            for sh in db.query(Shift).filter_by(schedule_id=sched.id).all():
                tag_ids = [st.tag_id for st in db.query(ShiftTag).filter_by(shift_id=sh.id).all()]
                shifts.append({
                    "id": sh.id, "employee_id": sh.employee_id, "position_id": sh.position_id,
                    "start_at": sh.start_at.isoformat() if sh.start_at else None,
                    "end_at": sh.end_at.isoformat() if sh.end_at else None,
                    "break_minutes": sh.break_minutes, "status": sh.status,
                    "notes": sh.notes, "tag_ids": tag_ids,
                })
        # roster = employees assigned to THIS store (location-keyed)
        emp_ids = [a.employee_id for a in
                   db.query(EmployeeStoreAssignment).filter_by(store_key=store).all()]
        roster = []
        if emp_ids:
            for e in (db.query(Employee).filter(Employee.id.in_(emp_ids))
                        .order_by(Employee.full_name).all()):
                roster.append({"id": e.id, "full_name": e.full_name, "active": e.active})
        # positions (B1/Sling taxonomy; store_key null = all-store). NB: no "area"
        # column exists - store_key is the only grouping field. Flag to ck.
        positions = [{"id": p.id, "name": p.name, "store_key": p.store_key}
                     for p in db.query(Position).order_by(Position.name).all()]
        tags = [{"id": t.id, "name": t.name} for t in db.query(Tag).order_by(Tag.name).all()]
        return jsonify({
            "ok": True, "store": store,
            "schedule": ({"id": sched.id, "week_start": sched.week_start.isoformat(),
                          "status": sched.status,
                          "published_at": sched.published_at.isoformat() if sched.published_at else None}
                         if sched else None),
            "shifts": shifts, "roster": roster, "positions": positions, "tags": tags,
        }), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/shifts/new", methods=["POST"])
@require_level(_MGR)
def sv2_shift_new():
    data = request.get_json(silent=True) or {}
    sid = data.get("schedule_id")
    start_at, end_at = _dt(data.get("start_at")), _dt(data.get("end_at"))
    if not sid or not start_at or not end_at:
        return jsonify({"ok": False, "error": "schedule_id, start_at, end_at required"}), 400
    db = SessionLocal()
    try:
        # schedule must belong to THIS store (no cross-store shift insertion)
        sched = db.query(Schedule).filter_by(id=sid, store_key=_store()).first()
        if sched is None:
            return jsonify({"ok": False, "error": "schedule not found in this store"}), 404
        emp_id = data.get("employee_id")
        status = "assigned" if emp_id else "open"
        if emp_id:
            blocker = scheduling_timeoff.conflict(emp_id, start_at.date())  # B7 hook
            if blocker:
                return jsonify({"ok": False, "error": blocker}), 409
        warn = scheduling_availability.warning(emp_id, start_at) if emp_id else None  # B8 hook
        now = datetime.utcnow()
        sh = Shift(schedule_id=sid, employee_id=emp_id, position_id=data.get("position_id"),
                   start_at=start_at, end_at=end_at, break_minutes=int(data.get("break_minutes") or 0),
                   status=status, notes=data.get("notes"), created_at=now, updated_at=now)
        db.add(sh)
        db.flush()
        for tid in (data.get("tag_ids") or []):
            db.add(ShiftTag(shift_id=sh.id, tag_id=tid, created_at=now))
        db.commit()
        return jsonify({"ok": True, "id": sh.id, "status": status, "warning": warn}), 201
    finally:
        db.close()


def _shift_in_store(db, shift_id):
    """A shift whose schedule belongs to the current store, else None."""
    return (db.query(Shift)
              .join(Schedule, Shift.schedule_id == Schedule.id)
              .filter(Shift.id == shift_id, Schedule.store_key == _store())
              .first())


@store_bp.route("/schedules-v2/shifts/<int:shift_id>", methods=["PUT"])
@require_level(_MGR)
def sv2_shift_update(shift_id):
    data = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        sh = _shift_in_store(db, shift_id)
        if sh is None:
            return jsonify({"ok": False, "error": "shift not found in this store"}), 404
        if "start_at" in data:
            sh.start_at = _dt(data["start_at"])
        if "end_at" in data:
            sh.end_at = _dt(data["end_at"])
        if "position_id" in data:
            sh.position_id = data["position_id"]
        if "break_minutes" in data:
            sh.break_minutes = int(data["break_minutes"] or 0)
        if "notes" in data:
            sh.notes = data["notes"]
        if "employee_id" in data:
            sh.employee_id = data["employee_id"]
            sh.status = "assigned" if data["employee_id"] else "open"
        # B7: an update that leaves the shift ASSIGNED must respect approved
        # time-off on the (possibly new) employee + (possibly new) date - the same
        # guard sv2_shift_new applies on create. Nothing is committed yet, so a 409
        # here leaves the shift untouched (ckai flagged this PUT-reassign gap #1974).
        if sh.employee_id and sh.start_at:
            blocker = scheduling_timeoff.conflict(sh.employee_id, sh.start_at.date())
            if blocker:
                return jsonify({"ok": False, "error": blocker}), 409
        # B8: availability is a SOFT advisory - surface it in the response, never
        # block (mirrors sv2_shift_new). ckai filled warning() in B8; I wire the PUT
        # call-site here per his recommendation, so create + PUT both return
        # "warning". (bulk-copy intentionally skipped: a soft per-shift advisory
        # doesn't fit a bulk op and the employee CAN still work - ckai #1984.)
        warn = (scheduling_availability.warning(sh.employee_id, sh.start_at)
                if (sh.employee_id and sh.start_at) else None)
        sh.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "id": sh.id, "status": sh.status, "warning": warn}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/shifts/<int:shift_id>", methods=["DELETE"])
@require_level(_MGR)
def sv2_shift_delete(shift_id):
    db = SessionLocal()
    try:
        sh = _shift_in_store(db, shift_id)
        if sh is None:
            return jsonify({"ok": False, "error": "shift not found in this store"}), 404
        db.query(ShiftTag).filter_by(shift_id=sh.id).delete()
        db.delete(sh)
        db.commit()
        return jsonify({"ok": True, "deleted": shift_id}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/shifts/bulk-copy", methods=["POST"])
@require_level(_MGR)
def sv2_shift_bulk_copy():
    """Copy every shift from a source schedule into a target schedule (same
    store), shifting each shift's datetimes by the week delta. Template-a-week."""
    data = request.get_json(silent=True) or {}
    src_id, dst_id = data.get("from_schedule_id"), data.get("to_schedule_id")
    if not src_id or not dst_id:
        return jsonify({"ok": False, "error": "from_schedule_id + to_schedule_id required"}), 400
    db = SessionLocal()
    try:
        src = db.query(Schedule).filter_by(id=src_id, store_key=_store()).first()
        dst = db.query(Schedule).filter_by(id=dst_id, store_key=_store()).first()
        if src is None or dst is None:
            return jsonify({"ok": False, "error": "source/target schedule not found in this store"}), 404
        offset = dst.week_start - src.week_start  # timedelta (week delta)
        now = datetime.utcnow()
        n = 0
        opened_for_timeoff = 0
        for sh in db.query(Shift).filter_by(schedule_id=src.id).all():
            new_start = sh.start_at + offset
            emp_id, status = sh.employee_id, sh.status
            # B7: don't template an assignment onto the employee's approved time-off.
            # Rather than 409 the whole copy for one conflict (bad UX), bring that
            # slot over as OPEN (coverage preserved) + report the count so the
            # manager can re-fill it. (ckai flagged this bulk-copy gap #1974.)
            if emp_id and scheduling_timeoff.conflict(emp_id, new_start.date()):
                emp_id, status = None, "open"
                opened_for_timeoff += 1
            db.add(Shift(schedule_id=dst.id, employee_id=emp_id, position_id=sh.position_id,
                         start_at=new_start, end_at=sh.end_at + offset,
                         break_minutes=sh.break_minutes, status=status, notes=sh.notes,
                         created_at=now, updated_at=now))
            n += 1
        db.commit()
        return jsonify({"ok": True, "copied": n, "to_schedule_id": dst_id,
                        "opened_for_timeoff": opened_for_timeoff}), 201
    finally:
        db.close()


@store_bp.route("/schedules-v2/schedule/<int:schedule_id>/publish", methods=["POST"])
@require_level(_MGR)
def sv2_schedule_publish(schedule_id):
    """B5: publish a draft -> its shifts become visible to the assigned employees
    (who can accept/decline). Idempotent (re-publish stays published). After the
    status flip, fire the B6 alarm hook. Manager-gated + store-scoped."""
    db = SessionLocal()
    try:
        sched = db.query(Schedule).filter_by(id=schedule_id, store_key=_store()).first()
        if sched is None:
            return jsonify({"ok": False, "error": "schedule not found in this store"}), 404
        now = datetime.utcnow()
        sched.status = "published"
        if sched.published_at is None:
            sched.published_at = now
        sched.updated_at = now
        db.commit()
        # B6 hook (ckai #1912): create pending shift_alarms AFTER the status flip.
        # No-op stub today; wrapped so a future B6 error can never fail the publish.
        try:
            scheduling_alarms.create_for_schedule(sched.id)
        except Exception:  # noqa: BLE001
            pass
        return jsonify({"ok": True, "id": sched.id, "status": sched.status,
                        "published_at": sched.published_at.isoformat()}), 200
    finally:
        db.close()
