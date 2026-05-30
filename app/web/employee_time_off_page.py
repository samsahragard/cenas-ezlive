"""Schedules V2 - Block 7: the employee time-off PAGE (frontend shell, ck).

GET /employee/time-off renders the mobile view where a logged-in employee
submits a time-off request (date range + optional reason), sees their request
history with status, and cancels a still-pending request.

Same split as B5/B6: this page route attaches to the employee_auth blueprint
(imported in app/__init__.py before ezempauth.install) so employee_auth.py stays
ckai's lane. The page reads ONLY the Employee name; ckai owns the time-off model
+ the data/action endpoints. Config hands the client the endpoint PATHS, LOCKED
#1976 (B5-consistent): the PAGE owns the parent /employee/time-off, the DATA list
is at /employee/time-off/list.

AUTH: /employee/time-off is in auth.py EXEMPT_PREFIXES so a session-less hit
reaches THIS view (not the staff keypad); the view self-guards on
session['employee_id'] and 302s to /employee/login. The one EXEMPT prefix also
covers ckai's /employee/time-off/list + /request + /<id> endpoints, each of which
has its own employee_id guard (401/own-row); isolation is enforced there, not by
this site gate. Every datum here is scoped to session['employee_id'].
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from flask import redirect, render_template, session

from app.db import SessionLocal
from app.models import Employee
from app.web.employee_auth import employee_auth


@employee_auth.route("/employee/time-off", methods=["GET"])
def employee_time_off_page():
    """Render the client-side time-off view. Requires an employee session;
    with none we bounce to /employee/login (the employee door, not the keypad).
    Only the Employee name is read here - requests come from ckai's data
    endpoint via fetch()."""
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
        "listUrl": "/employee/time-off/list",        # ckai: GET -> {ok, requests:[...]}
        "requestUrl": "/employee/time-off/request",   # ckai: POST {start_date,end_date,reason?} -> 201
        "itemBase": "/employee/time-off",             # ckai: DELETE <base>/<id> (own pending)
        "dashboardUrl": "/employee/dashboard",
        "loginUrl": "/employee/login",
    }
    return render_template(
        "employee_time_off.html",
        employee=view,
        config_json=json.dumps(config),
        dashboard_url="/employee/dashboard",
        login_url="/employee/login",
    )
