"""Schedules V2 - Block 8: the MANAGER availability view (ckai).

  GET /<store>/schedules-v2/availability/list [?employee_id]  -> this store's
      employees' recurring windows + unavailability blocks (for the scheduling
      calendar). JSON; /list reserves the bare path for ck's HTML page (B5-B7 split).

Rides store_bp (/<store_slug>/) so it inherits _pull_store + _per_store_gate (a
cross-store manager is 403/redirected before the view) + the partner factor; plus
@require_level('foh_manager'). Store-scoped: only employees with an
employee_store_assignments row for g.current_location (the location). Availability
is read-only here - it's the employee's to set; the manager just views it.
"""
from __future__ import annotations

from flask import g, jsonify, request

from app.db import SessionLocal
from app.models import (Employee, EmployeeAvailability, EmployeeStoreAssignment,
                        EmployeeUnavailabilityBlock)
from app.web.permissions import require_level
from app.web.store_routes import store_bp

_MGR = "foh_manager"


def _store():
    return getattr(g, "current_location", None)


def _fmt_hhmm(m) -> str:
    h, mi = divmod(int(m), 60)
    return "%02d:%02d" % (h, mi)


@store_bp.route("/schedules-v2/availability/list", methods=["GET"])
@require_level(_MGR)
def sv2_availability_list():
    """This store's employees' availability (recurring + blocks). ?employee_id
    narrows to one (only if that employee is assigned to this store)."""
    db = SessionLocal()
    try:
        emp_ids = [a[0] for a in
                   (db.query(EmployeeStoreAssignment.employee_id)
                      .filter(EmployeeStoreAssignment.store_key == _store()).all())]
        eid_filter = (request.args.get("employee_id") or "").strip()
        if eid_filter:
            try:
                fid = int(eid_filter)
            except ValueError:
                return jsonify({"ok": False, "error": "employee_id must be an integer"}), 400
            emp_ids = [e for e in emp_ids if e == fid]  # never reveal an out-of-store employee
        if not emp_ids:
            return jsonify({"ok": True, "availability": []}), 200

        names = {e.id: e.full_name for e in
                 db.query(Employee).filter(Employee.id.in_(emp_ids)).all()}
        recs_by_emp = {}
        for r in (db.query(EmployeeAvailability)
                    .filter(EmployeeAvailability.employee_id.in_(emp_ids)).all()):
            recs_by_emp.setdefault(r.employee_id, []).append(
                {"id": r.id, "day_of_week": r.day_of_week,
                 "start_time": _fmt_hhmm(r.start_minute), "end_time": _fmt_hhmm(r.end_minute)})
        blks_by_emp = {}
        for b in (db.query(EmployeeUnavailabilityBlock)
                    .filter(EmployeeUnavailabilityBlock.employee_id.in_(emp_ids)).all()):
            blks_by_emp.setdefault(b.employee_id, []).append(
                {"id": b.id,
                 "start_at": b.start_at.isoformat() if b.start_at else None,
                 "end_at": b.end_at.isoformat() if b.end_at else None,
                 "reason": b.reason})
        out = [{"employee_id": eid, "employee_name": names.get(eid),
                "recurring": recs_by_emp.get(eid, []), "blocks": blks_by_emp.get(eid, [])}
               for eid in emp_ids]
        return jsonify({"ok": True, "availability": out}), 200
    finally:
        db.close()
