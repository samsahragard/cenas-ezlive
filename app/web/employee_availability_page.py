"""Schedules V2 - Block 8: the employee availability PAGE (frontend shell, ck).

GET /employee/availability renders the mobile view where a logged-in employee
declares their recurring weekly availability (the windows they CAN work) and adds
one-off unavailability blocks (CANNOT-work spans).

Same split as B5/B6/B7: this page route attaches to the employee_auth blueprint
(imported in app/__init__.py before ezempauth.install) so employee_auth.py stays
ckai's lane. The page reads ONLY the Employee name; ckai owns the availability
models + the data/action endpoints. Config hands the client the endpoint PATHS,
LOCKED #1986 (B7-style): the PAGE owns the parent /employee/availability, the DATA
list is at /employee/availability/list.

AUTH: /employee/availability is in auth.py EXEMPT_PREFIXES so a session-less hit
reaches THIS view (not the staff keypad); the view self-guards on
session['employee_id'] and 302s to /employee/login. The one prefix also covers
ckai's /employee/availability/list + /recurring + /block endpoints, each with its
own employee_id guard (401 JSON). Every datum here is scoped to session['employee_id'].
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from flask import redirect, render_template, session

from app.db import SessionLocal
from app.models import Employee
from app.web.employee_auth import employee_auth


@employee_auth.route("/employee/availability", methods=["GET"])
def employee_availability_page():
    """Render the client-side availability editor. Requires an employee session;
    with none we bounce to /employee/login (the employee door, not the keypad).
    Only the Employee name is read here - recurring windows + blocks come from
    ckai's data endpoint via fetch()."""
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
        view = SimpleNamespace(first_name=first_name, full_name=full_name or None)
    finally:
        db.close()

    config = {
        "listUrl": "/employee/availability/list",          # ckai: GET -> {ok, recurring:[...], blocks:[...]}
        "recurringBase": "/employee/availability/recurring",  # ckai: POST {day_of_week,start_time,end_time}; DELETE <base>/<id>
        "blockBase": "/employee/availability/block",        # ckai: POST {start_at,end_at,reason?}; DELETE <base>/<id>
        "dashboardUrl": "/employee/dashboard",
        "loginUrl": "/employee/login",
    }
    return render_template(
        "employee_availability.html",
        employee=view,
        config_json=json.dumps(config),
        dashboard_url="/employee/dashboard",
        login_url="/employee/login",
    )
