"""Read-only employee profile hub.

GET /employee/my-profile renders a polished staff profile shell for the logged-in
employee. It reads only the session employee's display identity, stores, and
positions, then the client fetches existing session-scoped employee endpoints for
performance, roster, and schedule data. It does not accept employee identifiers
from the request and does not expose PII, Toast ids, link internals, or manager
free text.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from flask import redirect, render_template, session

from app.db import SessionLocal
from app.models import (
    Employee,
    EmployeePosition,
    EmployeeStoreAssignment,
    Position,
    Schedule,
    Shift,
    ShiftAcceptance,
)
from app.web.employee_auth import _STORE_LABELS, employee_auth


def _date_label(dt, today):
    d = dt.date()
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return dt.strftime("%a, %b ") + str(d.day)


def _time_label(dt):
    return dt.strftime("%I:%M %p").lstrip("0") if dt else ""


def _week_bounds(today):
    this_monday = today - timedelta(days=today.weekday())
    return this_monday, this_monday + timedelta(days=7)


def _profile_schedule(db, emp_id):
    """Own published shifts, stripped to employee-visible profile fields only."""
    today = datetime.utcnow().date()
    weeks = list(_week_bounds(today))
    scheds = (
        db.query(Schedule)
          .filter(Schedule.week_start.in_(weeks), Schedule.status == "published")
          .all()
    )
    week_by_sched = {s.id: s.week_start for s in scheds}
    if not week_by_sched:
        return {"shifts": []}

    rows = (
        db.query(Shift, Position.name)
          .outerjoin(Position, Shift.position_id == Position.id)
          .filter(
              Shift.employee_id == emp_id,
              Shift.schedule_id.in_(list(week_by_sched.keys())),
          )
          .order_by(Shift.start_at.asc())
          .all()
    )
    shift_ids = [sh.id for sh, _ in rows]
    resp_by_shift = {}
    if shift_ids:
        for a in (
            db.query(ShiftAcceptance)
              .filter(
                  ShiftAcceptance.employee_id == emp_id,
                  ShiftAcceptance.shift_id.in_(shift_ids),
              )
              .all()
        ):
            resp_by_shift[a.shift_id] = a.response

    shifts = []
    for sh, pos_name in rows:
        shifts.append({
            "start_at": sh.start_at.isoformat() if sh.start_at else None,
            "end_at": sh.end_at.isoformat() if sh.end_at else None,
            "date_label": _date_label(sh.start_at, today) if sh.start_at else "Scheduled",
            "time_label": (
                (_time_label(sh.start_at) + " - " + _time_label(sh.end_at)).strip(" -")
                if sh.start_at or sh.end_at else ""
            ),
            "position_name": (pos_name or "").strip(),
            "response": resp_by_shift.get(sh.id) or "pending",
        })
    return {"shifts": shifts}


def _profile_roster(db, stores):
    """Store roster preview with names and next shifts only; no hidden identifiers."""
    if not stores:
        return {"coworkers": []}
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    horizon = start + timedelta(days=8)
    rows = (
        db.query(Shift, Employee.full_name, Position.name, Schedule.store_key)
          .join(Schedule, Shift.schedule_id == Schedule.id)
          .join(Employee, Shift.employee_id == Employee.id)
          .outerjoin(Position, Shift.position_id == Position.id)
          .filter(
              Schedule.store_key.in_(stores),
              Schedule.status == "published",
              Shift.status == "assigned",
              Shift.employee_id.isnot(None),
              Shift.start_at >= start,
              Shift.start_at < horizon,
          )
          .order_by(Shift.start_at.asc())
          .all()
    )
    seen = set()
    coworkers = []
    for sh, full_name, pos_name, store_key in rows:
        name = (full_name or "").strip()
        if not name or sh.employee_id in seen:
            continue
        seen.add(sh.employee_id)
        coworkers.append({
            "name": name,
            "position": (pos_name or "").strip(),
            "store": _STORE_LABELS.get(store_key, (store_key or "").title()),
            "shift_when": _date_label(sh.start_at, today) if sh.start_at else "Scheduled",
            "shift_label": (
                (_time_label(sh.start_at) + " - " + _time_label(sh.end_at)).strip(" -")
                if sh.start_at or sh.end_at else ""
            ),
        })
    return {"coworkers": coworkers}


@employee_auth.route("/employee/my-profile", methods=["GET"])
def employee_my_profile_page():
    """Render the read-only staff profile hub for the session employee."""
    emp_id = session.get("employee_id")
    if not emp_id:
        return redirect("/employee/login")

    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == emp_id).first()
        if emp is None:
            for k in ("employee_id", "employee_session_version", "auth_ok"):
                session.pop(k, None)
            return redirect("/employee/login")

        full_name = (emp.full_name or "").strip()
        first_name = full_name.split(" ")[0] if full_name else None

        store_keys = [
            sk
            for (sk,) in (
                db.query(EmployeeStoreAssignment.store_key)
                  .filter(EmployeeStoreAssignment.employee_id == emp.id)
                  .order_by(EmployeeStoreAssignment.store_key.asc())
                  .all()
            )
            if sk
        ]
        stores = [
            _STORE_LABELS.get(sk, (sk or "").title())
            for sk in store_keys
        ]

        pos_rows = (
            db.query(EmployeePosition.store_key, Position.name)
              .outerjoin(Position, EmployeePosition.position_id == Position.id)
              .filter(EmployeePosition.employee_id == emp.id)
              .order_by(EmployeePosition.store_key.asc(), Position.name.asc())
              .all()
        )
        positions = []
        seen = set()
        for store_key, name in pos_rows:
            label = (name or "").strip()
            if not label:
                continue
            store_label = _STORE_LABELS.get(store_key, (store_key or "").title()) if store_key else None
            key = (label, store_label)
            if key in seen:
                continue
            seen.add(key)
            positions.append({"name": label, "store": store_label})

        initials = "".join(w[0] for w in (full_name or "").split()[:2]).upper() or "--"
        role = positions[0]["name"] if positions else "Team member"
        location = stores[0] if stores else None
        view = SimpleNamespace(
            first_name=first_name,
            full_name=full_name or None,
            stores=stores,
            positions=positions,
            initials=initials,
            role=role,
            location=location,
        )
        profile_data = {
            "schedule": _profile_schedule(db, emp.id),
            "roster": _profile_roster(db, store_keys),
        }
    finally:
        db.close()

    config = {
        "performanceUrl": "/employee/performance-center",
        "myPerfUrl": "/employee/my-performance",
        "dashboardUrl": "/employee/dashboard",
        "loginUrl": "/employee/login",
    }
    return render_template(
        "employee_my_profile.html",
        employee=view,
        tenure_label=None,         # not derivable from the current Employee row
        config_json=json.dumps(config),
        profile_data_json=json.dumps(profile_data),
        dashboard_url="/employee/dashboard",
        login_url="/employee/login",
    )
