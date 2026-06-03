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
from types import SimpleNamespace

from flask import redirect, render_template, session

from app.db import SessionLocal
from app.models import Employee, EmployeePosition, EmployeeStoreAssignment, Position
from app.web.employee_auth import _STORE_LABELS, employee_auth


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

        stores = [
            _STORE_LABELS.get(sk, (sk or "").title())
            for (sk,) in (
                db.query(EmployeeStoreAssignment.store_key)
                  .filter(EmployeeStoreAssignment.employee_id == emp.id)
                  .order_by(EmployeeStoreAssignment.store_key.asc())
                  .all()
            )
            if sk
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

        view = SimpleNamespace(
            first_name=first_name,
            full_name=full_name or None,
            stores=stores,
            positions=positions,
        )
    finally:
        db.close()

    config = {
        "performanceUrl": "/employee/performance-center",
        "scheduleUrl": "/employee/my-schedule/shifts",
        "rosterUrl": "/employee/roster",
        "dashboardUrl": "/employee/dashboard",
        "loginUrl": "/employee/login",
    }
    return render_template(
        "employee_my_profile.html",
        employee=view,
        config_json=json.dumps(config),
        dashboard_url="/employee/dashboard",
        login_url="/employee/login",
    )
