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
from app.models import Schedule, Shift, ShiftTag
from app.services import scheduling_availability, scheduling_timeoff
from app.web.permissions import current_user_id, require_level
from app.web.store_routes import store_bp

_MGR = "foh_manager"  # lowest manager level allowed to manage schedules


def _store() -> str | None:
    return getattr(g, "current_store", None)


def _dt(s):
    return datetime.fromisoformat(s) if s else None


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
        sh.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "id": sh.id, "status": sh.status}), 200
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
        for sh in db.query(Shift).filter_by(schedule_id=src.id).all():
            db.add(Shift(schedule_id=dst.id, employee_id=sh.employee_id, position_id=sh.position_id,
                         start_at=sh.start_at + offset, end_at=sh.end_at + offset,
                         break_minutes=sh.break_minutes, status=sh.status, notes=sh.notes,
                         created_at=now, updated_at=now))
            n += 1
        db.commit()
        return jsonify({"ok": True, "copied": n, "to_schedule_id": dst_id}), 201
    finally:
        db.close()
